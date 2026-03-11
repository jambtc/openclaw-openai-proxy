# OpenClaw OpenAI Proxy

OpenAI-compatible HTTP proxy that lets Open WebUI talk to OpenClaw through the
BFF backend (`be`) while preserving chat/session affinity. It exposes:

- `/v1/models` – advertises your OpenClaw agents as OpenAI models and publishes a
  pipeline filter that injects the Open WebUI `chat_id` into the OpenAI `user`
  field.
- `/v1/chat/completions` – forwards requests to `be` (`/v1/chat/completions`).
- `/v1/completions` – forwards requests to `be` (`/v1/completions`).
- `/v1/responses` – forwards requests to `be` (`/v1/responses`).
- `/v1/uploads/bridge` – forwards multipart upload requests to `be`
  (`/api/v1/uploads`) and returns the BE payload plus a bridge correlation id.
- `/<pipeline-id>/filter/(inlet|outlet)` – remote pipeline hooks consumed by Open
  WebUI filters.

## Features

- Stable session routing: the bundled pipeline copies `__metadata__.chat_id`
  into `user`, so OpenClaw derives a deterministic session key per chat.
- Explicit agent routing: every exposed model maps to an OpenClaw agent id
  (`model=openclaw:<agentId>`).
- Split YAML configuration: `backend` (BE) and `gateway` (OPC metadata/future
  direct calls).
- Works with Open WebUI's *Connections → OpenAI Compatible* feature and the
  Pipelines UI (filter type).

## Project layout

```
openclaw-openai-proxy/
├── openclaw_openai_proxy/
│   ├── config.py          # Pydantic models for gateway/agent/pipeline config
│   ├── backend.py         # HTTP client for BE calls
│   ├── gateway.py         # Async HTTP client for the OpenClaw Gateway
│   ├── main.py            # Entry point used by `openclaw-openai-proxy` CLI
│   ├── server.py          # FastAPI app + pipeline handlers
│   └── settings.py        # Loads YAML config via OPENCLAW_PROXY_CONFIG
├── config.example.yaml    # Sample configuration
├── pyproject.toml         # Project metadata + dependencies
└── README.md              # This file
```

## Configuration

Create a `config.yaml` (or point `OPENCLAW_PROXY_CONFIG` to another path):

```yaml
gateway:
  base_url: http://127.0.0.1:18789
  token: "${OPENCLAW_GATEWAY_TOKEN}"  # optional if proxy does not call gateway directly

backend:
  base_url: http://127.0.0.1:8000
  timeout_seconds: 120

agents:
  - id: "openclaw:contabo"
    name: "Contabo (OpenClaw)"
    description: "Instruito con tono tecnico."
    agent_id: "main"
    tags: ["internal", "openclaw"]

pipeline:
  id: "openclaw-session-filter"
  name: "OpenClaw session bridge"
  description: "Propaga chat_id nel campo user per sessioni stabili."
  pipelines: ["*"]
  priority: 500
```

Environment variables (optional):

- `OPENCLAW_PROXY_CONFIG`: path to the YAML file (default: `config.yaml`).
- `OPENCLAW_GATEWAY_TOKEN`: expanded when referenced as `${OPENCLAW_GATEWAY_TOKEN}`.

## Running locally

```bash
cd openclaw-openai-proxy
python -m venv .venv && source .venv/bin/activate
pip install -e .
OPENCLAW_PROXY_CONFIG=config.yaml openclaw-openai-proxy
```

The service listens on `0.0.0.0:4010` by default.

## Wiring it into Open WebUI

1. **Connection** – add a new OpenAI-compatible connection that points to the
   proxy (e.g. `http://HOST:4010`) and use any placeholder API key (the proxy
   currently trusts the caller; network-level ACLs are recommended).
2. **Models** – the `/v1/models` response will expose each configured agent as a
   selectable model.
3. **Pipeline filter** – in *Pipelines* choose the proxy connection, import the
   `openclaw-session-filter`, and attach it to the models that should inherit
   the stable session behaviour.

## Deploying (systemd snippet)

```ini
[Unit]
Description=OpenClaw OpenAI Proxy
After=network.target

[Service]
Environment=OPENCLAW_PROXY_CONFIG=/etc/openclaw-openai-proxy.yaml
WorkingDirectory=/opt/openclaw-openai-proxy
ExecStart=/opt/openclaw-openai-proxy/.venv/bin/openclaw-openai-proxy
Restart=always

[Install]
WantedBy=multi-user.target
```

## Limitations

- `/pipelines/upload` and `/pipelines/add` return HTTP 405 (not yet supported).
- `chat/completions`, `completions`, `responses`, and `uploads/bridge` are proxied; embeddings/images are out of scope.
- If upstream `be` returns `404 Not Found` on `/v1/responses`, the proxy applies a compatibility fallback to `be /v1/chat/completions` and translates the output to a `response`-like shape.
- The proxy trusts inbound requests; place it behind a reverse proxy or private
  network segment if you need authentication.

Contributions via pull requests/issues are welcome.
