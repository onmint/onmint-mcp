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
                "id": "att1",
                "status": state.get("final_status", "FILEDGR_DATA_ATTACHMENT_COMPLETED"),
                "ai_disclosure": {"declaration": "CREATED_WITHOUT_AI"},
                "files": [state["file"]]})
        if path == "/v1/authenticity/provenance/WMK-1":
            return httpx.Response(200, json=state["prov"])
        if path == "/v1/authenticity/analyze" and method == "POST":
            state["analyze_body"] = request.content
            return httpx.Response(200, json={"ai_signal_assessment": {"tier": "none"}})
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
    file = dict(VIDEO_FILE, mimetype="application/pdf", c2pa_embedded=False, watermarked=True,
                soft_binding_supported=True, credentials_only_reason=None)
    prov = dict(VIDEO_PROV, mime="application/pdf", filename="my doc.pdf", c2pa_embedded=None,
                c2pa_manifest_cid="cidSidecar")
    state = {"file": file, "prov": prov}
    _wire(monkeypatch, state)
    result = asyncio.run(server.label_ai_output(
        image_base64=base64.b64encode(PDF).decode(), filename="my doc.pdf", stream_id="s1"))

    assert state["ipfs_paths"] == ["/ipfs/cidDir/my doc.pdf"]
    assert result["credentialed_file_name"] == "my doc.pdf"
    assert result["c2pa_sidecar_url"].endswith("/ipfs/cidSidecar")
    # A PDF is watermarked (every page); it is not a credentials-only file.
    assert result["delivery"] == "watermarked"


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


# ------------------------------------------------------------------ delivery
JPEG_ROW = {"watermark_id": "WMK-1", "mimetype": "image/jpeg", "watermarked": False,
            "soft_binding_supported": True, "credentials_only_reason": None}


def _att(file, status):
    return {"id": "att1", "status": status, "files": [file]}


@pytest.mark.parametrize("status", ["FILEDGR_RECEIVED", "FILEDGR_EMBEDDING", "FILEDGR_UPLOADED",
                                    "FAILED"])
def test_a_jpeg_without_its_watermark_yet_is_not_called_credentials_only(status):
    """`watermarked` is False until the embed comes back (and on a failed submission); that
    is not the credentials-only outcome."""
    assert server._summary(_att(JPEG_ROW, status))["delivery"] is None


def test_a_completed_jpeg_without_a_watermark_is_reported_as_missing():
    result = server._summary(_att(JPEG_ROW, "FILEDGR_DATA_ATTACHMENT_COMPLETED"))
    assert result["delivery"] == "watermark_missing"
    assert result["soft_binding_supported"] is True


def test_the_per_file_decision_routes_an_animated_webp_to_credentials_only():
    row = dict(JPEG_ROW, mimetype="image/webp", soft_binding_supported=False,
               credentials_only_reason="animated")
    assert server._summary(_att(row, "FILEDGR_EMBEDDING"))["delivery"] == "credentials_only"
    # The reason alone is enough, too.
    row = dict(row, soft_binding_supported=None)
    assert server._summary(_att(row, "FILEDGR_EMBEDDING"))["delivery"] == "credentials_only"


@pytest.mark.parametrize("mime,status,delivery", [
    ("video/mp4", "FILEDGR_EMBEDDING", "credentials_only"),
    ("image/svg+xml", "FILEDGR_DATA_ATTACHMENT_COMPLETED", "credentials_only"),
    ("image/jpeg", "FILEDGR_EMBEDDING", None),
    ("image/jpeg", "FILEDGR_DATA_ATTACHMENT_COMPLETED", "watermark_missing"),
    ("application/pdf", "FILEDGR_DATA_ATTACHMENT_COMPLETED", "watermark_missing"),
    (None, "FILEDGR_DATA_ATTACHMENT_COMPLETED", None),
])
def test_a_legacy_row_falls_back_to_the_mime_rule(mime, status, delivery):
    row = dict(JPEG_ROW, mimetype=mime, soft_binding_supported=None)
    assert server._summary(_att(row, status))["delivery"] == delivery


def test_a_legacy_row_takes_the_decision_from_the_provenance(monkeypatch):
    """A file row stored before the per-file decision: the provenance's value decides."""
    file = dict(VIDEO_FILE, mimetype="image/webp", soft_binding_supported=None,
                credentials_only_reason=None)
    state = {"file": file, "prov": dict(VIDEO_PROV, mime="image/webp")}
    _wire(monkeypatch, state)
    result = asyncio.run(server.protect_original(
        image_base64=base64.b64encode(WEBP).decode(), filename="a.webp", stream_id="s1"))
    assert result["soft_binding_supported"] is False
    assert result["delivery"] == "credentials_only"


def test_get_status_on_a_jpeg_still_processing(monkeypatch):
    state = {"uploaded": b"x", "final_status": "FILEDGR_EMBEDDING", "file": JPEG_ROW}
    _wire(monkeypatch, state)
    result = asyncio.run(server.get_status("att1"))
    assert result["watermarked"] is False
    assert result["delivery"] is None


def test_a_failed_submission_stops_the_wait(monkeypatch):
    """FAILED is terminal: waiting on it returns at once instead of timing out."""
    monkeypatch.setattr(settings, "POLL_TIMEOUT_SECONDS", 0.05)
    state = {"final_status": "FAILED", "file": JPEG_ROW, "prov": {}}
    _wire(monkeypatch, state)
    result = asyncio.run(server.protect_original(
        image_base64=base64.b64encode(JPEG).decode(), filename="a.jpg", stream_id="s1"))
    assert result["status"] == "FAILED"
    assert result["delivery"] is None


# ------------------------------------------------------------------ size limits
def test_the_submit_limit_does_not_send_the_caller_to_another_surface(monkeypatch):
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 0.001)
    monkeypatch.setattr(settings, "MAX_AV_FILE_SIZE_MB", 0.001)
    with pytest.raises(ValueError) as err:
        asyncio.run(server.protect_original(image_base64="A" * 8192, stream_id="s1"))
    message = str(err.value)
    assert "on any surface" in message and "nothing was charged" in message
    # The web app and the API enforce the same limits: no "use them instead" advice.
    assert "use the web app" not in message and "presigned" not in message


def test_the_verify_limit_points_at_the_hash_lookup(monkeypatch):
    monkeypatch.setattr(settings, "VERIFY_MAX_FILE_MB", 0.001)
    state = {}
    _wire(monkeypatch, state)
    with pytest.raises(ValueError) as err:
        asyncio.run(server.verify_image(image_base64="A" * 8192))
    message = str(err.value)
    assert "get_provenance" in message and "by-hash" in message
    assert "submitted" not in message and "charged" not in message
    assert "verify_body" not in state


def test_analyze_refuses_an_oversized_base64_before_any_request(monkeypatch):
    monkeypatch.setattr(settings, "ANALYZE_MAX_FILE_MB", 0.001)
    state = {}
    _wire(monkeypatch, state)
    # Not valid base64: refused on its length, never decoded.
    with pytest.raises(ValueError, match="over the"):
        asyncio.run(server.analyze_image(image_base64="!" * 8192))
    assert "analyze_body" not in state


def test_analyze_still_takes_a_nameless_bmp(monkeypatch):
    """The analyze endpoint reads BMP; bytes the sniffer does not know keep the old name."""
    state = {}
    _wire(monkeypatch, state)
    bmp = b"BM" + (70).to_bytes(4, "little") + b"\x00" * 64
    asyncio.run(server.analyze_image(image_base64=base64.b64encode(bmp).decode()))
    assert b'filename="upload.png"' in state["analyze_body"]


def test_the_hosted_server_caps_uploads_below_the_pipeline(monkeypatch):
    monkeypatch.setattr(settings, "HOSTED", True)
    monkeypatch.setattr(settings, "HOSTED_MAX_UPLOAD_MB", 0.001)
    state = {}
    _wire(monkeypatch, state)
    with pytest.raises(ValueError) as err:
        asyncio.run(server.protect_original(
            image_base64=base64.b64encode(PNG + b"\x00" * 4096).decode(), filename="a.png",
            stream_id="s1"))
    assert "HOSTED" in str(err.value) and "presigned" in str(err.value)
    with pytest.raises(ValueError, match="HOSTED"):
        asyncio.run(server.verify_image(
            image_base64=base64.b64encode(PNG + b"\x00" * 4096).decode(), filename="a.png"))
    assert "create_body" not in state and "verify_body" not in state
    # Over stdio the same file is under the pipeline cap and goes through.
    monkeypatch.setattr(settings, "HOSTED", False)
    state.update(file=dict(VIDEO_FILE, mimetype="image/png"), prov=dict(VIDEO_PROV))
    asyncio.run(server.protect_original(
        image_base64=base64.b64encode(PNG + b"\x00" * 4096).decode(), filename="a.png",
        stream_id="s1"))
    assert state["create_body"]["filename"] == "a.png"


# ------------------------------------------------------------------ sniffing edge cases
@pytest.mark.parametrize("data", [
    b"\xff\xfe<\x00s\x00v\x00g\x00",   # UTF-16LE text
    b"\xff\xff\xff\xff" + b"\x00" * 16,  # fill bytes
    b"\xff\xfb\xf0\x00" + b"\x00" * 16,  # bitrate index 1111
    b"\xff\xfb\x9c\x00" + b"\x00" * 16,  # sample-rate index 11
    b"\xff\xeb\x90\x64" + b"\x00" * 16,  # reserved MPEG version 01
])
def test_not_an_mpeg_frame(data):
    assert sniff_extension(data) != ".mp3"


def test_a_utf16_svg_is_an_svg():
    svg = "<svg xmlns='http://www.w3.org/2000/svg'/>"
    assert sniff_extension(b"\xff\xfe" + svg.encode("utf-16-le")) == ".svg"
    assert sniff_extension(b"\xfe\xff" + svg.encode("utf-16-be")) == ".svg"
