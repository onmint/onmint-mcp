"""
Tests for the on:mint MCP client + server tools, driven entirely by an httpx.MockTransport so
no live backend is needed. Covers the submit->upload->poll flow, verify/analyze, ensure_stream
(reuse and provision), and the credentialed-file return from label_ai_output.
"""
import asyncio
import base64
import json as json_module
import os

os.environ.setdefault("ONMINT_API_URL", "https://api.test/v1")
os.environ.setdefault("ONMINT_API_KEY", "k")
os.environ.setdefault("ONMINT_API_SECRET", "s")
os.environ["ONMINT_POLL_INTERVAL_SECONDS"] = "0.01"
os.environ["ONMINT_POLL_TIMEOUT_SECONDS"] = "5"

import httpx
import pytest

from onmint_mcp import settings
from onmint_mcp.client import LabelTemplateUnknownError, OnmintApiError, OnmintClient
from onmint_mcp import server

TEMPLATE_ID = "3f0c9a52-6d1e-4c8b-9a57-0e2f4b1d7c11"
UNKNOWN_TEMPLATE = "00000000-0000-4000-8000-000000000000"


def make_transport(state):
    def handler(request: httpx.Request) -> httpx.Response:
        method, url = request.method, request.url
        path, host = url.path, url.host

        if host.startswith("ipfs"):  # IPFS gateway
            return httpx.Response(200, content=b"CREDENTIALED-BYTES")
        if host == "s3.test" and method == "PUT":  # presigned upload
            state["uploaded"] = True
            return httpx.Response(200, headers={"ETag": '"etag1"'})

        if path == "/v1/authenticity/attachments" and method == "POST":
            # The API now requires a declaration and answers 422 without one. Mirrored here
            # so a tool that stops sending it fails in this suite rather than in production.
            body = json_module.loads(request.content or b"{}")
            state["create_body"] = body
            if not (body.get("ai_disclosure") or {}).get("declaration"):
                return httpx.Response(422, json={"detail": [
                    {"type": "missing", "loc": ["body", "ai_disclosure"],
                     "msg": "Field required"}]})
            # Mirrors the API's refusal of a template id the organization does not have:
            # 400 with a stable code, and nothing created.
            if body.get("label_template") == UNKNOWN_TEMPLATE:
                return httpx.Response(400, json={
                    "error": "MINTYS_TEMPLATE_UNKNOWN",
                    "message": "That label template does not exist."})
            return httpx.Response(200, json={"id": "att1"})
        if path == "/v1/authenticity/attachments/att1" and method == "GET":
            if not state.get("uploaded"):
                return httpx.Response(200, json={
                    "id": "att1", "status": "FILEDGR_RECEIVED",
                    "presigned_urls": [{"part": 1, "link": "https://s3.test/put"}]})
            return httpx.Response(200, json={
                "id": "att1", "status": "FILEDGR_DATA_ATTACHMENT_COMPLETED",
                "ai_disclosure": {"declaration": "AI_GENERATED", "visible_ai_label": False},
                "files": [{"watermark_id": "WMK-1", "content_class": "AI_GENERATED",
                           "c2pa_manifest_cid": "cidC", "hash": "abc123"}]})
        if path == "/v1/authenticity/provenance/WMK-1" and method == "GET":
            return httpx.Response(200, json={
                "watermark_id": "WMK-1", "ipfs_cid": "cidC", "mode": "labeled",
                "content_class": "AI_GENERATED"})
        if path == "/v1/authenticity/verify" and method == "POST":
            return httpx.Response(200, json={"match_method": "watermark", "confidence": 0.9})
        if path == "/v1/authenticity/analyze" and method == "POST":
            return httpx.Response(200, json={
                "ai_generated_probability": 0.8,
                "ai_signal_assessment": {"score": 80.0, "tier": "CLEAR"}})

        # provisioning
        if path == "/v1/authenticity/vaults" and method == "GET":
            return httpx.Response(200, json=state["vaults"])
        if path.endswith("/streams") and method == "GET":
            return httpx.Response(200, json=state["streams"])
        if path == "/v1/authenticity/templates" and method == "POST":
            return httpx.Response(200, json={"id": "tpl1"})
        if path == "/v1/authenticity/vaults" and method == "POST":
            return httpx.Response(201, json={"id": "vault1"})
        if path.endswith("/streams") and method == "POST":
            return httpx.Response(200, json={"id": "stream-new"})

        return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})

    return httpx.MockTransport(handler)


def client(state):
    return OnmintClient(base_url=settings.ONMINT_API_URL, api_key="k", api_secret="s",
                        transport=make_transport(state))


def test_submit_and_wait_full_flow():
    state = {}
    att = asyncio.run(client(state).submit_and_wait(
        stream_id="s1", file_bytes=b"img", filename="a.png", ai_declaration="AI_GENERATED"))
    assert att["status"] == "FILEDGR_DATA_ATTACHMENT_COMPLETED"
    assert att["files"][0]["watermark_id"] == "WMK-1"


def test_verify_and_analyze():
    assert asyncio.run(client({}).verify(b"x", "a.png"))["match_method"] == "watermark"
    analysis = asyncio.run(client({}).analyze(b"x", "a.png"))
    # The tier, not the removed `ai_content_share` percentage.
    assert analysis["ai_signal_assessment"]["tier"] == "CLEAR"
    assert "ai_content_share" not in analysis


def test_fetch_ipfs():
    assert asyncio.run(client({}).fetch_ipfs("cidC")) == b"CREDENTIALED-BYTES"


def test_ensure_stream_reuses_existing():
    state = {"vaults": {"total_records": 1, "current_page": 1, "total_pages": 1,
                        "content": [{"id": "v1"}]},
             "streams": {"total_records": 1, "current_page": 1, "total_pages": 1,
                         "content": [{"id": "existing-stream"}]}}
    assert asyncio.run(client(state).ensure_stream()) == "existing-stream"


def test_ensure_stream_provisions_when_empty():
    state = {"vaults": {"total_records": 0, "current_page": 1, "total_pages": 0, "content": []},
             "streams": {"total_records": 0, "current_page": 1, "total_pages": 0, "content": []}}
    assert asyncio.run(client(state).ensure_stream()) == "stream-new"


def test_default_stream_id_short_circuits(monkeypatch):
    monkeypatch.setattr(settings, "DEFAULT_STREAM_ID", "env-stream")
    assert asyncio.run(client({}).ensure_stream()) == "env-stream"


def test_label_ai_output_returns_credentialed_file(monkeypatch):
    state = {}
    monkeypatch.setattr(server, "_client", lambda: client(state))
    img_b64 = base64.b64encode(b"input").decode()
    result = asyncio.run(server.label_ai_output(image_base64=img_b64, filename="gen.png", stream_id="s1"))
    assert result["mode"] == "labeled"
    # The tool's whole purpose, still hardcoded — it just declares instead of asserting a bool.
    assert state["create_body"]["ai_disclosure"]["declaration"] == "AI_GENERATED"
    assert result["content_class"] == "AI_GENERATED"
    assert result["verify_url"].endswith("/prove/WMK-1")
    assert result["provenance_url"].endswith("/authenticity/provenance/WMK-1")
    # credentialed bytes fetched from IPFS and returned base64
    assert base64.b64decode(result["credentialed_file_base64"]) == b"CREDENTIALED-BYTES"
    assert result["credentialed_file_name"] == "gen.png"


def test_submit_content_auto_provisions_stream(monkeypatch):
    state = {"vaults": {"content": [{"id": "v1"}]},
             "streams": {"content": [{"id": "auto-stream"}]}}
    monkeypatch.setattr(server, "_client", lambda: client(state))
    img_b64 = base64.b64encode(b"input").decode()
    # no stream_id passed -> ensure_stream reuses v1/auto-stream, submit still completes
    result = asyncio.run(server.submit_content(
        ai_declaration="AI_GENERATED", image_base64=img_b64, filename="x.png"))
    assert result["watermark_id"] == "WMK-1"


# ------------------------------------------------------- the declaration is mandatory
def test_submit_content_requires_a_declaration(monkeypatch):
    """No default. The declaration is signed in the user's name, so a tool that guesses it
    is putting words in their mouth cryptographically — and the API rejects it anyway."""
    monkeypatch.setattr(server, "_client", lambda: client({}))
    img_b64 = base64.b64encode(b"input").decode()

    with pytest.raises(TypeError):
        asyncio.run(server.submit_content(image_base64=img_b64, filename="x.png"))


def test_an_unknown_declaration_is_refused_before_the_request():
    """A clearer error than the server's 422, and it costs no round trip. The API stays the
    authority — this list can only ever produce a nicer message, never a different answer."""
    with pytest.raises(OnmintApiError, match="ai_declaration must be one of"):
        asyncio.run(client({}).submit_and_wait(
            stream_id="s1", file_bytes=b"img", filename="a.png", ai_declaration="MAYBE_AI"))


def test_the_declaration_and_its_answers_are_sent_explicitly():
    """Off has to travel as an explicit false: an omitted training-mining answer reads to a
    scraper as no objection recorded, which is not what the customer said."""
    state = {}
    asyncio.run(client(state).submit_and_wait(
        stream_id="s1", file_bytes=b"img", filename="a.png", ai_declaration="AI_MODIFIED",
        visible_ai_label=True))

    disclosure = state["create_body"]["ai_disclosure"]
    assert disclosure["declaration"] == "AI_MODIFIED"
    assert disclosure["visible_ai_label"] is True
    assert disclosure["allow_ai_training_and_mining"] is False


def test_protect_original_declares_no_ai(monkeypatch):
    """The counterpart hardcode: this tool used to send declared_ai=false."""
    state = {}
    monkeypatch.setattr(server, "_client", lambda: client(state))
    img_b64 = base64.b64encode(b"input").decode()

    asyncio.run(server.protect_original(image_base64=img_b64, filename="p.png", stream_id="s1"))

    assert state["create_body"]["ai_disclosure"]["declaration"] == "CREATED_WITHOUT_AI"


def test_the_summary_reports_the_declaration_that_was_signed(monkeypatch):
    """The detector's class is still reported next to it; they are different claims and the
    summary must not collapse them into one."""
    state = {}
    monkeypatch.setattr(server, "_client", lambda: client(state))
    img_b64 = base64.b64encode(b"input").decode()

    result = asyncio.run(server.label_ai_output(
        image_base64=img_b64, filename="gen.png", stream_id="s1"))

    assert result["ai_declaration"] == "AI_GENERATED"
    assert result["content_class"] == "AI_GENERATED"


# ------------------------------------------------------------ label_template (ONMINT-856)
# Omitted must stay OMITTED on the wire: the API reads an absent field as "use the
# organization's default", and an explicit null is a different request.
_SUBMIT_TOOLS = [
    ("submit_content", {"ai_declaration": "AI_GENERATED"}),
    ("label_ai_output", {}),
    ("protect_original", {}),
]


@pytest.mark.parametrize("tool,extra", _SUBMIT_TOOLS)
def test_label_template_omitted_is_absent_from_the_body(monkeypatch, tool, extra):
    state = {}
    monkeypatch.setattr(server, "_client", lambda: client(state))
    img_b64 = base64.b64encode(b"input").decode()

    asyncio.run(getattr(server, tool)(image_base64=img_b64, filename="a.png",
                                      stream_id="s1", **extra))

    assert "label_template" not in state["create_body"]


@pytest.mark.parametrize("tool,extra", _SUBMIT_TOOLS)
def test_label_template_given_is_sent_verbatim(monkeypatch, tool, extra):
    state = {}
    monkeypatch.setattr(server, "_client", lambda: client(state))
    img_b64 = base64.b64encode(b"input").decode()

    asyncio.run(getattr(server, tool)(image_base64=img_b64, filename="a.png",
                                      stream_id="s1", label_template=TEMPLATE_ID, **extra))

    assert state["create_body"]["label_template"] == TEMPLATE_ID


def test_submit_and_wait_omits_label_template_by_default():
    state = {}
    asyncio.run(client(state).submit_and_wait(
        stream_id="s1", file_bytes=b"img", filename="a.png", ai_declaration="AI_GENERATED"))
    assert "label_template" not in state["create_body"]


def test_an_unknown_label_template_is_refused_not_substituted(monkeypatch):
    """The refusal surfaces as its own error that tells the calling model what to do, and
    nothing is uploaded — there is no fallback to the default template."""
    state = {}
    monkeypatch.setattr(server, "_client", lambda: client(state))
    img_b64 = base64.b64encode(b"input").decode()

    with pytest.raises(LabelTemplateUnknownError) as exc:
        asyncio.run(server.label_ai_output(image_base64=img_b64, filename="a.png",
                                           stream_id="s1", label_template=UNKNOWN_TEMPLATE))

    assert "list_label_templates" in str(exc.value)
    assert "substitute" in str(exc.value)
    assert not state.get("uploaded")
