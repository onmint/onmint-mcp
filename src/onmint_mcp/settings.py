"""Configuration for the on:mint authenticity MCP server (from environment)."""
import os

# The public authenticity API (onmint-appcontroller-api, behind the /v1 ingress).
ONMINT_API_URL = os.environ.get("ONMINT_API_URL", "https://api.dev-onmint.com/v1").rstrip("/")

# API-key credentials issued by POST /register on the appcontroller-api.
ONMINT_API_KEY = os.environ.get("ONMINT_API_KEY", "")
ONMINT_API_SECRET = os.environ.get("ONMINT_API_SECRET", "")

# Watermark assets anchor on Polygon POS by default.
DEFAULT_LEDGER = os.environ.get("ONMINT_DEFAULT_LEDGER", "POLYGON_POS")

# Polling for the async submit pipeline.
POLL_INTERVAL_SECONDS = float(os.environ.get("ONMINT_POLL_INTERVAL_SECONDS", "5"))
POLL_TIMEOUT_SECONDS = float(os.environ.get("ONMINT_POLL_TIMEOUT_SECONDS", "1800"))

# Public IPFS gateway used to fetch the credentialed (watermarked + C2PA-signed) file bytes
# back, and the public app base for share/verify URLs.
IPFS_GATEWAY = os.environ.get("ONMINT_IPFS_GATEWAY", "https://ipfs.pub.dev-onmint.com").rstrip("/")
PUBLIC_APP_URL = os.environ.get("ONMINT_PUBLIC_APP_URL", "https://app.dev-onmint.com").rstrip("/")

# Optional: a stream to submit into when a tool is called without one. If unset, the client
# reuses the first existing vault/stream, else provisions a template->vault->stream.
DEFAULT_STREAM_ID = os.environ.get("ONMINT_DEFAULT_STREAM_ID", "")

# Transport: "stdio" (default, local) or "streamable-http" (hosted).
TRANSPORT = os.environ.get("ONMINT_MCP_TRANSPORT", "stdio")
