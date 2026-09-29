"""
on:mint authenticity MCP server.

Exposes the authenticity API as MCP tools so AI tools and developers can, from any MCP
client: submit content (images, audio, video or PDF) with an AI declaration, explicitly
attach an AI-Act label to AI-generated output, protect an original, verify any supported
file and analyze an image. mintys customers label an image or a zip batch through the
mintys job pipeline (`mintys_label_images`), which stays images (+PDF) only.

What a submission gets depends on the format (the tiers are decided by the pipeline, not
here; see SKILL.md):
  - JPEG/PNG/WebP/TIFF: invisible watermark + embedded C2PA + AI check + on-chain anchor.
    An animated WebP/APNG or a multi-page / high-bit-depth TIFF is detected from its bytes
    and handled like the credentials-only tier instead.
  - PDF: invisible watermark on every page + AI check (page 1) + a detached C2PA `.c2pa`
    sidecar (the PDF itself carries no embedded manifest).
  - SVG, GIF, HEIC/HEIF/AVIF, MP3/M4A/FLAC/WAV, MP4/MOV/AVI: CREDENTIALS ONLY. A C2PA
    manifest embedded in the original format, no watermark (so verify matches only the exact
    file), and the AI check recorded as not assessed.

Design note: the AI label is DECLARED, not detected. Every submit tool sends an
`ai_declaration` and that declaration is what gets signed into the C2PA manifest. Where the
format has an AI check, the detector's reading is reported alongside the declaration as a
secondary "automated assessment" that never overrides it.

`submit_content` therefore REQUIRES the declaration from its caller. `label_ai_output` and
`protect_original` hardcode one — that is what those two tools ARE, exactly as they already
hardcoded the old `declared_ai` boolean.
"""
import base64
import os
from typing import Optional
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

from onmint_mcp import http_auth, settings
from onmint_mcp.client import (OnmintClient, _mime, ipfs_url, is_audio_video,
                               sniff_extension)

# host/port/stateless are passed here rather than left to the environment: FastMCP forwards
# its own constructor defaults into pydantic-settings as init arguments, which outrank env
# vars, so FASTMCP_HOST and friends are silently ignored. stateless_http is what makes
# per-request credentials safe — see the http_auth module docstring.
mcp = FastMCP(
    "onmint-authenticity",
    host=settings.SERVER_HOST,
    port=settings.SERVER_PORT,
    streamable_http_path=settings.STREAMABLE_HTTP_PATH,
    stateless_http=True,
)


def _client() -> OnmintClient:
    """Build a client for whoever is calling.

    Hosted, that is the API key on the in-flight HTTP request and never a server-wide
    credential. Over stdio the process belongs to one user, so the environment is the
    caller's own configuration and OnmintClient's defaults apply.
    """
    if settings.HOSTED:
        api_key, api_secret = http_auth.require_credentials()
        return OnmintClient(api_key=api_key, api_secret=api_secret)
    return OnmintClient()


def _reject_local_path(argument: str, value: Optional[str]) -> None:
    """Refuse a filesystem argument when 'the filesystem' is the server's, not the caller's."""
    if value and settings.HOSTED:
        raise ValueError(
            f"`{argument}` refers to a local file path and is not available on the hosted "
            "on:mint MCP server — the path would be read from (or written to) the server's "
            "own disk, not yours. Send the bytes as `image_base64` instead, and take the "
            "credentialed file back with `return_file=true`."
        )


_MB = 1024 * 1024

# What to do instead, per tool, when a file is over the limit. The pipeline caps mirrored in
# settings are the platform's own, so the web app and the REST API refuse the same file: the
# submit hint must NOT send the caller there. Only the hosted cap is specific to this server.
_SUBMIT_TOO_LARGE = (
    "That is the platform's per-file limit (audio/video {av:g} MB, every other file {other:g} "
    "MB), enforced the same way by the web app and the API, so the file cannot be submitted "
    "on any surface as it is. Nothing was submitted and nothing was charged.")
_VERIFY_TOO_LARGE = (
    "Nothing was uploaded. A file this large can still be looked up by its SHA-256 with "
    "`get_provenance(sha256=...)` (GET /authenticity/provenance/by-hash/{sha256}), which "
    "matches both the original and the credentialed file.")
_ANALYZE_TOO_LARGE = "That is the analyze endpoint's upload limit. Nothing was uploaded."
_HOSTED_TOO_LARGE = (
    "That is the limit of the HOSTED MCP server only: it carries the file inline as base64 "
    "inside one JSON-RPC message, several copies of it in a pod shared with other callers. "
    "Nothing was uploaded or charged. Files up to the platform's own per-file limit "
    "(audio/video {av:g} MB, every other file {other:g} MB) go through a local (stdio) install "
    "of this MCP server with `image_path`, the web app, or the REST API directly "
    "(POST /authenticity/attachments, then PUT to the presigned upload URLs; POST "
    "/authenticity/verify to verify).")


def _fmt(hint: str) -> str:
    return hint.replace("{av:g}", f"{settings.MAX_AV_FILE_SIZE_MB:g}").replace(
        "{other:g}", f"{settings.MAX_FILE_SIZE_MB:g}")


def _too_large(size: int, cap_mb: float, what: str, hint: str) -> None:
    if cap_mb and size > cap_mb * _MB:
        raise ValueError(
            f"{what} is {size / _MB:.1f} MB, over the {cap_mb:g} MB limit. {_fmt(hint)}")


def _cap(cap_mb: float, hint: str) -> tuple:
    """The effective cap and its hint: the pipeline's, or the hosted server's when that is
    lower. Hosted, a max-size call costs the base64 string, the decoded bytes and the
    zip-of-one at once (about 670 MB for a 200 MB image), so two concurrent ones would
    exceed the pod's 1Gi limit; the hosted cap keeps a call well inside it."""
    hosted = settings.HOSTED_MAX_UPLOAD_MB
    if settings.HOSTED and hosted and (not cap_mb or hosted < cap_mb):
        return hosted, _HOSTED_TOO_LARGE
    return cap_mb, hint


def _load(image_path: Optional[str], image_base64: Optional[str], filename: Optional[str],
          *, sniff: bool = True, max_mb: Optional[float] = None, too_large_hint: str = "",
          fallback_name: Optional[str] = None):
    """Resolve file bytes from a local path or a base64 string (exactly one required).

    With base64 and no `filename`, the name comes from the bytes (`sniff_extension`), and a
    file they say nothing about is refused with a request for `filename`. It used to be
    named `upload.png` whatever it held, which sent audio, video and SVG to the API as a
    PNG. `sniff=False` keeps that old default and is for the mintys tool ONLY: mintys sniffs
    the bytes itself at intake and treats the name as a hint, and its behaviour is not
    changed by the main pipeline's formats.

    `max_mb` is checked BEFORE the bytes are read or decoded (from the file's size, or from
    the base64 length), so an oversized upload never costs a decoded copy in this process;
    `too_large_hint` is the tool's own advice for that case. `fallback_name` names nameless
    bytes the sniffer does not recognise instead of refusing them (analyze_image, whose
    endpoint also takes types the sniffer does not know).
    """
    _reject_local_path("image_path", image_path)
    if image_path:
        if max_mb:
            _too_large(os.path.getsize(image_path), max_mb, "The file", too_large_hint)
        with open(image_path, "rb") as f:
            return f.read(), filename or os.path.basename(image_path)
    if image_base64:
        if max_mb:
            _too_large(len(image_base64) * 3 // 4, max_mb, "The file", too_large_hint)
        data = base64.b64decode(image_base64)
        if filename:
            return data, filename
        if not sniff:
            return data, "upload.png"
        ext = sniff_extension(data)
        if ext is None and fallback_name:
            return data, fallback_name
        if ext is None:
            raise ValueError(
                "Could not tell the file type from its bytes. Pass `filename` with the real "
                "extension (e.g. clip.mp4, song.flac, logo.svg) along with image_base64.")
        return data, f"upload{ext}"
    raise ValueError("Provide either image_path (local file) or image_base64.")


def _check_pipeline_cap(data: bytes, fname: str) -> None:
    """The main pipeline's per-file cap for this file's type: audio/video, or anything else
    (lowered to the hosted cap on the hosted server)."""
    if is_audio_video(_mime(fname)):
        cap_mb, what = settings.MAX_AV_FILE_SIZE_MB, "This audio/video file"
    else:
        cap_mb, what = settings.MAX_FILE_SIZE_MB, "The file"
    cap_mb, hint = _cap(cap_mb, _SUBMIT_TOO_LARGE)
    _too_large(len(data), cap_mb, what, hint)


# The warning tokens that mean the AI check did not examine the file. A copy of the
# `NOT_ASSESSED_PREFIXES` in filedgr-pkg-datastructures (enums/vetting.py), which this
# package deliberately does not depend on (see AI_DECLARATIONS in client.py); prefixes,
# matched with startswith, colon-free.
_NOT_ASSESSED_PREFIXES = (
    "ai_check_not_applicable", "ai_check_not_run", "unsupported_mime", "decode_failed",
    "pdf_page_render_failed", "pdf_ai_check_disabled", "stub_verdict_not_yet_assessed",
)


def _ai_check_assessed(verdict) -> Optional[bool]:
    """Whether the AI check actually assessed the file, by the watermark service's own rule
    (WatermarkingService.ai_check_ran). None when there is no verdict to read, e.g. before
    processing finished; that is "unknown", not "not assessed"."""
    if not isinstance(verdict, dict):
        return None
    warnings = [str(w) for w in (verdict.get("warnings") or [])]
    if any(w.startswith(_NOT_ASSESSED_PREFIXES) for w in warnings):
        return False
    return (verdict.get("modules") or {}).get("ai_generated_probability") is not None


# The types a perceptual watermark is offered for, by the mime alone: the watermark
# service's _WATERMARKABLE_MIMES (file_service.py). Used ONLY for rows stored before the
# per-file decision (`soft_binding_supported`) was recorded; since then an animated WebP/APNG
# or a multi-page / high-bit-depth TIFF of one of these types goes credentials-only too, and
# only the stored per-file outcome says so.
_WATERMARK_EXPECTED_MIMES = frozenset({
    "image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/webp", "image/tiff",
    "application/pdf",
})
_COMPLETED = "FILEDGR_DATA_ATTACHMENT_COMPLETED"


def _watermark_expected(file: dict, soft_binding: Optional[bool] = None) -> Optional[bool]:
    """Whether this file was routed to get a watermark: the stored per-file decision first,
    the mime rule for a legacy row that has none, None when neither is known."""
    if soft_binding is None:
        soft_binding = file.get("soft_binding_supported")
    if soft_binding is False or file.get("credentials_only_reason"):
        return False
    if soft_binding is True:
        return True
    mime = (file.get("mimetype") or "").lower().split(";")[0].strip()
    if not mime:
        return None
    return mime in _WATERMARK_EXPECTED_MIMES


def _delivery(file: dict, status: Optional[str],
              soft_binding: Optional[bool] = None) -> Optional[str]:
    """What the file was delivered as, from the ROUTING decision, not from the embed result.

    `watermarked` is a plain bool that defaults to False, so it is False both on a JPEG still
    being processed and on a JPEG whose paid embed failed. Reading "no watermark" as
    "credentials only" would present a pending file, or a defect, as the product working as
    designed. So:
      - "credentials_only": the file was routed to credentials only (per-file decision, or
        the mime rule for a legacy row). The expected outcome, not a failure.
      - "watermarked": a watermark was embedded.
      - "watermark_missing": a watermark was expected, the submission COMPLETED, and there is
        none. A defect worth reporting.
      - None: not known yet (still processing), the submission failed (`status` says so),
        or the service reported nothing to decide from.
    """
    if file.get("watermarked"):
        return "watermarked"
    expected = _watermark_expected(file, soft_binding)
    if expected is False:
        return "credentials_only"
    if expected and status == _COMPLETED:
        return "watermark_missing"
    return None


def _summary(attachment: dict) -> dict:
    """Compact, model-friendly view of a finished submission."""
    files = attachment.get("files") or []
    first = files[0] if files else {}
    content_class = first.get("content_class")
    mode = None
    if content_class:
        mode = "labeled" if content_class in ("AI_EDITED", "AI_GENERATED") else "protected"
    disclosure = attachment.get("ai_disclosure") or {}
    return {
        "attachment_id": attachment.get("id"),
        "status": attachment.get("status"),
        # The authoritative label: what the rights holder declared and we signed. NOT_DECLARED
        # on an asset minted before declarations were required — that is the honest record for
        # those, and it must never be reported as "created without AI".
        "ai_declaration": disclosure.get("declaration"),
        "visible_ai_label": disclosure.get("visible_ai_label"),
        "declared_ai": attachment.get("declared_ai"),
        # Present on every file, including the ones that carry NO watermark: it is the
        # asset's id, not evidence of an embedded mark. `watermarked` is that evidence.
        "watermark_id": first.get("watermark_id"),
        # Declaration-driven, like the public provenance `mode`: "protected" is the
        # declared-mode vocabulary, not a claim that a watermark was embedded.
        "content_class": content_class,
        "mode": mode,
        "mimetype": first.get("mimetype"),
        # Whether a perceptual watermark was actually embedded. False until the embed has
        # come back, so on its own it cannot tell a pending or failed embed from a file that
        # never gets one; `delivery` below makes that distinction. JPEG/PNG/WebP/TIFF and
        # PDF are offered a watermark, and not every one of those (an animated WebP or APNG,
        # or a multi-page / high-bit-depth TIFF, goes credentials-only, decided per file at
        # ingest). `soft_binding_supported` False (with `credentials_only_reason`) says it was
        # never expected; None means a row stored before that was recorded.
        "watermarked": first.get("watermarked"),
        "watermark_algo": first.get("watermark_algo"),
        "soft_binding_supported": first.get("soft_binding_supported"),
        "credentials_only_reason": first.get("credentials_only_reason"),
        "delivery": _delivery(first, attachment.get("status")),
        # False for every credentials-only file (no detector for the type) and whenever
        # the check declined; the declaration above is then the only AI statement.
        "ai_check_assessed": _ai_check_assessed(first.get("vetting_verdict")),
        "c2pa_manifest_cid": first.get("c2pa_manifest_cid"),
        "c2pa_embedded": first.get("c2pa_embedded"),
        # `sha256` is the pre-sign hash that was anchored; `signed_sha256` is the hash of
        # the credentialed file a customer downloads (None for a PDF sidecar). Verify
        # matches either.
        "sha256": first.get("hash"),
        "signed_sha256": first.get("signed_sha256"),
    }


def _credentialed_name(filename: str, embedded: bool) -> str:
    """Same naming as the webapp's credentialedFilename: `<base>.credentialed.<ext>` for a
    file with its manifest embedded, the name unchanged for the PDF deliverable."""
    if not embedded:
        return filename
    base, ext = os.path.splitext(filename)
    return f"{base}.credentialed{ext}"


async def _submit(stream_id, image_path, image_base64, filename, name, title, category,
                  ai_declaration, wait, return_file=False, save_to=None,
                  visible_ai_label=False, allow_ai_training_and_mining=False,
                  label_template=None) -> dict:
    # The larger of the two pipeline caps before decoding (the hosted cap when that is
    # lower); the exact per-type cap after, once the name (possibly sniffed) says which type
    # this is.
    cap_mb, hint = _cap(max(settings.MAX_FILE_SIZE_MB, settings.MAX_AV_FILE_SIZE_MB),
                        _SUBMIT_TOO_LARGE)
    data, fname = _load(image_path, image_base64, filename, max_mb=cap_mb,
                        too_large_hint=hint)
    _check_pipeline_cap(data, fname)
    _reject_local_path("save_to", save_to)
    client = _client()
    if not stream_id:
        stream_id = await client.ensure_stream()
    attachment = await client.submit_and_wait(
        stream_id=stream_id, file_bytes=data, filename=fname, name=name,
        ai_declaration=ai_declaration, visible_ai_label=visible_ai_label,
        allow_ai_training_and_mining=allow_ai_training_and_mining,
        title=title, category=category, wait=wait, label_template=label_template,
    )
    result = _summary(attachment)
    first_file = (attachment.get("files") or [None])[0]
    wmid = result.get("watermark_id")
    # Enrich with the public provenance ("nutrition label") + canonical URLs, and — when
    # requested — the credentialed (C2PA-signed, and for JPEG/PNG/WebP/TIFF and PDF also
    # watermarked) file fetched from IPFS.
    if wmid:
        try:
            prov = await client.get_provenance(wmid) or {}
            result["provenance"] = prov
            result["provenance_url"] = f"{settings.ONMINT_API_URL}/authenticity/provenance/{wmid}"
            result["verify_url"] = f"{settings.PUBLIC_APP_URL}/prove/{wmid}"
            if "soft_binding_supported" in prov:
                result["soft_binding_supported"] = prov["soft_binding_supported"]
                # A legacy file row carries no per-file decision; the provenance does.
                if first_file is not None and first_file.get("soft_binding_supported") is None:
                    result["delivery"] = _delivery(first_file, attachment.get("status"),
                                                   prov["soft_binding_supported"])
            if (return_file or save_to):
                await _attach_credentialed_file(client, result, prov, fname, data_len=len(data),
                                                return_file=return_file, save_to=save_to)
        except Exception as ex:  # provenance/file enrichment is best-effort
            result["provenance_error"] = str(ex)
    return result


async def _attach_credentialed_file(client: OnmintClient, result: dict, prov: dict,
                                    fname: str, data_len: int, return_file: bool,
                                    save_to: Optional[str]) -> None:
    """Find the credentialed file on IPFS and hand it back (URL always, bytes when small).

    NOT `ipfs_cid`: that is the pre-signing deliverable, pinned wrapped in a directory, so
    its bare CID is a directory listing and even the file inside it carries no manifest.
    Every format except PDF has its manifest EMBEDDED, and that file is raw-pinned at
    `c2pa_manifest_cid` (the bare CID is the file). For a PDF, `c2pa_manifest_cid` is the
    detached `.c2pa` sidecar, so the deliverable is `<ipfs_cid>/<filename>` and the sidecar
    is reported next to it. `c2pa_embedded` says which; rows signed before it was recorded
    leave it None and fall back to the mime.
    """
    mime = prov.get("mime") or _mime(fname)
    name = os.path.basename(prov.get("filename") or fname)
    manifest_cid = prov.get("c2pa_manifest_cid")
    embedded = prov.get("c2pa_embedded")
    if embedded is None:
        embedded = bool(manifest_cid) and mime != "application/pdf"
    if embedded and manifest_cid:
        cid_path = manifest_cid
    elif prov.get("ipfs_cid"):
        cid_path = f"{prov['ipfs_cid']}/{quote(name)}"
        if manifest_cid:
            result["c2pa_sidecar_url"] = ipfs_url(manifest_cid)
    else:
        return
    result["credentialed_file_url"] = ipfs_url(cid_path)
    result["credentialed_file_name"] = _credentialed_name(name, bool(embedded and manifest_cid))

    # Audio and video are never inlined: base64 of a 100 MB video inside a JSON-RPC reply is
    # three more copies of it in this process (fetched, encoded, serialised), shared with
    # every other caller of a hosted pod. The public URL is the same file.
    inline = return_file and not is_audio_video(mime) and \
        data_len <= settings.MAX_INLINE_RETURN_MB * _MB
    if return_file and not inline:
        result["credentialed_file_inline"] = False
        result["credentialed_file_note"] = (
            "Not returned inline (audio/video, or over "
            f"{settings.MAX_INLINE_RETURN_MB:g} MB); download it from credentialed_file_url.")
    if not (inline or save_to):
        return
    # TODO(streaming): fetched whole into memory; stream to save_to once the pipeline streams.
    file_bytes = await client.fetch_ipfs(cid_path)
    if save_to:
        with open(save_to, "wb") as fh:
            fh.write(file_bytes)
        result["saved_to"] = save_to
    if inline:
        result["credentialed_file_base64"] = base64.b64encode(file_bytes).decode()


@mcp.tool()
async def submit_content(ai_declaration: str,
                         stream_id: Optional[str] = None,
                         image_path: Optional[str] = None,
                         image_base64: Optional[str] = None,
                         filename: Optional[str] = None,
                         name: Optional[str] = None,
                         title: Optional[str] = None,
                         category: Optional[str] = None,
                         visible_ai_label: bool = False,
                         allow_ai_training_and_mining: bool = False,
                         wait: bool = True,
                         return_file: bool = False,
                         save_to: Optional[str] = None,
                         label_template: Optional[str] = None) -> dict:
    """Submit a file for authenticity processing: signed C2PA Content Credentials + on-chain
    anchor, plus an invisible watermark for the formats that take one.

    Accepts images, audio, video and PDF through `image_path` / `image_base64` (the names
    are kept for compatibility). With `image_base64`, pass `filename` with the real
    extension; without it the type is read from the bytes, and unrecognised bytes are
    refused. Per file: audio/video up to 100 MB, everything else up to 200 MB.
      - JPEG, PNG, WebP, TIFF: invisible watermark + embedded C2PA + AI check.
      - PDF: invisible watermark on every page + AI check (page 1) + a detached C2PA
        `.c2pa` sidecar.
      - SVG, GIF, HEIC, HEIF, AVIF, MP3, M4A, FLAC, WAV, MP4, MOV, AVI (and an animated
        WebP/APNG or a multi-page / high-bit-depth TIFF): credentials only. The C2PA
        manifest is embedded in the original format, NO watermark is embedded
        (`soft_binding_supported=false`, `delivery="credentials_only"`), and the AI check
        is recorded as not assessed (`ai_check_assessed=false`).
    `delivery="watermark_missing"` means a watermark was expected and is not there: a
    defect to report, not the credentials-only outcome.

    `ai_declaration` is REQUIRED and has no default — it is the authoritative AI label and it
    is signed into the credentials, so ASK THE USER rather than inferring it from the file.
    One of CREATED_WITHOUT_AI, AI_ENHANCED, AI_MODIFIED, AI_GENERATED. Submitting without it
    is rejected by the API and costs nothing.

    `visible_ai_label` burns the visible AI label into the pixels; it is only accepted for
    AI_MODIFIED / AI_GENERATED, and only applies to JPEG/PNG/WebP/TIFF.
    `allow_ai_training_and_mining` (default false = refuse) writes the standard
    c2pa.training-mining assertion.

    For JPEG/PNG/WebP/TIFF and PDF an AI detector runs and is reported alongside the
    declaration as a
    secondary automated assessment; it never overrides what was declared. `stream_id` is
    optional — if omitted, a stream is reused/provisioned automatically. Set `return_file` (or
    `save_to`) to get the credentialed file back: `credentialed_file_url` always, and the
    bytes as base64 too except for audio/video and large files. Returns the final status,
    provenance, and verify URLs.

    `label_template`: id of one of the organization's label templates; it sets how the
    visible AI label LOOKS (artwork, frame, colour, logo), never what it says, and only shows
    when a visible label is drawn (`visible_ai_label=true`). Omit it to use
    the organization's default. Call `list_label_templates` to find an id. An unknown id is
    refused (MINTYS_TEMPLATE_UNKNOWN) and nothing is submitted or labelled with a substitute.
    """
    return await _submit(stream_id, image_path, image_base64, filename, name, title, category,
                         ai_declaration, wait, return_file=return_file, save_to=save_to,
                         visible_ai_label=visible_ai_label,
                         allow_ai_training_and_mining=allow_ai_training_and_mining,
                         label_template=label_template)


@mcp.tool()
async def label_ai_output(image_path: Optional[str] = None,
                          image_base64: Optional[str] = None,
                          filename: Optional[str] = None,
                          stream_id: Optional[str] = None,
                          name: Optional[str] = None,
                          title: Optional[str] = None,
                          category: Optional[str] = None,
                          ai_declaration: str = "AI_GENERATED",
                          visible_ai_label: bool = False,
                          allow_ai_training_and_mining: bool = False,
                          wait: bool = True,
                          return_file: bool = True,
                          save_to: Optional[str] = None,
                          label_template: Optional[str] = None) -> dict:
    """Attach a secure AI label to AI-generated output (for AI-tool providers, EU AI Act Art.
    50) and get the credentialed file back.

    Accepts images, audio, video and PDF through `image_path` / `image_base64` (the names
    are kept for compatibility). With `image_base64`, pass `filename` with the real
    extension; without it the type is read from the bytes, and unrecognised bytes are
    refused. Per file: audio/video up to 100 MB, everything else up to 200 MB.
      - JPEG, PNG, WebP, TIFF: invisible watermark + embedded C2PA + AI check.
      - PDF: invisible watermark on every page + AI check (page 1) + a detached C2PA
        `.c2pa` sidecar.
      - SVG, GIF, HEIC, HEIF, AVIF, MP3, M4A, FLAC, WAV, MP4, MOV, AVI (and an animated
        WebP/APNG or a multi-page / high-bit-depth TIFF): credentials only. The C2PA
        manifest is embedded in the original format, NO watermark is embedded
        (`soft_binding_supported=false`, `delivery="credentials_only"`), and the AI check
        is recorded as not assessed (`ai_check_assessed=false`).
    `delivery="watermark_missing"` means a watermark was expected and is not there: a
    defect to report, not the credentials-only outcome.

    Declares AI_GENERATED by default — that is what this tool is for, exactly as it used to
    hardcode declared_ai=true. Override `ai_declaration` with AI_MODIFIED if AI changed an
    existing asset rather than generating it from nothing; the other two values are not
    appropriate here and `protect_original` is the tool for them.

    The declaration is signed into the C2PA manifest as an IPTC digitalSourceType, for every
    format, and is also encoded in the watermark for JPEG/PNG/WebP/TIFF and PDF. For the
    credentials-only formats the signed manifest is the only carrier. Set
    `visible_ai_label=true` to also burn the visible label into the pixels (JPEG/PNG/WebP/TIFF
    only). `stream_id` is optional (auto-provisioned). By default returns the labeled file
    (`credentialed_file_url`, and the bytes as base64 except for audio/video and large files)
    plus provenance and a public verify URL; pass save_to to also write it out.

    This is the on:mint pipeline (stream, IPFS, on-chain anchor). mintys customers labelling
    an image or a zip batch use `mintys_label_images` instead.

    `label_template`: id of one of the organization's label templates; it sets how the
    visible AI label LOOKS (artwork, frame, colour, logo), never what it says, and only shows
    when a visible label is drawn (`visible_ai_label=true`). Omit it to use
    the organization's default. Call `list_label_templates` to find an id. An unknown id is
    refused (MINTYS_TEMPLATE_UNKNOWN) and nothing is submitted or labelled with a substitute.
    """
    return await _submit(stream_id, image_path, image_base64, filename, name, title, category,
                         ai_declaration, wait=wait, return_file=return_file, save_to=save_to,
                         visible_ai_label=visible_ai_label,
                         allow_ai_training_and_mining=allow_ai_training_and_mining,
                         label_template=label_template)


@mcp.tool()
async def protect_original(image_path: Optional[str] = None,
                           image_base64: Optional[str] = None,
                           filename: Optional[str] = None,
                           stream_id: Optional[str] = None,
                           name: Optional[str] = None,
                           title: Optional[str] = None,
                           category: Optional[str] = None,
                           ai_declaration: str = "CREATED_WITHOUT_AI",
                           allow_ai_training_and_mining: bool = False,
                           wait: bool = True,
                           return_file: bool = False,
                           save_to: Optional[str] = None,
                           label_template: Optional[str] = None) -> dict:
    """Protect an original (authored/captured) asset.

    Accepts images, audio, video and PDF through `image_path` / `image_base64` (the names
    are kept for compatibility). With `image_base64`, pass `filename` with the real
    extension; without it the type is read from the bytes, and unrecognised bytes are
    refused. Per file: audio/video up to 100 MB, everything else up to 200 MB.
      - JPEG, PNG, WebP, TIFF: invisible watermark + embedded C2PA + AI check.
      - PDF: invisible watermark on every page + AI check (page 1) + a detached C2PA
        `.c2pa` sidecar.
      - SVG, GIF, HEIC, HEIF, AVIF, MP3, M4A, FLAC, WAV, MP4, MOV, AVI (and an animated
        WebP/APNG or a multi-page / high-bit-depth TIFF): credentials only. The C2PA
        manifest is embedded in the original format, NO watermark is embedded
        (`soft_binding_supported=false`, `delivery="credentials_only"`), and the AI check
        is recorded as not assessed (`ai_check_assessed=false`).
    `delivery="watermark_missing"` means a watermark was expected and is not there: a
    defect to report, not the credentials-only outcome.

    Declares CREATED_WITHOUT_AI by default — that is what this tool is for, exactly as it used
    to hardcode declared_ai=false. Override with AI_ENHANCED if the asset was retouched,
    upscaled or denoised with AI: the content is still what was captured, and it is still
    protected rather than labelled, but the declaration should say so. Only use this tool if
    the user has confirmed it; do not assume a file is AI-free because it looks like a photo.

    For JPEG/PNG/WebP/TIFF and PDF our detector runs and is reported as a secondary automated
    assessment. It does NOT override the declaration — if it disagrees, both readings are
    published and the disagreement is visible, which is the information a reviewer needs.
    The credentials-only formats have no detector; the declaration stands alone.

    `stream_id` is optional (auto-provisioned). Returns the final status, provenance, and
    verify URLs; set return_file/save_to to also get the credentialed file.

    `label_template` is accepted for parity but changes nothing visible here: this tool draws
    no visible label. It is still validated, so an unknown id is still refused.

    `label_template`: id of one of the organization's label templates; it sets how the
    visible AI label LOOKS (artwork, frame, colour, logo), never what it says. Omit it to use
    the organization's default. Call `list_label_templates` to find an id. An unknown id is
    refused (MINTYS_TEMPLATE_UNKNOWN) and nothing is submitted or labelled with a substitute.
    """
    return await _submit(stream_id, image_path, image_base64, filename, name, title, category,
                         ai_declaration, wait=wait, return_file=return_file, save_to=save_to,
                         allow_ai_training_and_mining=allow_ai_training_and_mining,
                         label_template=label_template)


@mcp.tool()
async def verify_image(image_path: Optional[str] = None,
                       image_base64: Optional[str] = None,
                       filename: Optional[str] = None) -> dict:
    """Verify ANY supported file (image, audio, video or PDF, up to 100 MB), even one never
    uploaded to us. Cascades exact-hash -> watermark decode -> pHash similarity and validates
    the C2PA manifest. The watermark step only exists for the watermarked formats (JPEG, PNG,
    WebP, TIFF, PDF) and pHash for the images among them; a credentials-only file (SVG, GIF,
    HEIC/HEIF/AVIF, audio, video; `soft_binding_supported` false) matches only as the exact
    file, by either its original or its credentialed hash, or by its embedded manifest.
    `match_method=none` means the file is unknown to on:mint. Returns match method,
    confidence, provenance, and C2PA state. With `image_base64`, pass `filename` with the
    real extension. A file over the limit can still be looked up by its SHA-256 with
    `get_provenance`."""
    cap_mb, hint = _cap(settings.VERIFY_MAX_FILE_MB, _VERIFY_TOO_LARGE)
    data, fname = _load(image_path, image_base64, filename, max_mb=cap_mb,
                        too_large_hint=hint)
    return await _client().verify(file_bytes=data, filename=fname)


@mcp.tool()
async def analyze_image(image_path: Optional[str] = None,
                        image_base64: Optional[str] = None,
                        filename: Optional[str] = None) -> dict:
    """Report the AI signals an image carries: `ai_signal_assessment` gives one of three tiers
    (no / isolated / clear AI signals detected), plus the per-signal breakdown (faces, NSFW,
    EXIF/ELA manipulation). Images only (JPEG/PNG/WebP/TIFF/BMP are the ones the detector
    assesses), up to 25 MB; there is no AI analysis for audio, video or SVG.

    The old `ai_content_share` percentage has been REMOVED — it was read as "this share of the
    image is AI", which is not what the detector measures. The raw score is still available
    under `ai_signal_assessment.score` and `ai_generated_probability` for a technical view.
    Present all of it as an automated assessment, never as a verdict about the asset: the
    asset's AI label is its rights holder's declaration, not a detector's opinion."""
    # Nameless bytes the sniffer does not know (a BMP, say) keep the old `upload.png` name
    # rather than being refused: the analyze endpoint reads the image itself and accepts
    # types the submit pipeline does not.
    cap_mb, hint = _cap(settings.ANALYZE_MAX_FILE_MB, _ANALYZE_TOO_LARGE)
    data, fname = _load(image_path, image_base64, filename, max_mb=cap_mb,
                        too_large_hint=hint, fallback_name="upload.png")
    return await _client().analyze(file_bytes=data, filename=fname)


@mcp.tool()
async def get_status(attachment_id: str) -> dict:
    """Fetch the current status of a submission by attachment id (for wait=false submissions).
    Once finished, per-file provenance is populated: watermark_id, content_class, and whether
    a watermark was actually embedded (`watermarked`, `delivery`) and the AI check ran
    (`ai_check_assessed`). A credentials-only file says so up front
    (`soft_binding_supported=false`, `delivery="credentials_only"`); for a file that gets a
    watermark, `delivery` stays null until the submission completes."""
    return _summary(await _client().get_status(attachment_id))


@mcp.tool()
async def get_provenance(watermark_id: Optional[str] = None,
                         sha256: Optional[str] = None) -> dict:
    """Look up an asset's public provenance ('nutrition label') by watermark id or SHA-256:
    the verdict, AI classification, C2PA/on-chain proof, and content analysis."""
    client = _client()
    if watermark_id:
        return await client.get_provenance(watermark_id)
    if sha256:
        return await client.get_provenance_by_hash(sha256)
    raise ValueError("Provide either watermark_id or sha256.")


# ============================================================ mintys jobs
# mintys is its own pipeline, not a mode of the on:mint one: no stream, no IPFS pin, no
# on-chain anchor, one declaration (or auto_label) per job, and the output comes back as a
# download that expires. Folding it into label_ai_output would silently change that tool's
# inputs and its return shape, so it is a tool of its own and each docstring names the other.
def _mintys_template(job: dict) -> dict:
    """The template applied to a job, as {id, name}, whichever shape the API reports it in."""
    nested = job.get("label_template")
    if isinstance(nested, dict):
        return {"id": nested.get("id"), "name": nested.get("name")}
    return {"id": job.get("label_template_id"), "name": job.get("label_template_name")}


def _mintys_summary(job: dict) -> dict:
    items = job.get("items") or []
    failed = [{"filename": i.get("filename"), "failure_reason": i.get("failure_reason"),
               "failure_detail": i.get("failure_detail")}
              for i in items if i.get("status") == "FAILED"]
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "total": job.get("total"),
        "done": job.get("done"),
        "skipped": job.get("skipped"),
        "failed": job.get("failed"),
        "failed_items": failed,
        "credits_spent": job.get("credits_spent"),
        "label_template": _mintys_template(job),
        "result_available": job.get("result_available"),
        "expires_at": job.get("expires_at"),
    }


async def _attach_mintys_result(client: OnmintClient, result: dict, job_id: str,
                                return_file: bool, save_to: Optional[str]) -> dict:
    if not (return_file or save_to):
        return result
    try:
        data, name, content_type = await client.get_mintys_job_result(job_id)
    except Exception as ex:  # the job itself is reported either way
        result["result_error"] = str(ex)
        return result
    if save_to:
        with open(save_to, "wb") as fh:
            fh.write(data)
        result["saved_to"] = save_to
    if return_file:
        result["result_file_base64"] = base64.b64encode(data).decode()
        result["result_file_name"] = name
        result["result_content_type"] = content_type
    return result


@mcp.tool()
async def mintys_label_images(image_path: Optional[str] = None,
                              image_base64: Optional[str] = None,
                              filename: Optional[str] = None,
                              ai_declaration: Optional[str] = None,
                              auto_label: bool = False,
                              visible_label: bool = False,
                              label_position: Optional[str] = None,
                              label_variant: Optional[str] = None,
                              label_template: Optional[str] = None,
                              title: Optional[str] = None,
                              description: Optional[str] = None,
                              wait: bool = True,
                              return_file: bool = True,
                              save_to: Optional[str] = None) -> dict:
    """mintys: label one image, or a .zip of images, through the mintys job pipeline (AI
    check, visible EU AI Act label, invisible watermark, signed C2PA credentials). For mintys
    customers. No stream, IPFS or on-chain anchor; for that on:mint pipeline use
    `label_ai_output` instead.

    Exactly one of `ai_declaration` (CREATED_WITHOUT_AI, AI_ENHANCED, AI_MODIFIED,
    AI_GENERATED; applied to EVERY file in the upload, so ask the user) or `auto_label=true`
    (the AI check decides per file; a file it clears comes back SKIPPED, which is a success).
    `visible_label=true` burns in the visible label; `label_position` is top_right (default),
    top_left, bottom_right or bottom_left; omit `label_variant` to pick it automatically.
    Sending base64, set `filename` (e.g. batch.zip) so the type is known.

    `label_template`: id of one of the organization's label templates; it sets how the
    visible label LOOKS (artwork, frame, colour, logo), never what it says, and applies to
    the whole job. Omit it to use the organization's default. Call `list_label_templates` to
    find an id. An unknown id is refused (MINTYS_TEMPLATE_UNKNOWN) and nothing is labelled
    with a substitute. The applied template's id and name are recorded on the job and
    returned as `label_template`.

    With `wait=true` (default) polls until the job finishes and returns counts, failed files,
    the applied template and, with `return_file` (default) or `save_to`, the labelled output
    (a zip for a zip upload). With `wait=false` returns the job id at once; follow up with
    `get_mintys_job`. The output is temporary: download it before `expires_at`."""
    # sniff=False: the mintys upload keeps its old naming exactly (see _load).
    data, fname = _load(image_path, image_base64, filename, sniff=False)
    _reject_local_path("save_to", save_to)
    client = _client()
    accepted = await client.submit_mintys_job(
        file_bytes=data, filename=fname, ai_declaration=ai_declaration, auto_label=auto_label,
        visible_label=visible_label, label_position=label_position,
        label_variant=label_variant, label_template=label_template, title=title,
        description=description)
    job_id = str(accepted["job_id"])
    if not wait:
        result = _mintys_summary(await client.get_mintys_job(job_id))
        result.update(accepted=accepted.get("accepted"),
                      credits_reserved=accepted.get("credits_reserved"))
        return result
    result = _mintys_summary(await client.wait_mintys_job(job_id))
    result.update(accepted=accepted.get("accepted"),
                  credits_reserved=accepted.get("credits_reserved"))
    return await _attach_mintys_result(client, result, job_id, return_file, save_to)


@mcp.tool()
async def get_mintys_job(job_id: str, return_file: bool = False,
                         save_to: Optional[str] = None) -> dict:
    """mintys: progress of a job from `mintys_label_images` (e.g. one submitted with
    wait=false): status, done/skipped/failed counts, failed files, and the label template
    applied (`label_template` {id, name}). Set `return_file` or `save_to` to also fetch the
    labelled output once `result_available` is true; before that, or after `expires_at`, the
    download is refused and reported under `result_error`."""
    _reject_local_path("save_to", save_to)
    client = _client()
    result = _mintys_summary(await client.get_mintys_job(job_id))
    return await _attach_mintys_result(client, result, job_id, return_file, save_to)


@mcp.tool()
async def delete_mintys_job(job_id: str) -> dict:
    """mintys: delete a job's temporary output now instead of waiting for `expires_at`. An
    unknown job id is an error, not a success."""
    await _client().delete_mintys_job(job_id)
    return {"job_id": job_id, "deleted": True}


# ============================================================ Label templates
# Deliberately NOT named `list_templates`: that tool already exists and lists the asset
# templates of the provisioning graph. Two tools both called "templates" is how a calling
# model picks the wrong one without noticing, so the names and the docstrings each say which.
@mcp.tool()
async def list_label_templates() -> dict:
    """List the organization's LABEL templates: how the visible AI label LOOKS (artwork,
    frame, colour, logo). Returns `templates` [{id, name, is_default}] and `default_id`.

    Pass an `id` from here as `label_template` to mintys_label_images, label_ai_output,
    submit_content or protect_original. Omitting `label_template` uses the template marked
    `is_default`; an id not in this list is refused and nothing is labelled with a
    substitute. Templates are created and edited in the web app, not through this tool.

    Not the asset templates `list_templates` returns; those are a different thing."""
    return await _client().list_label_templates()


# ============================================================ Provisioning (P4)
# Manage the template -> vault -> stream graph over the API, so a developer can set up a place
# to submit content without touching the web app.
@mcp.tool()
async def list_vaults(page: int = 1, page_size: int = 20, archived: bool = False) -> dict:
    """List your vaults (paginated). Each vault holds streams that submissions go into."""
    return await _client().list_vaults(page=page, page_size=page_size, archived=archived) or {"content": []}


@mcp.tool()
async def list_streams(vault_id: str, page: int = 1, page_size: int = 20) -> dict:
    """List the streams inside a vault. Submit content into a stream's id."""
    return await _client().get_vault_streams(vault_id, page=page, page_size=page_size) or {"content": []}


@mcp.tool()
async def list_templates(page: int = 1, page_size: int = 20,
                         public: bool = False, search_term: str = "") -> dict:
    """List ASSET templates: step 1 of the template -> vault -> stream graph that on:mint
    submissions go into (optionally public / filtered by search term).

    NOT label templates. These ids are never valid as `label_template`; for how the visible
    AI label looks, call `list_label_templates`."""
    return await _client().list_templates(page=page, page_size=page_size,
                                          public=public, search_term=search_term) or {"content": []}


@mcp.tool()
async def create_template(name: str, hint: str = "authenticity", public: bool = False) -> dict:
    """Create a headless ASSET template (step 1 of provisioning a place to submit). Returns the
    template. Not a label template: those are created in the web app, not over the API."""
    return await _client().create_template(name=name, hint=hint, public=public)


@mcp.tool()
async def create_vault(template_id: str, name: str, description: Optional[str] = None) -> dict:
    """Create a vault from a template_id (step 2 — mints on-chain). Returns the vault."""
    return await _client().create_vault(template_id=template_id, name=name, description=description)


@mcp.tool()
async def create_stream(vault_id: str, mapping: str = "default",
                        description: Optional[str] = None, required: bool = False) -> dict:
    """Create a stream inside a vault (step 3). Returns the stream; submit content into its id."""
    return await _client().create_stream(vault_id=vault_id, mapping=mapping,
                                         description=description, required=required)


@mcp.tool()
async def ensure_stream() -> dict:
    """Get a ready-to-use stream id with zero setup: reuse an existing stream, or provision a
    template -> vault -> stream. Returns {"stream_id": ...}."""
    return {"stream_id": await _client().ensure_stream()}


# ================================================================== Hosted transport
# Health endpoints for the Kubernetes probes. `custom_route` registers them outside the MCP
# protocol and outside any authorization, which is what a probe needs: the kubelet has no
# credentials and speaks HTTP, not MCP. They deliberately do no I/O — the API this server
# fronts is a dependency, not part of this process's liveness, and a probe that failed when
# the API had a bad minute would restart every pod in the middle of it.
@mcp.custom_route("/health/live", methods=["GET"])
async def health_live(_request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "alive"})


@mcp.custom_route("/health/ready", methods=["GET"])
async def health_ready(_request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ready"})


def http_app():
    """The hosted ASGI app: the streamable-http MCP endpoint plus per-caller credentials."""
    http_auth.assert_stateless(mcp)
    app = mcp.streamable_http_app()
    app.add_middleware(http_auth.CallerCredentialsMiddleware)
    return app


def main() -> None:
    if not settings.HOSTED:
        mcp.run(transport=settings.TRANSPORT)
        return

    # Served through uvicorn directly rather than mcp.run("streamable-http") so the
    # credentials middleware can be wrapped around the app before it starts.
    import uvicorn

    uvicorn.run(http_app(), host=settings.SERVER_HOST, port=settings.SERVER_PORT)


if __name__ == "__main__":
    main()
