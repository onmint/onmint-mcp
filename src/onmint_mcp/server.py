"""
on:mint authenticity MCP server.

Exposes the authenticity API as MCP tools so AI tools and developers can, from any MCP
client: submit content with an AI declaration, explicitly attach an AI-Act label to
AI-generated output, protect an original, and verify/analyze any image.

Design note: the AI label is DECLARED, not detected. Every submit tool sends an
`ai_declaration` and that declaration is what gets signed into the C2PA manifest. The AI
detector still runs server-side, and its reading is reported alongside the declaration as a
secondary "automated assessment" that never overrides it.

`submit_content` therefore REQUIRES the declaration from its caller. `label_ai_output` and
`protect_original` hardcode one — that is what those two tools ARE, exactly as they already
hardcoded the old `declared_ai` boolean.
"""
import base64
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from onmint_mcp import http_auth, settings
from onmint_mcp.client import OnmintClient

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


def _load(image_path: Optional[str], image_base64: Optional[str], filename: Optional[str]):
    """Resolve image bytes from a local path or a base64 string (exactly one required)."""
    _reject_local_path("image_path", image_path)
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
        "watermark_id": first.get("watermark_id"),
        "content_class": content_class,
        "mode": mode,
        "c2pa_manifest_cid": first.get("c2pa_manifest_cid"),
        "sha256": first.get("hash"),
    }


async def _submit(stream_id, image_path, image_base64, filename, name, title, category,
                  ai_declaration, wait, return_file=False, save_to=None,
                  visible_ai_label=False, allow_ai_training_and_mining=False,
                  label_template=None) -> dict:
    data, fname = _load(image_path, image_base64, filename)
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
    """Submit an image for authenticity processing: invisible watermark + signed C2PA Content
    Credentials + on-chain anchor.

    `ai_declaration` is REQUIRED and has no default — it is the authoritative AI label and it
    is signed into the credentials, so ASK THE USER rather than inferring it from the file.
    One of CREATED_WITHOUT_AI, AI_ENHANCED, AI_MODIFIED, AI_GENERATED. Submitting without it
    is rejected by the API and costs nothing.

    `visible_ai_label` burns the visible AI label into the pixels; it is only accepted for
    AI_MODIFIED / AI_GENERATED. `allow_ai_training_and_mining` (default false = refuse) writes
    the standard c2pa.training-mining assertion.

    An AI detector still runs and is reported alongside the declaration as a secondary
    automated assessment; it never overrides what was declared. `stream_id` is optional — if
    omitted, a stream is reused/provisioned automatically. Set `return_file` (or `save_to`) to
    get the credentialed file back. Returns the final status, provenance, and verify URLs.

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

    Declares AI_GENERATED by default — that is what this tool is for, exactly as it used to
    hardcode declared_ai=true. Override `ai_declaration` with AI_MODIFIED if AI changed an
    existing asset rather than generating it from nothing; the other two values are not
    appropriate here and `protect_original` is the tool for them.

    The declaration is signed into the C2PA manifest as an IPTC digitalSourceType and encoded
    in the watermark. Set `visible_ai_label=true` to also burn the visible label into the
    pixels. `stream_id` is optional (auto-provisioned). By default returns the labeled file
    bytes (base64) plus provenance and a public verify URL; pass save_to to also write it out.

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

    Declares CREATED_WITHOUT_AI by default — that is what this tool is for, exactly as it used
    to hardcode declared_ai=false. Override with AI_ENHANCED if the asset was retouched,
    upscaled or denoised with AI: the content is still what was captured, and it is still
    protected rather than labelled, but the declaration should say so. Only use this tool if
    the user has confirmed it; do not assume a file is AI-free because it looks like a photo.

    Our detector still runs and is reported as a secondary automated assessment. It does NOT
    override the declaration any more — if it disagrees, both readings are published and the
    disagreement is visible, which is the information a reviewer needs.

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
    """Verify ANY image, even one never uploaded to us. Cascades exact-hash -> watermark
    decode -> pHash similarity and validates the C2PA manifest. `match_method=none` means the
    image is unknown to on:mint. Returns match method, confidence, provenance, and C2PA state."""
    data, fname = _load(image_path, image_base64, filename)
    return await _client().verify(file_bytes=data, filename=fname)


@mcp.tool()
async def analyze_image(image_path: Optional[str] = None,
                        image_base64: Optional[str] = None,
                        filename: Optional[str] = None) -> dict:
    """Report the AI signals an image carries: `ai_signal_assessment` gives one of three tiers
    (no / isolated / clear AI signals detected), plus the per-signal breakdown (faces, NSFW,
    EXIF/ELA manipulation). Works on any image.

    The old `ai_content_share` percentage has been REMOVED — it was read as "this share of the
    image is AI", which is not what the detector measures. The raw score is still available
    under `ai_signal_assessment.score` and `ai_generated_probability` for a technical view.
    Present all of it as an automated assessment, never as a verdict about the asset: the
    asset's AI label is its rights holder's declaration, not a detector's opinion."""
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
