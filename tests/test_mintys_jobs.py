"""
The mintys job tools, driven against a stub of the PINNED public-API contract:

    POST   /mintys/jobs                 multipart; one image or one zip
    GET    /mintys/jobs/{job_id}        progress, incl. the applied template id and name
    GET    /mintys/jobs/{job_id}/result the labelled output
    DELETE /mintys/jobs/{job_id}

The stub refuses an unknown `label_template` the way the API does (400
MINTYS_TEMPLATE_UNKNOWN, nothing created) and records every form field it receives, so
"omitted" and "present" can be told apart on the wire.
"""
import asyncio
import base64
import os
import re

os.environ.setdefault("ONMINT_API_URL", "https://api.test/v1")
os.environ.setdefault("ONMINT_API_KEY", "k")
os.environ.setdefault("ONMINT_API_SECRET", "s")
os.environ["ONMINT_POLL_INTERVAL_SECONDS"] = "0.01"

import httpx
import pytest

from onmint_mcp import server, settings
from onmint_mcp.client import LabelTemplateUnknownError, OnmintApiError, OnmintClient

JOB_ID = "8d3f7a0e-2b1c-4e5d-9f60-7a8b9c0d1e2f"
TEMPLATE_ID = "3f0c9a52-6d1e-4c8b-9a57-0e2f4b1d7c11"
DEFAULT_TEMPLATE_ID = "5b6c7d8e-0000-4000-8000-00000000d3fa"
UNKNOWN_TEMPLATE = "00000000-0000-4000-8000-000000000000"
NAMES = {TEMPLATE_ID: "Brand dark", DEFAULT_TEMPLATE_ID: "Standard"}


def _form_fields(request: httpx.Request) -> dict:
    """Every non-file part of a multipart body, by name."""
    body = request.content.decode("latin-1")
    return dict(re.findall(r'name="([^"]+)"\r\n\r\n(.*?)\r\n--', body, re.S))


def _file_disposition(request: httpx.Request) -> str:
    return re.search(r'Content-Disposition: form-data; name="file"[^\r]*',
                     request.content.decode("latin-1")).group(0)


def make_stub(state):
    def handler(request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        base = f"/v1/mintys/jobs/{JOB_ID}"

        if method == "POST" and path == "/v1/mintys/jobs":
            fields = _form_fields(request)
            state["form"] = fields
            state["file_disposition"] = _file_disposition(request)
            chosen = fields.get("label_template", DEFAULT_TEMPLATE_ID)
            if chosen not in NAMES:
                return httpx.Response(400, json={"error": "MINTYS_TEMPLATE_UNKNOWN",
                                                 "message": "Unknown label template."})
            state["created"] = True
            state["template"] = chosen
            return httpx.Response(202, json={"job_id": JOB_ID, "accepted": 2, "total": 2,
                                             "credits_reserved": 2})

        if method == "GET" and path == base:
            state["polls"] = state.get("polls", 0) + 1
            finished = state["polls"] >= 2
            return httpx.Response(200, json={
                "job_id": JOB_ID,
                "status": "DONE" if finished else "LABELLING",
                "total": 2, "done": 1 if finished else 0, "skipped": 0,
                "failed": 1 if finished else 0, "credits_spent": 1 if finished else 0,
                "label_template_id": state["template"],
                "label_template_name": NAMES[state["template"]],
                "items": [
                    {"filename": "a.png", "status": "DONE" if finished else "LABELLING"},
                    {"filename": "b.png", "status": "FAILED" if finished else "CHECKING",
                     "failure_reason": "UNSUPPORTED_FORMAT" if finished else None},
                ],
                "result_available": finished,
                "expires_at": "2026-09-21T18:00:00Z"})

        if method == "GET" and path == f"{base}/result":
            if state.get("polls", 0) < 2:
                return httpx.Response(409, json={"detail": "job still running"})
            state["downloaded"] = True
            return httpx.Response(200, content=b"PK-LABELLED-ZIP", headers={
                "content-type": "application/zip",
                "content-disposition": f'attachment; filename="mintys-job-{JOB_ID}.zip"'})

        if method == "DELETE" and path == base:
            state["deleted"] = True
            return httpx.Response(204)

        return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})
    return httpx.MockTransport(handler)


@pytest.fixture
def stub(monkeypatch):
    state = {}
    monkeypatch.setattr(server, "_client", lambda: OnmintClient(
        base_url=settings.ONMINT_API_URL, api_key="k", api_secret="s",
        transport=make_stub(state)))
    return state


ZIP_B64 = base64.b64encode(b"PK-zip-bytes").decode()


def test_label_template_omitted_is_absent_from_the_form(stub):
    result = asyncio.run(server.mintys_label_images(
        image_base64=ZIP_B64, filename="batch.zip", ai_declaration="AI_GENERATED"))

    assert "label_template" not in stub["form"]
    # ...and the default the API applied is what the job reports.
    assert result["label_template"] == {"id": DEFAULT_TEMPLATE_ID, "name": "Standard"}


def test_label_template_given_is_sent_and_reported_back(stub):
    result = asyncio.run(server.mintys_label_images(
        image_base64=ZIP_B64, filename="batch.zip", ai_declaration="AI_GENERATED",
        label_template=TEMPLATE_ID))

    assert stub["form"]["label_template"] == TEMPLATE_ID
    assert result["label_template"] == {"id": TEMPLATE_ID, "name": "Brand dark"}


def test_full_job_waits_and_returns_the_labelled_output(stub):
    result = asyncio.run(server.mintys_label_images(
        image_base64=ZIP_B64, filename="batch.zip", ai_declaration="AI_MODIFIED",
        visible_label=True))

    assert stub["form"]["ai_disclosure"] == "AI_MODIFIED"
    assert stub["form"]["visible_label"] == "true"
    assert stub["form"]["auto_label"] == "false"
    assert "label_position" not in stub["form"]
    assert result["status"] == "DONE"
    assert (result["done"], result["failed"], result["credits_reserved"]) == (1, 1, 2)
    assert result["failed_items"] == [{"filename": "b.png",
                                       "failure_reason": "UNSUPPORTED_FORMAT",
                                       "failure_detail": None}]
    assert base64.b64decode(result["result_file_base64"]) == b"PK-LABELLED-ZIP"
    assert result["result_file_name"] == f"mintys-job-{JOB_ID}.zip"
    assert result["result_content_type"] == "application/zip"


def test_an_unknown_label_template_is_refused_and_no_job_is_created(stub):
    with pytest.raises(LabelTemplateUnknownError) as exc:
        asyncio.run(server.mintys_label_images(
            image_base64=ZIP_B64, filename="batch.zip", ai_declaration="AI_GENERATED",
            label_template=UNKNOWN_TEMPLATE))

    assert "list_label_templates" in str(exc.value)
    assert not stub.get("created")


def test_the_upload_filename_reaches_the_api_unquoted(stub):
    """The API reads filenames; a percent-encoded one ('Kampagne%20Herbst.zip') is a
    different file. Asserted on the serialised header, where that corruption happens."""
    asyncio.run(server.mintys_label_images(
        image_base64=ZIP_B64, filename="Kampagne Herbst.zip", ai_declaration="AI_GENERATED",
        wait=False))

    assert 'filename="Kampagne Herbst.zip"' in stub["file_disposition"]


@pytest.mark.parametrize("kwargs", [
    {},                                                    # neither answer
    {"ai_declaration": "AI_GENERATED", "auto_label": True},  # both answers
    {"ai_declaration": "MAYBE_AI"},                        # not a declaration
])
def test_the_declaration_rule_is_checked_before_anything_is_sent(stub, kwargs):
    with pytest.raises(OnmintApiError):
        asyncio.run(server.mintys_label_images(image_base64=ZIP_B64, filename="b.zip",
                                               **kwargs))
    assert "form" not in stub


def test_auto_label_sends_no_declaration(stub):
    asyncio.run(server.mintys_label_images(image_base64=ZIP_B64, filename="b.zip",
                                           auto_label=True, wait=False))
    assert stub["form"]["auto_label"] == "true"
    assert "ai_disclosure" not in stub["form"]


def test_wait_false_returns_the_job_without_downloading(stub):
    result = asyncio.run(server.mintys_label_images(
        image_base64=ZIP_B64, filename="batch.zip", ai_declaration="AI_GENERATED", wait=False))

    assert result["job_id"] == JOB_ID
    assert result["status"] == "LABELLING"
    assert "result_file_base64" not in result and not stub.get("downloaded")


def test_get_mintys_job_reports_a_download_that_is_not_ready(stub):
    stub["template"] = TEMPLATE_ID
    result = asyncio.run(server.get_mintys_job(JOB_ID, return_file=True))

    assert result["status"] == "LABELLING"
    assert "409" in result["result_error"]
    assert result["label_template"] == {"id": TEMPLATE_ID, "name": "Brand dark"}


def test_delete_mintys_job(stub):
    assert asyncio.run(server.delete_mintys_job(JOB_ID)) == {"job_id": JOB_ID, "deleted": True}
    assert stub["deleted"]
