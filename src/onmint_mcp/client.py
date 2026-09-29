"""
Async HTTP client for the on:mint authenticity API (onmint-appcontroller-api /authenticity).

Encapsulates the full developer flow so the MCP tools stay thin:
  - submit_and_wait: create -> fetch presigned URL(s) -> upload (single/multipart) -> poll.
    The upload is a zip-of-one (see `zip_of_one`), which is what the ingest expects.
  - verify / analyze: multipart forwards
  - get_status / get_provenance: reads

All requests carry the API-key headers. The optional `transport` argument lets tests inject
an httpx.MockTransport so the flow can be exercised without a live backend.
"""
import asyncio
import io
import mimetypes
import os
import zipfile
from typing import Any, Optional

import httpx

from onmint_mcp import settings

_PART_SIZE = 30 * 1024 * 1024  # 30 MB — matches the backend multipart threshold

# The four declarations a rights holder may choose. Mirrored here as plain strings rather
# than imported, because this package deliberately depends on nothing but httpx and mcp — an
# MCP server a developer installs with `pip install git+...` should not drag in the platform's
# internal packages. The API is the authority: an unknown value is rejected there with a 422,
# so this list can only ever be a nicer error, never a second source of truth.
AI_DECLARATIONS = ("CREATED_WITHOUT_AI", "AI_ENHANCED", "AI_MODIFIED", "AI_GENERATED")
_PRESIGN_READY = {"FILEDGR_RECEIVED", "FILEDGR_REVIEWED"}
# FAILED is where every genuine pipeline failure lands (and the only status refunded); ERROR
# is kept for the one legacy site that still writes it. Without FAILED here a failed
# submission was polled until POLL_TIMEOUT_SECONDS and reported as a timeout.
_TERMINAL = {"FILEDGR_DATA_ATTACHMENT_COMPLETED", "FAILED", "ERROR"}


class OnmintApiError(RuntimeError):
    pass


# The stable code the API answers a `label_template` it cannot resolve with.
TEMPLATE_UNKNOWN = "MINTYS_TEMPLATE_UNKNOWN"


class LabelTemplateUnknownError(OnmintApiError):
    """`label_template` names a template the organization does not have.

    Its own type because the right reaction differs from every other 400: nothing was
    submitted and nothing was labelled with a substitute, and the fix is to re-read the
    templates, not to change code. The message says so, since it is what a calling model reads.
    """


def _raise_for(method: str, path: str, resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    if resp.status_code == 400:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("error") == TEMPLATE_UNKNOWN:
            raise LabelTemplateUnknownError(
                f"{TEMPLATE_UNKNOWN}: the label_template id is not one of this organization's "
                "label templates. Nothing was submitted and nothing was labelled with a "
                "substitute template. Call list_label_templates for the valid ids, or omit "
                "label_template to use the organization's default.")
    raise OnmintApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")


class OnmintClient:
    def __init__(self,
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 api_secret: Optional[str] = None,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._base = (base_url or settings.ONMINT_API_URL).rstrip("/")
        self._headers = {
            "x-api-key": api_key or settings.ONMINT_API_KEY,
            "x-api-secret": api_secret or settings.ONMINT_API_SECRET,
        }
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=60.0, transport=self._transport)

    async def _json(self, method: str, path: str, **kw) -> Any:
        url = f"{self._base}{path}"
        async with self._client() as c:
            resp = await c.request(method, url, headers=self._headers, **kw)
        _raise_for(method, path, resp)
        return resp.json() if resp.content else None

    # --------------------------------------------------------------- submit
    async def submit_and_wait(self,
                              stream_id: str,
                              file_bytes: bytes,
                              filename: str,
                              ai_declaration: str,
                              name: Optional[str] = None,
                              non_ai_medium: Optional[str] = None,
                              visible_ai_label: bool = False,
                              allow_ai_training_and_mining: bool = False,
                              title: Optional[str] = None,
                              category: Optional[str] = None,
                              ledger: Optional[str] = None,
                              wait: bool = True,
                              label_template: Optional[str] = None) -> dict:
        """Create a submission, upload the file, and (optionally) wait for the pipeline to finish.

        `ai_declaration` is REQUIRED and positional-by-convention (it sits ahead of every
        optional argument) because the API requires it: a submission without one is rejected
        with a 422 and no credit is spent. It is the authoritative AI label — what gets signed
        into the C2PA manifest — so there is no default here either. Guessing it on the
        caller's behalf would be putting words in a rights holder's mouth, cryptographically.

        `label_template` names one of the organization's label templates (how the visible
        label LOOKS). It is put on the wire only when given: an omitted field is what tells
        the API to use the organization's default, and an explicit null is a different
        request. An id the account does not have is refused with 400
        MINTYS_TEMPLATE_UNKNOWN before anything is created, never swapped for the default.

        Returns the final attachment dict. Per-file provenance (watermark_id, content_class,
        vetting verdict) lives under `files[]` once processing completes.
        """
        if ai_declaration not in AI_DECLARATIONS:
            raise OnmintApiError(
                f"ai_declaration must be one of {list(AI_DECLARATIONS)}, got {ai_declaration!r}")

        disclosure = {
            "declaration": ai_declaration,
            # Sent explicitly rather than omitted. An absent entry is an answer the server
            # has to assume, and "did not say" and "said no" are different statements — most
            # of all for training rights, where an unstated entry reads to a scraper as no
            # objection recorded.
            "visible_ai_label": visible_ai_label,
            "allow_ai_training_and_mining": allow_ai_training_and_mining,
        }
        if non_ai_medium is not None:
            disclosure["non_ai_medium"] = non_ai_medium

        # What goes up is a zip-of-one, never the raw file. The watermark ingest opens the
        # uploaded object with ZipFile unconditionally, so a raw MP4 or JPEG PUT to the
        # presigned URL failed as `internal_error exc=BadZipFile` and was refunded: no MCP
        # submission could ever be minted. This is the same shape the webapp sends
        # (createZipFromFiles with one entry, upload_as_zip=false): the ENTRY name is what
        # the ingest resolves the type from, so it has to carry the real extension, and the
        # create-body `filename` stays the media's own name, not the zip's.
        payload = zip_of_one(file_bytes, filename)

        create_body = {
            "name": name or filename,
            "stream_id": stream_id,
            "ledger": ledger or settings.DEFAULT_LEDGER,
            "filename": filename,
            # The size of what is uploaded, not of the media: the presigned part count is
            # derived from it, and a stored zip is a few hundred bytes larger than its entry.
            "estimated_size": len(payload),
            "ai_disclosure": disclosure,
        }
        if title is not None:
            create_body["title"] = title
        if category is not None:
            create_body["category"] = category
        if label_template is not None:
            create_body["label_template"] = label_template

        created = await self._json("POST", "/authenticity/attachments", json=create_body)
        attachment_id = created["id"]

        # Presigned upload URLs are served on GET while the attachment is RECEIVED/REVIEWED.
        attachment = await self._await_presigned(attachment_id)
        await self._upload(attachment_id, attachment, payload)

        if not wait:
            return await self.get_status(attachment_id)
        return await self._poll_terminal(attachment_id)

    async def _await_presigned(self, attachment_id: str) -> dict:
        waited = 0.0
        while waited <= settings.POLL_TIMEOUT_SECONDS:
            att = await self.get_status(attachment_id)
            if att.get("presigned_urls"):
                return att
            if att.get("status") not in _PRESIGN_READY:
                # Moved past the upload window without ever exposing URLs — surface it.
                raise OnmintApiError(
                    f"attachment {attachment_id} reached {att.get('status')} before upload URLs appeared")
            await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)
            waited += settings.POLL_INTERVAL_SECONDS
        raise OnmintApiError(f"timed out waiting for upload URLs for {attachment_id}")

    async def _upload(self, attachment_id: str, attachment: dict, file_bytes: bytes) -> None:
        parts = attachment["presigned_urls"]
        async with self._client() as c:
            if len(parts) == 1:
                # Single-part: PUT the whole file to the presigned link. No complete step.
                link = parts[0]["link"]
                resp = await c.put(link, content=file_bytes)
                if resp.status_code >= 400:
                    raise OnmintApiError(f"single-part upload failed: {resp.status_code}")
                return
            # Multipart: PUT each 30 MB chunk, collect ETags, then complete.
            completed = []
            for part in sorted(parts, key=lambda p: p["part"]):
                idx = part["part"] - 1
                chunk = file_bytes[idx * _PART_SIZE:(idx + 1) * _PART_SIZE]
                resp = await c.put(part["link"], content=chunk)
                if resp.status_code >= 400:
                    raise OnmintApiError(f"multipart upload part {part['part']} failed: {resp.status_code}")
                etag = (resp.headers.get("ETag") or resp.headers.get("etag") or "").strip('"')
                completed.append({"part": part["part"], "etag": etag})
        await self._json("PUT", f"/authenticity/attachments/{attachment_id}", json=completed)

    async def _poll_terminal(self, attachment_id: str) -> dict:
        waited = 0.0
        while waited <= settings.POLL_TIMEOUT_SECONDS:
            att = await self.get_status(attachment_id)
            if att.get("status") in _TERMINAL:
                return att
            await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)
            waited += settings.POLL_INTERVAL_SECONDS
        raise OnmintApiError(f"timed out waiting for {attachment_id} to finish")

    # --------------------------------------------------------------- reads
    async def get_status(self, attachment_id: str) -> dict:
        return await self._json("GET", f"/authenticity/attachments/{attachment_id}")

    async def get_provenance(self, watermark_id: str) -> dict:
        return await self._json("GET", f"/authenticity/provenance/{watermark_id}")

    async def get_provenance_by_hash(self, sha256: str) -> dict:
        return await self._json("GET", f"/authenticity/provenance/by-hash/{sha256}")

    # ------------------------------------------------------------ verify tool
    async def verify(self, file_bytes: bytes, filename: str) -> dict:
        files = {"file": (filename, file_bytes, _mime(filename))}
        return await self._json("POST", "/authenticity/verify", files=files)

    async def analyze(self, file_bytes: bytes, filename: str) -> dict:
        files = {"file": (filename, file_bytes, _mime(filename))}
        return await self._json("POST", "/authenticity/analyze", files=files)

    # ---------------------------------------------------- credentialed file
    async def fetch_ipfs(self, cid: str) -> bytes:
        """Fetch bytes from the public IPFS gateway: a bare CID, or `<dir cid>/<name>`.

        Which CID holds the credentialed file is the caller's decision (see server._submit):
        an embedded-manifest file is raw-pinned at `c2pa_manifest_cid`, while `ipfs_cid` is
        the pre-signing deliverable wrapped in a directory, whose bare CID is a listing.
        """
        url = ipfs_url(cid)
        async with self._client() as c:
            resp = await c.get(url)
        if resp.status_code >= 400:
            raise OnmintApiError(f"IPFS fetch {cid} -> {resp.status_code}")
        return resp.content

    # ------------------------------------------------------------ mintys jobs
    async def submit_mintys_job(self,
                                file_bytes: bytes,
                                filename: str,
                                ai_declaration: Optional[str] = None,
                                auto_label: bool = False,
                                visible_label: bool = False,
                                label_position: Optional[str] = None,
                                label_variant: Optional[str] = None,
                                label_template: Optional[str] = None,
                                title: Optional[str] = None,
                                description: Optional[str] = None) -> dict:
        """POST /mintys/jobs: one image or one zip, one set of answers for every entry.

        `ai_declaration` and `auto_label` are two answers to one question and exactly one is
        required; checked here for a clearer error than the API's 400, which stays the
        authority. Optional fields go on the form only when given — above all
        `label_template`, where an absent field is what means "the organization's default".
        Returns the 202 body: job_id, accepted, total, credits_reserved.
        """
        if auto_label and ai_declaration is not None:
            raise OnmintApiError("send ai_declaration OR auto_label=true, not both")
        if not auto_label and ai_declaration is None:
            raise OnmintApiError("ai_declaration is required unless auto_label=true")
        if ai_declaration is not None and ai_declaration not in AI_DECLARATIONS:
            raise OnmintApiError(
                f"ai_declaration must be one of {list(AI_DECLARATIONS)}, got {ai_declaration!r}")

        form = {"auto_label": _flag(auto_label), "visible_label": _flag(visible_label)}
        optional = {"ai_disclosure": ai_declaration, "label_position": label_position,
                    "label_variant": label_variant, "label_template": label_template,
                    "title": title, "description": description}
        form.update({k: str(v) for k, v in optional.items() if v is not None})
        files = {"file": (filename, file_bytes, _mime(filename))}
        return await self._json("POST", "/mintys/jobs", data=form, files=files)

    async def get_mintys_job(self, job_id: str) -> dict:
        return await self._json("GET", f"/mintys/jobs/{job_id}")

    async def wait_mintys_job(self, job_id: str) -> dict:
        """Poll until the job has finished: FAILED, or DONE with its result ready to download.

        A job is DONE even when some entries failed (the rows say which); FAILED means none
        succeeded. DONE is not enough on its own because the result may still be assembling.
        """
        waited = 0.0
        while waited <= settings.POLL_TIMEOUT_SECONDS:
            job = await self.get_mintys_job(job_id)
            status = job.get("status")
            if status == "FAILED" or (status == "DONE" and job.get("result_available")):
                return job
            await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)
            waited += settings.POLL_INTERVAL_SECONDS
        raise OnmintApiError(f"timed out waiting for mintys job {job_id} to finish")

    async def get_mintys_job_result(self, job_id: str) -> tuple[bytes, str, str]:
        """The labelled output: (bytes, filename, content type). 409 while the job is still
        running, 410 once the temporary result has expired."""
        path = f"/mintys/jobs/{job_id}/result"
        async with self._client() as c:
            resp = await c.get(f"{self._base}{path}", headers=self._headers)
        _raise_for("GET", path, resp)
        filename = _disposition_filename(resp.headers.get("content-disposition"))
        content_type = resp.headers.get("content-type", "application/octet-stream")
        return resp.content, filename or f"mintys-job-{job_id}.zip", content_type

    async def delete_mintys_job(self, job_id: str) -> None:
        await self._json("DELETE", f"/mintys/jobs/{job_id}")

    # ------------------------------------------------------------ label templates
    async def list_label_templates(self) -> dict:
        """The organization's LABEL templates (how the visible AI label looks), reduced to what
        a caller needs to choose one: id, name, and which one is the default.

        Unrelated to `list_templates`, which lists ASSET templates for provisioning.
        """
        raw = await self._json("GET", "/mintys/label-templates")
        items = raw.get("content", []) if isinstance(raw, dict) else (raw or [])
        templates = [{"id": t.get("id"), "name": t.get("name"),
                      "is_default": bool(t.get("is_default"))} for t in items]
        default = next((t["id"] for t in templates if t["is_default"]), None)
        return {"templates": templates, "default_id": default}

    # -------------------------------------------- provisioning (templates/vaults/streams)
    async def list_templates(self, page: int = 1, page_size: int = 20,
                             public: bool = False, search_term: str = "") -> Optional[dict]:
        return await self._json("GET", "/authenticity/templates", params={
            "page": page, "page_size": page_size,
            "public": str(public).lower(), "search_term": search_term})

    async def create_template(self, name: str, hint: str = "authenticity",
                             required_streams: Optional[list] = None, public: bool = False) -> dict:
        body = {"name": name, "hint": hint,
                "required_streams": required_streams or ["default"], "public": public}
        return await self._json("POST", "/authenticity/templates", json=body)

    async def list_vaults(self, page: int = 1, page_size: int = 20, archived: bool = False) -> Optional[dict]:
        return await self._json("GET", "/authenticity/vaults", params={
            "page": page, "page_size": page_size, "archived": str(archived).lower()})

    async def get_vault_streams(self, vault_id: str, page: int = 1, page_size: int = 20) -> Optional[dict]:
        return await self._json("GET", f"/authenticity/vaults/{vault_id}/streams",
                                params={"page": page, "page_size": page_size})

    async def create_vault(self, template_id: str, name: str,
                          ledger: Optional[str] = None, description: Optional[str] = None) -> dict:
        body = {"template_id": template_id, "name": name, "ledger": ledger or settings.DEFAULT_LEDGER}
        if description is not None:
            body["description"] = description
        return await self._json("POST", "/authenticity/vaults", json=body)

    async def create_stream(self, vault_id: str, mapping: str,
                           description: Optional[str] = None, required: bool = False) -> dict:
        body = {"mapping": mapping, "description": description, "required": required}
        return await self._json("POST", f"/authenticity/vaults/{vault_id}/streams", json=body)

    async def ensure_stream(self) -> str:
        """Return a stream id to submit into with zero prior setup:
        1. ONMINT_DEFAULT_STREAM_ID if configured;
        2. else the first stream of the first existing vault;
        3. else provision a headless template -> vault -> stream.
        """
        if settings.DEFAULT_STREAM_ID:
            return settings.DEFAULT_STREAM_ID

        vaults = await self.list_vaults()
        for vault in (vaults or {}).get("content", []):
            vid = vault.get("id")
            if not vid:
                continue
            streams = await self.get_vault_streams(vid)
            for stream in (streams or {}).get("content", []):
                if stream.get("id"):
                    return stream["id"]

        # Nothing to reuse — provision a fresh chain. (Note: in some environments API vault
        # creation can be slow to settle; set ONMINT_DEFAULT_STREAM_ID to skip this.)
        tpl = await self.create_template(name="MCP Authenticity")
        template_id = _id_of(tpl)
        vault = await self.create_vault(template_id=template_id, name="MCP Authenticity Vault")
        vault_id = _id_of(vault)
        stream = await self.create_stream(vault_id=vault_id, mapping="default", required=False)
        stream_id = _id_of(stream)
        if not stream_id:
            raise OnmintApiError("could not provision a stream to submit into")
        return stream_id


def _id_of(obj: Optional[dict]) -> str:
    """Extract an id from an Add*Response (flat id, or nested under its entity key)."""
    if not obj:
        raise OnmintApiError("empty provisioning response")
    if obj.get("id"):
        return obj["id"]
    for key in ("template", "vault", "stream"):
        nested = obj.get(key) or {}
        if nested.get("id"):
            return nested["id"]
    raise OnmintApiError(f"no id in provisioning response: {list(obj)[:6]}")


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _disposition_filename(header: Optional[str]) -> Optional[str]:
    """The filename of a Content-Disposition header (plain `filename`, or `filename*` alone)."""
    if not header:
        return None
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["content-disposition"] = header
    return msg.get_filename()


def ipfs_url(cid_path: str) -> str:
    """Public gateway URL for a bare CID or a `<dir cid>/<name>` path."""
    return f"{settings.IPFS_GATEWAY}/ipfs/{cid_path}"


def zip_of_one(file_bytes: bytes, filename: str) -> bytes:
    """Wrap one file in a single-entry zip, the upload shape the watermark ingest reads.

    A file whose name already says .zip is sent as it is: it already is what the ingest
    unzips, and every file in it is then validated and billed on its own, exactly as a
    webapp archive upload is. Anything else becomes one entry named after the file's
    basename, since the ingest takes the type from the entry's extension and its bytes.

    Stored, not deflated: every supported media format is already compressed, so deflate
    only spends CPU (and, on a 100 MB video, noticeable time) to save nothing.

    TODO(streaming): this builds the whole archive in memory next to the decoded upload.
    Fine at the per-file caps; the streaming refactor should write it straight to the PUT.
    """
    if filename.lower().endswith(".zip"):
        return file_bytes
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(os.path.basename(filename) or filename, file_bytes)
    return buf.getvalue()


# The canonical media types of the main pipeline, keyed by extension, ahead of `mimetypes`.
# `mimetypes` reads the host's tables, and python:3.12-slim (the hosted image) has no
# /etc/mime.types: there .flac and .m4a come back as None (sent as application/octet-stream),
# .wav as audio/x-wav and .avi as video/avi or nothing, depending on the Python build. The
# API resolves the type from the part's content type first, so a host-dependent guess meant
# the same file verified differently on a laptop and on the hosted server. The JPEG, PNG,
# WebP, TIFF, GIF, PDF and zip values are the ones `mimetypes` already produced, so the
# mintys upload, which also goes through `_mime`, sends what it sent before; a .heic/.heif
# there now says image/heic|heif instead of the host's guess, which mintys only reads as a
# hint (it sniffs the bytes at intake and keeps its own HEIC->JPEG transcode).
_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".gif": "image/gif", ".svg": "image/svg+xml",
    ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac", ".wav": "audio/wav",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".pdf": "application/pdf", ".zip": "application/zip",
}


def _mime(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    return _EXT_MIME.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def is_audio_video(mime: Optional[str]) -> bool:
    return bool(mime) and mime.split("/", 1)[0] in ("audio", "video")


# ISO-BMFF major brands (bytes 8..12 of an `ftyp` box) -> extension. Only brands whose
# answer is certain are named, and an unknown brand is silence (None: the caller is asked
# for `filename`), never a guess. It used to be the other way round: every brand not listed
# fell through to `.mp4`, and M4B/M4P were named `.m4a`. The ingest refuses Canon's CR3 raw
# (`crx `), 3GP/3G2 phone clips, iTunes M4V video and M4B/M4P audiobooks by EXTENSION only
# (_CAMERA_RAW_EXTENSIONS / _UNSOLD_EXTENSIONS in service-watermark ingest_validation), and
# its own sniff either says nothing about those bytes (`crx `) or calls them MP4/M4A, so a
# name made up here was the declared type and it stood: a nameless CR3 was accepted,
# credentialed, anchored and billed as a VIDEO/MP4. So the unsold brands get their OWN
# extensions, which the ingest then refuses by name exactly as it would the caller's file,
# and `.mp4` is given only for the MP4 brands of the ingest's _ISOBMFF_MEDIA_BRANDS.
# `mif1`/`msf1`/`miaf` are the generic HEIF brands and are decided from the compatible
# brands instead (see sniff_extension): an AVIF is commonly `mif1` + `avif`.
_FTYP_BRANDS = {
    b"qt  ": ".mov",
    b"heic": ".heic", b"heix": ".heic", b"heim": ".heic", b"heis": ".heic",
    b"hevc": ".heic", b"hevx": ".heic",
    b"avif": ".avif", b"avis": ".avif",
    b"M4A ": ".m4a",
    # Not sold: named so the ingest refuses them by extension (see above).
    b"M4B ": ".m4b", b"M4P ": ".m4p", b"M4V ": ".m4v",
    **{brand: ".mp4" for brand in (b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41",
                                   b"mp42", b"mp71", b"avc1", b"dash", b"mmp4")},
}
# 3GPP (`3gp4`, `3gp5`, `3gp6`, `3ge6`, ...) and 3GPP2 (`3g2a`, ...) brands, matched by
# prefix since the version digit varies. Not sold; named so the ingest refuses them.
_FTYP_BRAND_PREFIXES = ((b"3g2", ".3g2"), (b"3gp", ".3gp"), (b"3ge", ".3gp"),
                        (b"3gg", ".3gp"), (b"3gr", ".3gp"), (b"3gs", ".3gp"))
_FTYP_GENERIC_IMAGE_BRANDS = (b"mif1", b"msf1", b"miaf")
# Top-level QuickTime atoms an older .mov can start with instead of `ftyp`.
_QT_LEADING_ATOMS = (b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot")


def sniff_extension(data: bytes) -> Optional[str]:
    """The extension the leading bytes say this file has, or None when they say nothing.

    Used only when a caller sends base64 with no `filename`, which used to be named
    `upload.png` whatever it held: a WAV, FLAC, AVI, SVG or ftyp-less MOV then reached the
    API as image/png (the ingest's own sniff does not recognise those and so never overrode
    the extension), and verify handed an MP4 to the C2PA reader as a PNG and reported no
    credentials. Deliberately a short list of unambiguous signatures: a wrong guess here is
    worse than asking the caller for the name, which is what None leads to.
    """
    head = data[:4096]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    # Classic and BigTIFF, both byte orders. A DNG/NEF/CR2/ARW is TIFF-based and lands here
    # too; the ingest recognises and refuses those from the bytes, whatever the name says.
    if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return ".tif"
    if head.startswith(b"%PDF-"):
        return ".pdf"
    if head.startswith(b"fLaC"):
        return ".flac"
    if head.startswith(b"RIFF") and len(head) >= 12:
        return {b"WAVE": ".wav", b"AVI ": ".avi", b"WEBP": ".webp"}.get(head[8:12])
    if len(head) >= 12 and head[4:8] == b"ftyp":
        major = head[8:12]
        if major in _FTYP_BRANDS:
            return _FTYP_BRANDS[major]
        if major in _FTYP_GENERIC_IMAGE_BRANDS:
            box_len = int.from_bytes(head[0:4], "big")
            compatible = head[16:min(box_len, len(head))]
            brands = {compatible[i:i + 4] for i in range(0, len(compatible) - 3, 4)}
            if brands & {b"avif", b"avis"}:
                return ".avif"
            if brands & {b"heic", b"heix", b"heim", b"heis"}:
                return ".heic"
            return ".heif"
        for prefix, ext in _FTYP_BRAND_PREFIXES:
            if major.startswith(prefix):
                return ext
        # Anything else (`crx ` = Canon CR3 raw, `f4v `, a vendor brand): say nothing
        # rather than guess a sold format for it.
        return None
    if len(head) >= 8 and head[4:8] in _QT_LEADING_ATOMS:
        return ".mov"
    if head.startswith(b"ID3"):
        return ".mp3"
    # An MPEG audio frame header: the 11-bit sync, a real layer (bits 1-2 of the second byte
    # non-zero; ADTS AAC shares the sync but has layer 00, and JPEG was matched above), a
    # defined version (not the reserved 01), not FF FE / FF FF (the UTF-16LE BOM, and fill
    # bytes: both are technically MPEG-1 Layer I, which nobody ships, and a UTF-16 SVG or
    # XML must not become upload.mp3), and a third byte whose bitrate index is not 1111 and
    # sample-rate index is not 11, the "bad" values no real frame carries.
    if (len(head) >= 3 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0 and (head[1] & 0x06)
            and head[1] not in (0xFE, 0xFF) and (head[1] & 0x18) != 0x08
            and (head[2] >> 4) != 0xF and ((head[2] >> 2) & 3) != 3):
        return ".mp3"
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        # A UTF-16 text file. An SVG saved that way is named .svg, so the ingest's own SVG
        # check decides whether it takes that encoding and says so, instead of an mp3 guess.
        try:
            head = head[:len(head) & ~1].decode("utf-16").encode("utf-8", "ignore")
        except UnicodeDecodeError:
            return None
    text = head.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    if text.startswith((b"<svg", b"<?xml", b"<!--", b"<!doctype svg")) and b"<svg" in text:
        return ".svg"
    return None
