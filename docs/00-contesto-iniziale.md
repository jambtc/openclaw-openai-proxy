# Contesto Iniziale (proxy / boxedai / be / openclaw)

Data: 2026-03-11

## Riferimenti repository

- `proxy`: `/var/www/openclaw-openai-proxy`
- `boxedai`: `/var/www/open-webui`
- `be`: `/var/www/openclaw-based-backend` (path richiesto: `/var/www/openclaw-based-backed`)
- `openclaw` (repo): `https://github.com/openclaw/openclaw`

## Naming condiviso

- `proxy` = OpenAI-compatible bridge tra UI e backend applicativo
- `boxedai` = interfaccia Open WebUI
- `be` = openclaw-based-backend
- `openclaw` = runtime/agent platform a valle del backend
- `opc` = alias rapido per `openclaw`

## Flow di funzionamento

- forward: `boxedai -> proxy -> be -> openclaw`
- reverse: `openclaw -> be -> proxy -> boxedai`

## BE: componenti e avvio

- In `be` sono presenti i servizi infrastrutturali:
  - Postgres (persistenza dati conversazioni/messaggi/sessioni).
  - Keycloak (auth/OIDC per integrazione con boxedai).
  - MinIO (object storage file upload/download).
- Lo script `scripts/dev_run.sh` avvia il backend FastAPI usando variabili da `.env` (tipicamente con venv attivo) ed espone `0.0.0.0:${BFF_PORT:-8000}`.
- OpenClaw non e nel compose infra di `be`: viene raggiunto come servizio esterno.

## Contratto tra componenti

- `boxedai` parla protocollo OpenAI-compatible verso `proxy`.
- `boxedai` ha gia attiva la Filter Function `function/openclaw_session_bridge.py` che imposta `body.user = sha256(user_id:chat_id)` per mantenere coerente la sessione verso `opc`.
- `proxy` espone endpoint OpenAI (models/chat-completions), applica compatibilita e normalizzazione payload.
- `proxy` instrada le chiamate operative (chat + upload/documenti) verso `be`.
- `proxy` gestisce affinita di sessione per chat (chiave deterministica in `user` quando disponibile metadata chat).
- `be` orchestra logica applicativa e integrazione con `openclaw`.
- `openclaw` esegue agenti/workflow e restituisce output lungo la stessa catena in verso inverso.

## Stato tecnico corrente del proxy

- Endpoint principali disponibili:
  - `GET /v1/models` (alias `/models`)
  - `POST /v1/chat/completions` (alias `/chat/completions`)
  - `POST /v1/completions` (alias `/completions`)
  - `POST /v1/responses` (alias `/responses`)
  - `POST /v1/uploads/bridge` (alias `/uploads/bridge`)
  - endpoint pipeline/filter/valves anche con alias `/v1/...`.
- Mapping modelli:
  - `model` in ingresso viene risolto su config agenti e tradotto in formato OpenClaw (`openclaw:<agent_id>`).
- Session affinity:
  - in `inlet` pipeline, se c'e `chat_id`, imposta `user = sha256(user_id:chat_id)` (`enforce_user=true`).
- Config runtime:
  - file YAML via `OPENCLAW_PROXY_CONFIG` (default `config.yaml`)
  - split config `gateway` (opc) e `backend` (be)
  - token gateway via env expansion `${OPENCLAW_GATEWAY_TOKEN}`.
- Completions:
  - forwarding attuale non-streaming (`stream=false`) verso `be` su endpoint OpenAI compatibili:
    - `/v1/chat/completions`
    - `/v1/completions`
    - `/v1/responses`
  - fallback compatibilita: se `be /v1/responses` ritorna `404 Not Found`, il proxy traduce la richiesta su `be /v1/chat/completions` e ritorna shape `response` minima.

## Porte esposte (tutti i servizi del contesto)

- `boxedai` (`/var/www/open-webui`):
  - `127.0.0.1:3001 -> container 8080` (Open WebUI)
  - `6333 -> 6333` (Qdrant)
- `proxy` (`/var/www/openclaw-openai-proxy`):
  - `4010 -> 4010` (OpenClaw OpenAI Proxy)
- `be` API (`/var/www/openclaw-based-backend`):
  - `8000` (uvicorn via `scripts/dev_run.sh`, default `BFF_PORT`)
- `be` infra (`docker-compose.infra.yml`):
  - `5432 -> 5432` (Postgres)
  - `9000 -> 9000` (MinIO API S3)
  - `9001 -> 9001` (MinIO Console)
  - `8080 -> 8080` (Keycloak)
- `openclaw` (servizio esterno al compose di `be`):
  - `18789` (HTTP Gateway)
  - `18789/ws` (WebSocket RPC)
- dipendenza runtime boxedai:
  - `11434` (Ollama atteso da Open WebUI; non esposto nel compose corrente di `boxedai`)

## Invarianti operative

- mantenere compatibilita OpenAI lato `boxedai`.
- mantenere sessione stabile per chat lungo la catena.
- evitare loop architetturali tra componenti upstream/downstream.
- centralizzare nel `proxy` adattamento protocollo e regole di routing/mapping.
