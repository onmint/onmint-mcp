---
name: onmint-authenticity
description: >-
  Protect original content or attach an EU AI Act label to AI-generated content, and verify
  the authenticity / AI content of ANY image, via the on:mint authenticity API. Use when a
  task involves proving an image is an authored original, labeling AI-generated output as
  synthetic (C2PA + invisible watermark + on-chain anchor), or checking whether an image is
  AI-generated / manipulated.
---

# on:mint authenticity

One interface, one AI check. Submit content and on:mint **always runs an AI check first**,
then routes to one of two modes of the *same* pipeline:

- **Not AI (the normal case)** → *protect original*: invisible watermark + C2PA Content
  Credentials + on-chain anchor establishing the asset as an authored/captured original.
- **AI detected (edited or fully generated)** → *label AI*: the same pipeline, but the C2PA
  manifest carries a machine-readable `digitalSourceType` (EU AI Act Art. 50) and the
  watermark encodes the AI class.

The caller may declare "this is AI" upfront, but **detection decides the mode** — the
declaration is recorded for transparency only.

## When to use which tool

| Goal | Tool |
|---|---|
| Submit content, let the AI check decide protect-vs-label | `submit_content` |
| An AI tool attaching a secure AI label to its own output (Art. 50), getting the labeled file back | `label_ai_output` |
| Protect an authored original (still AI-checked) | `protect_original` |
| Verify any image (even one we never stored) + its provenance | `verify_image` |
| Report how much AI content an image holds | `analyze_image` |
| Poll a `wait=false` submission | `get_status` |
| Look up an asset's provenance by watermark id / SHA-256 | `get_provenance` |
| Get a ready-to-submit stream (reuse or auto-provision) | `ensure_stream` |
| List / create templates, vaults, streams over the API | `list_*` / `create_template` / `create_vault` / `create_stream` |

`label_ai_output` returns the **credentialed file** (base64) plus a public `verify_url` by
default; pass `save_to=<path>` to also write it. All submit tools take an **optional**
`stream_id` — omit it and one is reused/provisioned automatically.

## Setup

The server talks to the on:mint authenticity API with API-key credentials (from
`POST /register` on the appcontroller-api). Set:

- `ONMINT_API_URL` (default `https://api.dev-onmint.com/v1`)
- `ONMINT_API_KEY`, `ONMINT_API_SECRET`

Run locally over stdio (`onmint-mcp`) or hosted over HTTP (`ONMINT_MCP_TRANSPORT=streamable-http`).

Get credentials from `POST /register`, then manage additional scoped keys under `/v1/keys`
(or the web app's **Developers** page).

## Prerequisites and gotchas

- **`stream_id` is optional.** Submissions land in a stream (inside a vault). Omit `stream_id`
  and the server reuses an existing stream or provisions a template → vault → stream. Set
  `ONMINT_DEFAULT_STREAM_ID` to pin one and skip provisioning (recommended for repeated use;
  API vault creation can be slow to settle in some environments).
- **Images only** get an invisible watermark; other blobs are provenance-only (hash + C2PA
  sidecar). AI classification still applies.
- **`verify_image` / `analyze_image` work on any image** — no prior submission needed. A
  `match_method` of `none` means the image is unknown to on:mint; the AI analysis is still
  returned.
- AI-generation probability is a **calibrated estimate**, not ground truth — present it as a
  probability, never as proof.

## Direct API (without MCP)

The same operations are available over HTTP for CI/CD and scripts (all API-key gated):

- `POST /v1/authenticity/attachments` → create submission (`declared_ai` optional) → then
  `GET` for presigned upload URLs → upload → `PUT` to complete (multipart) → `GET` to poll.
- `POST /v1/authenticity/verify` (multipart) → verify-by-file.
- `POST /v1/authenticity/analyze` (multipart) → AI content analysis (incl. `ai_content_share`).
- `GET /v1/authenticity/provenance/{watermark_id}` and `/provenance/by-hash/{sha256}`.
- Provision over the API: `POST /v1/authenticity/templates` → `POST /v1/authenticity/vaults`
  → `POST /v1/authenticity/vaults/{id}/streams`.
- Manage keys: `GET/POST /v1/keys`, `POST /v1/keys/{key}/rotate`, `POST /v1/keys/{key}/revoke`.

Interactive docs at `GET /v1/docs`.
