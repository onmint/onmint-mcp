"""
The main-pipeline tools on every supported media type: what goes up (a zip-of-one named
after the real file), how a nameless base64 upload is named, the per-type size caps, which
IPFS object comes back as the credentialed file, and what the summary says about
credentials-only files. Driven by an httpx.MockTransport like test_client.py.
"""
import asyncio
import base64
import io
import json as json_module
import os
import zipfile

os.environ.setdefault("ONMINT_API_URL", "https://api.test/v1")
os.environ.setdefault("ONMINT_API_KEY", "k")
os.environ.setdefault("ONMINT_API_SECRET", "s")
os.environ["ONMINT_POLL_INTERVAL_SECONDS"] = "0.01"
os.environ["ONMINT_POLL_TIMEOUT_SECONDS"] = "5"

import httpx
import pytest

from onmint_mcp import server, settings
from onmint_mcp.client import OnmintClient, _mime, sniff_extension, zip_of_one

MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64
MOV = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  " + b"\x00" * 64
MOV_NO_FTYP = b"\x00\x00\x00\x08wide\x00\x00\x10\x00mdat" + b"\x00" * 64
M4A = b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00M4A mp42isom" + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
AVIF_MIF1 = b"\x00\x00\x00\x1cftypmif1\x00\x00\x00\x00mif1avifmiaf" + b"\x00" * 64
HEIF = b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1miaf" + b"\x00" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64
AVI = b"RIFF\x24\x00\x00\x00AVI LIST" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
FLAC = b"fLaC\x00\x00\x00\x22" + b"\x00" * 64
MP3_ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
MP3_SYNC = b"\xff\xfb\x90\x64" + b"\x00" * 64
ADTS_AAC = b"\xff\xf1\x50\x80" + b"\x00" * 64
SVG = b"\xef\xbb\xbf<?xml version='1.0'?>\n<svg xmlns='http://www.w3.org/2000/svg'/>"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
TIFF = b"II*\x00\x08\x00\x00\x00" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"\x00" * 64


@pytest.mark.parametrize("data,ext", [
    (MP4, ".mp4"), (MOV, ".mov"), (MOV_NO_FTYP, ".mov"), (M4A, ".m4a"), (HEIC, ".heic"),
    (AVIF_MIF1, ".avif"), (HEIF, ".heif"), (WAV, ".wav"), (AVI, ".avi"), (WEBP, ".webp"),
    (FLAC, ".flac"), (MP3_ID3, ".mp3"), (MP3_SYNC, ".mp3"), (SVG, ".svg"), (JPEG, ".jpg"),
    (PNG, ".png"), (GIF, ".gif"), (TIFF, ".tif"), (PDF, ".pdf"),
])
def test_sniff_extension(data, ext):
    assert sniff_extension(data) == ext


@pytest.mark.parametrize("data", [b"hello world", ADTS_AAC, b"<html><body/></html>", b""])
def test_sniff_says_nothing_rather_than_guess(data):
    assert sniff_extension(data) is None


@pytest.mark.parametrize("name,mime", [
    ("a.flac", "audio/flac"), ("a.m4a", "audio/mp4"), ("a.wav", "audio/wav"),
    ("a.mp3", "audio/mpeg"), ("a.MP4", "video/mp4"), ("a.mov", "video/quicktime"),
    ("a.avi", "video/x-msvideo"), ("a.heic", "image/heic"), ("a.heif", "image/heif"),
    ("a.avif", "image/avif"), ("a.svg", "image/svg+xml"), ("a.jpg", "image/jpeg"),
    ("a.png", "image/png"), ("a.pdf", "application/pdf"), ("a.zip", "application/zip"),
])
def test_mime_does_not_depend_on_the_host_tables(name, mime):
    assert _mime(name) == mime


def test_zip_of_one_names_the_entry_after_the_file():
    payload = zip_of_one(b"VIDEO", "clips/clip.mp4")
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.namelist() == ["clip.mp4"]
        assert zf.read("clip.mp4") == b"VIDEO"
        assert zf.getinfo("clip.mp4").compress_type == zipfile.ZIP_STORED


def test_a_zip_is_sent_as_it_is():
    already = zip_of_one(b"x", "a.png")
    assert zip_of_one(already, "batch.zip") is already


# ------------------------------------------------------------------ the full tool path
def make_transport(state):
    def handler(request: httpx.Request) -> httpx.Response:
        method, path, host = request.method, request.url.path, request.url.host
        if host.startswith("ipfs"):
            state.setdefault("ipfs_paths", []).append(path)
            return httpx.Response(200, content=b"CREDENTIALED:" + path.encode())
        if host == "s3.test" and method == "PUT":
            state["uploaded"] = request.content
            return httpx.Response(200, headers={"ETag": '"e"'})
        if path == "/v1/authenticity/attachments" and method == "POST":
            state["create_body"] = json_module.loads(request.content)
            return httpx.Response(200, json={"id": "att1"})
        if path == "/v1/authenticity/attachments/att1" and method == "GET":
            if state.get("uploaded") is None:
                return httpx.Response(200, json={
                    "id": "att1", "status": "FILEDGR_RECEIVED",
                    "presigned_urls": [{"part": 1, "link": "https://s3.test/put"}]})
            return httpx.Response(200, json={
                "id": "att1", "status": "FILEDGR_DATA_ATTACHMENT_COMPLETED",
                "ai_disclosure": {"declaration": "CREATED_WITHOUT_AI"},
                "files": [state["file"]]})
        if path == "/v1/authenticity/provenance/WMK-1":
            return httpx.Response(200, json=state["prov"])
        if path == "/v1/authenticity/verify" and method == "POST":
            state["verify_body"] = request.content
            return httpx.Response(200, json={"match_method": "hash"})
        return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})
    return httpx.MockTransport(handler)


def _wire(monkeypatch, state):
    monkeypatch.setattr(server, "_client", lambda: OnmintClient(
        base_url=settings.ONMINT_API_URL, api_key="k", api_secret="s",
        transport=make_transport(state)))


VIDEO_FILE = {
    "watermark_id": "WMK-1", "content_class": None, "mimetype": "video/mp4",
    "watermarked": False, "soft_binding_supported": False, "credentials_only_reason": "format",
    "c2pa_manifest_cid": "cidSigned", "c2pa_embedded": True, "hash": "pre",
    "signed_sha256": "post",
    "vetting_verdict": {"warnings": ["ai_check_not_applicable:video/mp4"], "modules": {}},
}
VIDEO_PROV = {"watermark_id": "WMK-1", "ipfs_cid": "cidDir", "c2pa_manifest_cid": "cidSigned",
              "c2pa_embedded": True, "mime": "video/mp4", "filename": "upload.mp4",
              "soft_binding_supported": False}


def test_a_nameless_base64_video_is_uploaded_as_a_zip_of_one_mp4(monkeypatch):
    state = {"file": VIDEO_FILE, "prov": VIDEO_PROV}
    _wire(monkeypatch, state)
    result = asyncio.run(server.protect_original(
        image_base64=base64.b64encode(MP4).decode(), stream_id="s1"))

    assert state["create_body"]["filename"] == "upload.mp4"
    assert state["create_body"]["estimated_size"] == len(state["uploaded"])
    with zipfile.ZipFile(io.BytesIO(state["uploaded"])) as zf:
        assert zf.namelist() == ["upload.mp4"]
        assert zf.read("upload.mp4") == MP4

    # The summary says what the file actually got.
    assert result["watermarked"] is False
    assert result["delivery"] == "credentials_only"
    assert result["soft_binding_supported"] is False
    assert result["ai_check_assessed"] is False
    assert result["signed_sha256"] == "post"


def test_unrecognised_bytes_without_a_filename_are_refused(monkeypatch):
    state = {}
    _wire(monkeypatch, state)
    with pytest.raises(ValueError, match="filename"):
        asyncio.run(server.protect_original(
            image_base64=base64.b64encode(b"just some bytes").decode(), stream_id="s1"))
    assert "create_body" not in state


def test_the_embedded_credentialed_file_is_the_manifest_cid_and_video_is_not_inlined(
        monkeypatch):
    state = {"file": VIDEO_FILE, "prov": VIDEO_PROV}
    _wire(monkeypatch, state)
    result = asyncio.run(server.label_ai_output(
        image_base64=base64.b64encode(MP4).decode(), filename="clip.mp4", stream_id="s1"))

    assert result["credentialed_file_url"].endswith("/ipfs/cidSigned")
    assert result["credentialed_file_name"] == "upload.credentialed.mp4"
    assert "credentialed_file_base64" not in result
    assert result["credentialed_file_inline"] is False
    # Nothing was fetched just to be thrown away.
    assert "ipfs_paths" not in state


def test_an_embedded_image_comes_back_inline_from_the_manifest_cid(monkeypatch):
    file = dict(VIDEO_FILE, mimetype="image/jpeg", watermarked=True,
                soft_binding_supported=True, credentials_only_reason=None,
                vetting_verdict={"warnings": [], "modules": {"ai_generated_probability": 0.1}})
    prov = dict(VIDEO_PROV, mime="image/jpeg", filename="photo.jpg",
                soft_binding_supported=True, c2pa_embedded=None)
    state = {"file": file, "prov": prov}
    _wire(monkeypatch, state)
    result = asyncio.run(server.label_ai_output(
        image_base64=base64.b64encode(JPEG).decode(), filename="photo.jpg", stream_id="s1"))

    assert state["ipfs_paths"] == ["/ipfs/cidSigned"]
    assert base64.b64decode(result["credentialed_file_base64"]) == b"CREDENTIALED:/ipfs/cidSigned"
    assert result["credentialed_file_name"] == "photo.credentialed.jpg"
    assert result["delivery"] == "watermarked"
    assert result["ai_check_assessed"] is True


def test_a_pdf_comes_back_from_inside_the_wrapped_directory(monkeypatch):
    file = dict(VIDEO_FILE, mimetype="application/pdf", c2pa_embedded=False)
    prov = dict(VIDEO_PROV, mime="application/pdf", filename="my doc.pdf", c2pa_embedded=None,
                c2pa_manifest_cid="cidSidecar")
    state = {"file": file, "prov": prov}
    _wire(monkeypatch, state)
    result = asyncio.run(server.label_ai_output(
        image_base64=base64.b64encode(PDF).decode(), filename="my doc.pdf", stream_id="s1"))

    assert state["ipfs_paths"] == ["/ipfs/cidDir/my doc.pdf"]
    assert result["credentialed_file_name"] == "my doc.pdf"
    assert result["c2pa_sidecar_url"].endswith("/ipfs/cidSidecar")


def test_audio_and_video_are_capped_lower_than_everything_else(monkeypatch):
    monkeypatch.setattr(settings, "MAX_AV_FILE_SIZE_MB", 0.001)  # ~1 KB
    state = {}
    _wire(monkeypatch, state)
    big = MP4 + b"\x00" * 4096
    with pytest.raises(ValueError, match="audio/video"):
        asyncio.run(server.protect_original(
            image_base64=base64.b64encode(big).decode(), filename="clip.mp4", stream_id="s1"))
    assert "create_body" not in state
    # The same size as an image is under the general cap and goes through.
    state.update(file=dict(VIDEO_FILE, mimetype="image/png"), prov=dict(VIDEO_PROV))
    asyncio.run(server.protect_original(
        image_base64=base64.b64encode(PNG + b"\x00" * 4096).decode(), filename="a.png",
        stream_id="s1"))
    assert state["create_body"]["filename"] == "a.png"


def test_the_cap_is_checked_before_decoding(monkeypatch):
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 0.001)
    monkeypatch.setattr(settings, "MAX_AV_FILE_SIZE_MB", 0.001)
    # Not valid base64 at all: it must be refused on its length, never decoded.
    with pytest.raises(ValueError, match="over the"):
        asyncio.run(server.protect_original(image_base64="!" * 8192, stream_id="s1"))


def test_verify_sends_a_nameless_flac_as_audio_flac(monkeypatch):
    state = {}
    _wire(monkeypatch, state)
    asyncio.run(server.verify_image(image_base64=base64.b64encode(FLAC).decode()))
    assert b'filename="upload.flac"' in state["verify_body"]
    assert b"Content-Type: audio/flac" in state["verify_body"]


def test_mintys_keeps_its_old_default_name(monkeypatch):
    """The mintys tool is out of this change: a nameless upload is still `upload.png` there
    (mintys sniffs the bytes itself), even when the bytes are something else."""
    data, name = server._load(None, base64.b64encode(MP4).decode(), None, sniff=False)
    assert name == "upload.png" and data == MP4
