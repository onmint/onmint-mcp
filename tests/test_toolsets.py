"""ONMINT_MCP_TOOLSETS decides which tool groups the server advertises.

Run in a subprocess: the setting is read at import time, so an in-process test would see
whatever the first import saw.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ONMINT_TOOLS = [
    "submit_content", "label_ai_output", "protect_original", "verify_image", "analyze_image",
    "get_status", "get_provenance", "list_vaults", "list_streams", "list_templates",
    "create_template", "create_vault", "create_stream", "ensure_stream",
]
MINTYS_TOOLS = [
    "mintys_label_images", "get_mintys_job", "delete_mintys_job", "list_label_templates",
]

SNIPPET = """
import asyncio, json
from onmint_mcp import server
tools = asyncio.run(server.mcp.list_tools())
print(json.dumps(sorted(t.name for t in tools)))
"""


def _advertised(toolsets):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    env.pop("ONMINT_MCP_TOOLSETS", None)
    if toolsets is not None:
        env["ONMINT_MCP_TOOLSETS"] = toolsets
    out = subprocess.run([sys.executable, "-c", SNIPPET], env=env, capture_output=True,
                         text=True, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def test_default_advertises_every_tool():
    assert _advertised(None) == sorted(ONMINT_TOOLS + MINTYS_TOOLS)


def test_mintys_only_hides_the_onmint_tools():
    assert _advertised("mintys") == sorted(MINTYS_TOOLS)


def test_onmint_only_hides_the_mintys_tools():
    assert _advertised("onmint") == sorted(ONMINT_TOOLS)
