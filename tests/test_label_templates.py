"""
`list_label_templates` and the guard against confusing it with `list_templates`.

The two tools list unrelated things — label templates (how the visible AI label looks) and
asset templates (the template -> vault -> stream provisioning graph) — and a calling model
chooses between them by name and docstring alone. These tests pin both.
"""
import asyncio
import os

os.environ.setdefault("ONMINT_API_URL", "https://api.test/v1")
os.environ.setdefault("ONMINT_API_KEY", "k")
os.environ.setdefault("ONMINT_API_SECRET", "s")

import httpx

from onmint_mcp import server, settings
from onmint_mcp.client import OnmintClient

# The pinned public-API contract: GET /mintys/label-templates -> id, name, is_default.
LABEL_TEMPLATES = {"content": [
    {"id": "t-standard", "name": "Standard", "is_default": False, "frame": "none"},
    {"id": "t-brand", "name": "Brand dark", "is_default": True, "color": "#101010"},
]}


def _client(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/v1/mintys/label-templates":
            return httpx.Response(200, json=LABEL_TEMPLATES)
        return httpx.Response(404, json={"detail": "unhandled"})
    return OnmintClient(base_url=settings.ONMINT_API_URL, api_key="k", api_secret="s",
                        transport=httpx.MockTransport(handler))


def test_list_label_templates_returns_id_name_and_the_default(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_client", lambda: _client(seen))

    result = asyncio.run(server.list_label_templates())

    assert seen == [("GET", "/v1/mintys/label-templates")]
    assert result == {
        "templates": [
            {"id": "t-standard", "name": "Standard", "is_default": False},
            {"id": "t-brand", "name": "Brand dark", "is_default": True},
        ],
        "default_id": "t-brand",
    }


def _descriptions():
    tools = asyncio.run(server.mcp.list_tools())
    return {t.name: t.description or "" for t in tools}


def test_both_template_tools_exist_and_say_which_is_which():
    """Renaming either tool, or letting their docstrings drift into describing the same
    thing, would let a model pick the wrong one silently."""
    tools = _descriptions()
    assert "list_templates" in tools and "list_label_templates" in tools

    label, asset = tools["list_label_templates"], tools["list_templates"]

    # Each names its own thing...
    assert "LABEL templates" in label and "is_default" in label and "label_template" in label
    assert "ASSET templates" in asset and "vault" in asset
    # ...does not describe the other's...
    assert "vault" not in label
    assert "is_default" not in asset
    # ...and points at the other tool by name.
    assert "list_templates" in label
    assert "list_label_templates" in asset


def test_every_tool_taking_label_template_states_the_rules():
    """The docstring is the calling model's only instruction: omitted means the default,
    where to find an id, and that an unknown id is refused rather than substituted."""
    tools = asyncio.run(server.mcp.list_tools())
    taking = [t for t in tools if "label_template" in (t.inputSchema.get("properties") or {})]
    assert {t.name for t in taking} >= {"submit_content", "label_ai_output", "protect_original",
                                       "mintys_label_images"}
    for t in taking:
        d = " ".join((t.description or "").split())
        assert "Omit it to use the organization's default" in d, t.name
        assert "list_label_templates" in d, t.name
        assert "refused" in d and "substitute" in d, t.name
