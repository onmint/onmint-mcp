"""
Integration tests for the HOSTED (streamable-http) transport.

These run the real thing rather than poking at internals: a subprocess serving
`onmint-mcp` over HTTP, a stub on:mint API in-process, and genuine MCP clients speaking the
streamable-http protocol. That is deliberate — the properties under test (which credential
reaches the API, whether the listener is reachable off loopback) are properties of the
assembled process, and every one of them has a plausible-looking unit test that would pass
while the deployed server was broken.

The one that matters most is `test_credentials_do_not_leak_between_callers`: one process now
serves every tenant, so a bug that lets caller B's tool call run with caller A's API key is
not a glitch, it is content signed and credits spent under the wrong identity.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return
        except Exception as ex:  # not up yet
            last = ex
        time.sleep(0.2)
    raise RuntimeError(f"{url} never became ready: {last}")


@pytest.fixture(scope="module")
def stub_api():
    """A stand-in for the on:mint API that reports back which credential it was called with."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    import uvicorn

    async def vaults(request):
        return JSONResponse({"content": [{
            "seen_api_key": request.headers.get("x-api-key"),
            "seen_api_secret": request.headers.get("x-api-secret"),
        }]})

    async def health(_request):
        return JSONResponse({"ok": True})

    port = _free_port()
    app = Starlette(routes=[
        Route("/v1/authenticity/vaults", vaults),
        Route("/stub-health", health),
    ])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for(f"http://127.0.0.1:{port}/stub-health")
    yield f"http://127.0.0.1:{port}/v1"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def hosted_server(stub_api):
    """The MCP server itself, hosted, in a subprocess — exactly as the container runs it."""
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "ONMINT_MCP_TRANSPORT": "streamable-http",
        "ONMINT_MCP_PORT": str(port),
        "ONMINT_API_URL": stub_api,
        # Set on purpose: nothing may fall back to these. A hosted server that borrowed a
        # process-wide credential would serve every caller as one tenant, and the assertions
        # below would still pass if they only checked "a key arrived".
        "ONMINT_API_KEY": "SERVER-WIDE-KEY-MUST-NOT-BE-USED",
        "ONMINT_API_SECRET": "SERVER-WIDE-SECRET-MUST-NOT-BE-USED",
    }
    proc = subprocess.Popen([sys.executable, "-m", "onmint_mcp"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_for(f"http://127.0.0.1:{port}/health/live")
        yield f"http://127.0.0.1:{port}", port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def _call_tool(url: str, tool: str, arguments: dict, headers: dict):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(f"{url}/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, arguments)


def _payload(result) -> dict:
    """The tool's structured return value, whichever shape this SDK version reports it in."""
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_health_endpoints_answer_without_credentials(hosted_server):
    """The kubelet has no API key and does not speak MCP; the probes must still answer."""
    url, _ = hosted_server
    for path in ("/health/live", "/health/ready"):
        resp = httpx.get(f"{url}{path}", timeout=5.0)
        assert resp.status_code == 200, path


def _listen_addresses(port: int) -> list:
    """The local addresses the OS says something is LISTENING on for `port`.

    Asked of the kernel rather than probed with a connection. Probing looks simpler but is
    not sound here: the obvious "connect from a non-loopback address" version picks the host
    IP off the default route, which on any machine with a VPN up is a point-to-point utun
    address that does not loop back to a local wildcard listener — so it reports a failure
    against a perfectly correct bind. Returns [] when the platform cannot be inspected.
    """
    proc_net = Path("/proc/net/tcp")
    if proc_net.exists():  # Linux, including the CI runner
        addresses = []
        for path in (proc_net, Path("/proc/net/tcp6")):
            if not path.exists():
                continue
            for line in path.read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) < 4 or fields[3] != "0A":  # 0A = TCP_LISTEN
                    continue
                hex_addr, _, hex_port = fields[1].partition(":")
                if int(hex_port, 16) == port:
                    addresses.append(hex_addr)
        return addresses

    lsof = shutil.which("lsof")  # macOS
    if lsof:
        out = subprocess.run([lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True).stdout
        return [line.split()[-2] for line in out.splitlines()[1:] if len(line.split()) >= 2]
    return []


def test_listener_is_bound_to_the_wildcard_not_loopback(hosted_server):
    """FastMCP defaults to 127.0.0.1, which in a container answers nobody but itself.

    The kubelet's probes and the Service both dial the pod IP, so a server bound to loopback
    fails every probe and never receives traffic — while looking perfectly healthy to anyone
    who curls it from inside the container.

    Made worse by the fact that the obvious fix does not work: FastMCP hands its constructor
    defaults to pydantic-settings as init arguments, which outrank the environment, so
    setting FASTMCP_HOST=0.0.0.0 in the ConfigMap changes nothing at all. The host must be
    passed to the constructor, and this test is what says so out loud.
    """
    _, port = hosted_server
    addresses = _listen_addresses(port)
    if not addresses:
        pytest.skip("cannot inspect listening sockets on this platform")
    # Linux reports the wildcard as an all-zero hex address, lsof as `*:port`.
    wildcard = [a for a in addresses if set(a) <= {"0"} or a.startswith("*:")]
    assert wildcard, f"server is not listening on the wildcard address: {addresses}"


@pytest.mark.anyio
async def test_caller_credentials_reach_the_api(hosted_server):
    url, _ = hosted_server
    result = await _call_tool(url, "list_vaults", {}, headers={
        "x-api-key": "caller-key", "x-api-secret": "caller-secret"})
    seen = _payload(result)["content"][0]
    assert seen["seen_api_key"] == "caller-key"
    assert seen["seen_api_secret"] == "caller-secret"
    assert "SERVER-WIDE" not in (seen["seen_api_key"] or "")


@pytest.mark.anyio
async def test_credentials_do_not_leak_between_callers(hosted_server):
    """Two tenants, one process: each tool call must run as the caller that made it."""
    url, _ = hosted_server
    first = _payload(await _call_tool(url, "list_vaults", {}, headers={
        "x-api-key": "tenant-a", "x-api-secret": "secret-a"}))["content"][0]
    second = _payload(await _call_tool(url, "list_vaults", {}, headers={
        "x-api-key": "tenant-b", "x-api-secret": "secret-b"}))["content"][0]
    assert first["seen_api_key"] == "tenant-a"
    assert second["seen_api_key"] == "tenant-b"


@pytest.mark.anyio
async def test_uncredentialed_call_fails_closed(hosted_server):
    """No headers must mean no access — never a silent fallback to the server's own key."""
    url, _ = hosted_server
    result = await _call_tool(url, "list_vaults", {}, headers={})
    assert result.isError
    text = result.content[0].text
    assert "x-api-key" in text


@pytest.mark.anyio
async def test_tools_are_discoverable_without_credentials(hosted_server):
    """A client must be able to connect and list tools before it is configured with a key."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url, _ = hosted_server
    async with streamablehttp_client(f"{url}/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
    assert {"submit_content", "protect_original", "label_ai_output"} <= names


@pytest.mark.anyio
async def test_local_filesystem_arguments_are_refused_when_hosted(hosted_server):
    """`image_path`/`save_to` mean the SERVER's disk here — they must not be honoured."""
    url, _ = hosted_server
    result = await _call_tool(url, "protect_original", {"image_path": "/etc/hostname"},
                              headers={"x-api-key": "k", "x-api-secret": "s"})
    assert result.isError
    assert "not available on the hosted" in result.content[0].text


@pytest.fixture
def anyio_backend():
    return "asyncio"
