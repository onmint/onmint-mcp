"""
Tests for the on:mint MCP client + server tools, driven entirely by an httpx.MockTransport so
no live backend is needed. Covers the submit->upload->poll flow, verify/analyze, ensure_stream
(reuse and provision), and the credentialed-file return from label_ai_output.
"""
import asyncio
import base64
import os

os.environ.setdefault("ONMINT_API_URL", "https://api.test/v1")
os.environ.setdefault("ONMINT_API_KEY", "k")
os.environ.setdefault("ONMINT_API_SECRET", "s")
os.environ["ONMINT_POLL_INTERVAL_SECONDS"] = "0.01"
os.environ["ONMINT_POLL_TIMEOUT_SECONDS"] = "5"

import httpx

from onmint_mcp import settings
from onmint_mcp.client import OnmintClient
from onmint_mcp import server


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
            return httpx.Response(200, json={"id": "att1"})
        if path == "/v1/authenticity/attachments/att1" and method == "GET":
            if not state.get("uploaded"):
                return httpx.Response(200, json={
                    "id": "att1", "status": "FILEDGR_RECEIVED",
                    "presigned_urls": [{"part": 1, "link": "https://s3.test/put"}]})
            return httpx.Response(200, json={
                "id": "att1", "status": "FILEDGR_DATA_ATTACHMENT_COMPLETED", "declared_ai": True,
                "files": [{"watermark_id": "WMK-1", "content_class": "AI_GENERATED",
                           "c2pa_manifest_cid": "cidC", "hash": "abc123"}]})
        if path == "/v1/authenticity/provenance/WMK-1" and method == "GET":
            return httpx.Response(200, json={
                "watermark_id": "WMK-1", "ipfs_cid": "cidC", "mode": "labeled",
                "content_class": "AI_GENERATED"})
        if path == "/v1/authenticity/verify" and method == "POST":
            return httpx.Response(200, json={"match_method": "watermark", "confidence": 0.9})
        if path == "/v1/authenticity/analyze" and method == "POST":
            return httpx.Response(200, json={"ai_generated_probability": 0.8, "ai_content_share": 80})

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
        stream_id="s1", file_bytes=b"img", filename="a.png", declared_ai=True))
    assert att["status"] == "FILEDGR_DATA_ATTACHMENT_COMPLETED"
    assert att["files"][0]["watermark_id"] == "WMK-1"


def test_verify_and_analyze():
    assert asyncio.run(client({}).verify(b"x", "a.png"))["match_method"] == "watermark"
    assert asyncio.run(client({}).analyze(b"x", "a.png"))["ai_content_share"] == 80


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
    result = asyncio.run(server.submit_content(image_base64=img_b64, filename="x.png"))
    assert result["watermark_id"] == "WMK-1"
