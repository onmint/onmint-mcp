"""
Async HTTP client for the on:mint authenticity API (onmint-appcontroller-api /authenticity).

Encapsulates the full developer flow so the MCP tools stay thin:
  - submit_and_wait: create -> fetch presigned URL(s) -> upload (single/multipart) -> poll
  - verify / analyze: multipart forwards
  - get_status / get_provenance: reads

All requests carry the API-key headers. The optional `transport` argument lets tests inject
an httpx.MockTransport so the flow can be exercised without a live backend.
"""
import asyncio
import mimetypes
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
_TERMINAL = {"FILEDGR_DATA_ATTACHMENT_COMPLETED", "ERROR"}


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

        create_body = {
            "name": name or filename,
            "stream_id": stream_id,
            "ledger": ledger or settings.DEFAULT_LEDGER,
            "filename": filename,
            "estimated_size": len(file_bytes),
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
        await self._upload(attachment_id, attachment, file_bytes)

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
        """Fetch the credentialed (watermarked + C2PA-signed) file bytes from the IPFS gateway."""
        url = f"{settings.IPFS_GATEWAY}/ipfs/{cid}"
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


def _mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"
