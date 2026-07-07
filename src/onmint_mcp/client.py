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
_PRESIGN_READY = {"FILEDGR_RECEIVED", "FILEDGR_REVIEWED"}
_TERMINAL = {"FILEDGR_DATA_ATTACHMENT_COMPLETED", "ERROR"}


class OnmintApiError(RuntimeError):
    pass


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
        if resp.status_code >= 400:
            raise OnmintApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.content else None

    # --------------------------------------------------------------- submit
    async def submit_and_wait(self,
                              stream_id: str,
                              file_bytes: bytes,
                              filename: str,
                              name: Optional[str] = None,
                              declared_ai: Optional[bool] = None,
                              title: Optional[str] = None,
                              category: Optional[str] = None,
                              ledger: Optional[str] = None,
                              wait: bool = True) -> dict:
        """Create a submission, upload the file, and (optionally) wait for the pipeline to finish.

        Returns the final attachment dict. Per-file provenance (watermark_id, content_class,
        vetting verdict) lives under `files[]` once processing completes.
        """
        create_body = {
            "name": name or filename,
            "stream_id": stream_id,
            "ledger": ledger or settings.DEFAULT_LEDGER,
            "filename": filename,
            "estimated_size": len(file_bytes),
        }
        if declared_ai is not None:
            create_body["declared_ai"] = declared_ai
        if title is not None:
            create_body["title"] = title
        if category is not None:
            create_body["category"] = category

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


def _mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"
