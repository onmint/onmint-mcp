# onmint-mcp

MCP server exposing the **on:mint authenticity API** — protect originals or attach an EU AI
Act label to AI-generated content, and verify the authenticity / AI content of any image.

**You declare, we sign.** Every submission carries a required `ai_declaration` —
`CREATED_WITHOUT_AI`, `AI_ENHANCED`, `AI_MODIFIED` or `AI_GENERATED` — and that declaration
is the authoritative AI label: it is written into the signed C2PA manifest as an IPTC
`digitalSourceType` and encoded in the watermark. One pipeline either way (watermark + C2PA +
on-chain anchor).

An AI detector still runs, and it no longer decides anything. Its reading comes back as a
secondary automated assessment in one of three tiers and never overrides the declaration.

## Tools

**Content**
- `submit_content` — submit an image. **Requires `ai_declaration`** — ask the user, do not
  guess: it is signed in their name.
- `label_ai_output` — attach a secure AI label to AI-generated output (for AI-tool providers);
  declares `AI_GENERATED`. Returns the **credentialed file** (base64) + a public `verify_url`
  by default.
- `protect_original` — protect an authored original; declares `CREATED_WITHOUT_AI` (override
  with `AI_ENHANCED` for AI retouching/upscaling).
- `verify_image` — verify any image (hash → watermark → pHash) + C2PA validation.
- `analyze_image` — report the AI signals an image carries, as one of three tiers.
- `get_status` — poll a `wait=false` submission.
- `get_provenance` — look up provenance by watermark id or SHA-256.

**mintys** (the mintys job pipeline: no stream, no IPFS, no on-chain anchor)
- `mintys_label_images` — label one image or a `.zip` batch. One `ai_declaration` for the
  whole upload, or `auto_label=true`. Waits and returns the labelled output by default.
- `get_mintys_job` / `delete_mintys_job` — poll a `wait=false` job (and fetch its output),
  or drop its temporary output early.

**Label templates** — how the visible AI label looks
- `list_label_templates` — the organization's label templates: `id`, `name`, `is_default`.

`label_template` is an optional argument of `mintys_label_images`, `label_ai_output`,
`submit_content` and `protect_original`. Omit it to use the organization's default. An id
the organization does not have is refused (`MINTYS_TEMPLATE_UNKNOWN`) and nothing is
labelled with a substitute. The template applied to a mintys job is reported back as
`label_template` `{id, name}`.

**Provisioning** (set up a place to submit, over the API — no web app needed)
- `ensure_stream` — get a ready-to-use stream id (reuse or auto-provision).
- `list_vaults` / `list_streams` / `list_templates` — discover existing resources.
  `list_templates` lists **asset** templates, not label templates.
- `create_template` / `create_vault` / `create_stream` — build the graph explicitly.

The submit tools take an optional `stream_id`; when omitted they call `ensure_stream`, so a
caller needs **zero prior setup**. The credentialed file is fetched back from IPFS; pass
`return_file=true` (default on `label_ai_output`) or `save_to=<path>` to receive/write it.

## Install & run

```bash
pip install -e .
export ONMINT_API_URL=https://api.dev-onmint.com/v1
export ONMINT_API_KEY=...        # from POST /register on the appcontroller-api
export ONMINT_API_SECRET=...
onmint-mcp                        # stdio (local); or ONMINT_MCP_TRANSPORT=streamable-http
```

## MCP client config (stdio)

```json
{
  "mcpServers": {
    "onmint-authenticity": {
      "command": "onmint-mcp",
      "env": {
        "ONMINT_API_URL": "https://api.dev-onmint.com/v1",
        "ONMINT_API_KEY": "...",
        "ONMINT_API_SECRET": "..."
      }
    }
  }
}
```

## Hosted (streamable-http)

Deployed by CI to `https://api.dev-onmint.com/mcp` (dev, on push to `dev`) and
`https://api.app.onmint.io/mcp` (prod, on push to `main`). Manifests live in
`filedgr-k8s-deployments/environments/{dev,prod}/services/onmint-mcp`.

**Credentials travel with the request, not with the server.** One hosted process serves every
tenant, so it holds no API key of its own: send your own `x-api-key` / `x-api-secret` on every
request and the server acts as you. There is no fallback to the environment — an
uncredentialed tool call fails rather than borrowing someone else's identity. `initialize`
and `tools/list` need no credentials, so a client can connect and discover tools first.

```json
{
  "mcpServers": {
    "onmint": {
      "url": "https://api.app.onmint.io/mcp",
      "headers": {
        "x-api-key": "YOUR_KEY",
        "x-api-secret": "YOUR_SECRET"
      }
    }
  }
}
```

Two tool arguments behave differently here than over stdio: `image_path` and `save_to` name a
path on the *server's* disk rather than yours, so the hosted server refuses both. Send
`image_base64` and take the result back with `return_file=true`.

Run it yourself:

```bash
docker build -t onmint-mcp .
docker run -p 8000:8000 -e ONMINT_MCP_TRANSPORT=streamable-http onmint-mcp
```

Configuration (`ONMINT_MCP_HOST`, `ONMINT_MCP_PORT`, `ONMINT_MCP_HTTP_PATH`) defaults to
`0.0.0.0:8000/mcp`. `GET /health/live` and `/health/ready` answer for orchestrator probes.

## Bundle

`manifest.json` describes an MCP bundle (`.mcpb`) for one-click install into supporting
clients, prompting the user for their API key/secret. Build with the `mcpb` CLI.

## Skill

`SKILL.md` is the agent-facing description of the workflow — which tool fits which goal, and
the caveats the tool descriptions cannot carry. The server gives an agent the *tools*; the
skill tells it *when and how* to reach for them. Install both.

`.claude/skills/onmint-authenticity/SKILL.md` symlinks the root file, so cloning this repo
into a project already puts the skill on disk where Claude Code looks for it. To install it
standalone:

```bash
mkdir -p ~/.claude/skills/onmint-authenticity
curl -o ~/.claude/skills/onmint-authenticity/SKILL.md \
  https://raw.githubusercontent.com/onmint/onmint-mcp/main/SKILL.md
```

## Notes

- `stream_id` is optional — omit it and a stream is reused/provisioned automatically
  (set `ONMINT_DEFAULT_STREAM_ID` to pin one and skip provisioning).
- Images are provided as a local `image_path` or `image_base64`.
- Get credentials from `POST /register` on the API, then manage keys under `/keys` (or the
  web app's Developers page).
- See `SKILL.md` for the agent-facing skill description.
