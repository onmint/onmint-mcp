"""
on:mint authenticity MCP server.

Exposes the authenticity API as MCP tools so AI tools and developers can, from any MCP
client: submit content (the mandatory AI check decides protect-vs-label), explicitly attach
an AI-Act label to AI-generated output, protect an original, and verify/analyze any image.

Design note: the AI check ALWAYS runs server-side and its result decides the mode. A tool
like `label_ai_output` records the caller's "this is AI" claim (recommended for AI-tool
providers under the EU AI Act), but detection — not the claim — sets the final label.
"""
import base64
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from onmint_mcp import settings
from onmint_mcp.client import OnmintClient

mcp = FastMCP("onmint-authenticity")


def _client() -> OnmintClient:
    return OnmintClient()


def _load(image_path: Optional[str], image_base64: Optional[str], filename: Optional[str]):
    """Resolve image bytes from a local path or a base64 string (exactly one required)."""
    if image_path:
        with open(image_path, "rb") as f:
            return f.read(), filename or os.path.basename(image_path)
    if image_base64:
        return base64.b64decode(image_base64), filename or "upload.png"
    raise ValueError("Provide either image_path (local file) or image_base64.")


def _summary(attachment: dict) -> dict:
    """Compact, model-friendly view of a finished submission."""
    files = attachment.get("files") or []
    first = files[0] if files else {}
    content_class = first.get("content_class")
    mode = None
    if content_class:
        mode = "labeled" if content_class in ("AI_EDITED", "AI_GENERATED") else "protected"
    return {
        "attachment_id": attachment.get("id"),
        "status": attachment.get("status"),
        "declared_ai": attachment.get("declared_ai"),
        "watermark_id": first.get("watermark_id"),
        "content_class": content_class,
        "mode": mode,
        "c2pa_manifest_cid": first.get("c2pa_manifest_cid"),
        "sha256": first.get("hash"),
    }


async def _submit(stream_id, image_path, image_base64, filename, name, title, category,
                  declared_ai, wait, return_file=False, save_to=None) -> dict:
    data, fname = _load(image_path, image_base64, filename)
    client = _client()
    if not stream_id:
        stream_id = await client.ensure_stream()
    attachment = await client.submit_and_wait(
        stream_id=stream_id, file_bytes=data, filename=fname, name=name,
        declared_ai=declared_ai, title=title, category=category, wait=wait,
    )
    result = _summary(attachment)
    wmid = result.get("watermark_id")
    # Enrich with the public provenance ("nutrition label") + canonical URLs, and — when
    # requested — the credentialed (watermarked + C2PA-signed) file bytes fetched from IPFS.
    if wmid:
        try:
            prov = await client.get_provenance(wmid)
            result["provenance"] = prov
            result["provenance_url"] = f"{settings.ONMINT_API_URL}/authenticity/provenance/{wmid}"
            result["verify_url"] = f"{settings.PUBLIC_APP_URL}/prove/{wmid}"
            cid = (prov or {}).get("ipfs_cid")
            if (return_file or save_to) and cid:
                file_bytes = await client.fetch_ipfs(cid)
                if save_to:
                    with open(save_to, "wb") as fh:
                        fh.write(file_bytes)
                    result["saved_to"] = save_to
                if return_file:
                    result["credentialed_file_base64"] = base64.b64encode(file_bytes).decode()
                    result["credentialed_file_name"] = fname
        except Exception as ex:  # provenance/file enrichment is best-effort
            result["provenance_error"] = str(ex)
    return result


@mcp.tool()
async def submit_content(stream_id: Optional[str] = None,
                         image_path: Optional[str] = None,
                         image_base64: Optional[str] = None,
                         filename: Optional[str] = None,
                         name: Optional[str] = None,
                         title: Optional[str] = None,
                         category: Optional[str] = None,
                         declared_ai: Optional[bool] = None,
                         wait: bool = True,
                         return_file: bool = False,
                         save_to: Optional[str] = None) -> dict:
    """Submit an image for authenticity processing. The mandatory AI check runs first and
    its result decides the mode: an original is protected; AI-edited/AI-generated content is
    labeled per the EU AI Act. `declared_ai` optionally records your upfront claim (does not
    override detection). `stream_id` is optional — if omitted, a stream is reused/provisioned
    automatically. Set `return_file` (or `save_to`) to get the credentialed file back. Returns
    the final status, provenance, and share/verify URLs."""
    return await _submit(stream_id, image_path, image_base64, filename, name, title, category,
                         declared_ai, wait, return_file=return_file, save_to=save_to)


@mcp.tool()
async def label_ai_output(image_path: Optional[str] = None,
                          image_base64: Optional[str] = None,
                          filename: Optional[str] = None,
                          stream_id: Optional[str] = None,
                          name: Optional[str] = None,
                          title: Optional[str] = None,
                          category: Optional[str] = None,
                          wait: bool = True,
                          return_file: bool = True,
                          save_to: Optional[str] = None) -> dict:
    """Attach a secure AI label to AI-generated output (for AI-tool providers, EU AI Act Art.
    50) and get the credentialed file back. Submits with declared_ai=true; the AI check still
    runs and, for genuine AI content, emits a C2PA digitalSourceType marking + AI-tagged
    watermark. `stream_id` is optional (auto-provisioned). By default returns the labeled file
    bytes (base64) plus provenance and a public verify URL; pass save_to to also write it out."""
    return await _submit(stream_id, image_path, image_base64, filename, name, title, category,
                         declared_ai=True, wait=wait, return_file=return_file, save_to=save_to)


@mcp.tool()
async def protect_original(image_path: Optional[str] = None,
                           image_base64: Optional[str] = None,
                           filename: Optional[str] = None,
                           stream_id: Optional[str] = None,
                           name: Optional[str] = None,
                           title: Optional[str] = None,
                           category: Optional[str] = None,
                           wait: bool = True,
                           return_file: bool = False,
                           save_to: Optional[str] = None) -> dict:
    """Protect an original (authored/captured) asset: submits with declared_ai=false. The AI
    check still runs — if it detects AI content, the asset is labeled AI instead (detection
    decides). `stream_id` is optional (auto-provisioned). Returns the final status, provenance,
    and share/verify URLs; set return_file/save_to to also get the credentialed file."""
    return await _submit(stream_id, image_path, image_base64, filename, name, title, category,
                         declared_ai=False, wait=wait, return_file=return_file, save_to=save_to)


@mcp.tool()
async def verify_image(image_path: Optional[str] = None,
                       image_base64: Optional[str] = None,
                       filename: Optional[str] = None) -> dict:
    """Verify ANY image, even one never uploaded to us. Cascades exact-hash -> watermark
    decode -> pHash similarity and validates the C2PA manifest. `match_method=none` means the
    image is unknown to on:mint. Returns match method, confidence, provenance, and C2PA state."""
    data, fname = _load(image_path, image_base64, filename)
    return await _client().verify(file_bytes=data, filename=fname)


@mcp.tool()
async def analyze_image(image_path: Optional[str] = None,
                        image_base64: Optional[str] = None,
                        filename: Optional[str] = None) -> dict:
    """Report how much AI content an image holds: AI-generated probability + label, plus the
    per-signal breakdown (faces, NSFW, EXIF/ELA manipulation). Works on any image. These are
    calibrated estimates from open detectors, not ground-truth verdicts."""
    data, fname = _load(image_path, image_base64, filename)
    return await _client().analyze(file_bytes=data, filename=fname)


@mcp.tool()
async def get_status(attachment_id: str) -> dict:
    """Fetch the current status of a submission by attachment id (for wait=false submissions).
    Once finished, per-file provenance (watermark_id, content_class) is populated."""
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
    """List templates available to you (optionally public / filtered by search term)."""
    return await _client().list_templates(page=page, page_size=page_size,
                                          public=public, search_term=search_term) or {"content": []}


@mcp.tool()
async def create_template(name: str, hint: str = "authenticity", public: bool = False) -> dict:
    """Create a headless template (step 1 of provisioning a place to submit). Returns the template."""
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


def main() -> None:
    mcp.run(transport=settings.TRANSPORT)


if __name__ == "__main__":
    main()
