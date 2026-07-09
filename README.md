# onmint-mcp

MCP server exposing the **on:mint authenticity API** — protect originals or attach an EU AI
Act label to AI-generated content, and verify the authenticity / AI content of any image.

One AI check, two modes of one pipeline: every submission is AI-checked first; originals are
protected (watermark + C2PA + on-chain anchor) and AI content is labeled (adds a C2PA
`digitalSourceType` marking + AI-tagged watermark). Detection decides the mode; an optional
caller declaration is recorded only.

## Tools

**Content**
- `submit_content` — submit an image; the AI check decides protect-vs-label.
- `label_ai_output` — attach a secure AI label to AI-generated output (for AI-tool providers).
  Returns the **credentialed file** (base64) + a public `verify_url` by default.
- `protect_original` — protect an authored original (still AI-checked).
- `verify_image` — verify any image (hash → watermark → pHash) + C2PA validation.
- `analyze_image` — report how much AI content an image holds.
- `get_status` — poll a `wait=false` submission.
- `get_provenance` — look up provenance by watermark id or SHA-256.

**Provisioning** (set up a place to submit, over the API — no web app needed)
- `ensure_stream` — get a ready-to-use stream id (reuse or auto-provision).
- `list_vaults` / `list_streams` / `list_templates` — discover existing resources.
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

```bash
docker build -t onmint-mcp .
docker run -p 8000:8000 \
  -e ONMINT_API_KEY=... -e ONMINT_API_SECRET=... \
  -e ONMINT_MCP_TRANSPORT=streamable-http onmint-mcp
```

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
