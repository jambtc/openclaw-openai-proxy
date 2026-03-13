# Contesto Iniziale (proxy / boxedai / be / openclaw)

Data aggiornamento: 2026-03-13

## Riferimenti repository

- `proxy`: `/var/www/openclaw-openai-proxy`
- `boxedai`: `/var/www/open-webui`
- `be`: `/var/www/openclaw-based-backend` (path richiesto: `/var/www/openclaw-based-backed`)
- `openclaw` (repo): `https://github.com/openclaw/openclaw`

## Naming condiviso

- `proxy` = componente in evoluzione verso **edge gateway**
- `boxedai` = interfaccia Open WebUI
- `be` = openclaw-based-backend (BFF + RAG documentale)
- `openclaw` / `opc` = runtime agent a valle del backend

## Perche il pivot a Edge Gateway

Requisito prodotto:
- intercettare `POST /api/v1/files`
- evitare persistenza finale documento nel dominio Box
- forzare percorso documenti `box -> proxy -> be`

Vincolo tecnico:
- le Function OpenWebUI non intercettano i router HTTP files (`/api/v1/files`)
- quindi serve un layer L7 davanti a Box (reverse proxy / API gateway)

## Flow: stato attuale vs target

Stato attuale (implementato):
- chat/completions: `boxedai -> proxy -> be -> openclaw`
- upload bridge tecnico disponibile su `proxy /v1/uploads/bridge`

Target edge gateway:
- richieste Box FE passano da gateway
- route upload `/api/v1/files*` intercettate e deviate su `proxy -> be`
- completions arricchite con `public_url` risolta via `GET /api/v1/uploads`

## Contratto operativo corrente

- Endpoint OpenAI nel proxy:
  - `GET /v1/models` (alias `/models`)
  - `POST /v1/chat/completions` (alias `/chat/completions`)
  - `POST /v1/completions` (alias `/completions`)
  - `POST /v1/responses` (alias `/responses`)
  - `POST /v1/uploads/bridge` (alias `/uploads/bridge`)
- Session bridge attivo in Function Box:
  - `body.user = sha256(user_id:chat_id)`
- `v1/responses` ha fallback su `chat/completions` se upstream non disponibile.

## Porte esposte (contesto)

- `boxedai` (`/var/www/open-webui`):
  - `127.0.0.1:3001 -> 8080`
  - `6333 -> 6333` (Qdrant)
- `proxy` (`/var/www/openclaw-openai-proxy`):
  - `4010 -> 4010`
- `be` API (`/var/www/openclaw-based-backend`):
  - `8000` (default `BFF_PORT`)
- `be` infra:
  - `5432` (Postgres)
  - `9000` / `9001` (MinIO API/Console)
  - `8080` (Keycloak)
- `openclaw` esterno:
  - `18789` HTTP
  - `18789/ws` WebSocket

## Invarianti

- compatibilita FE Box senza regressioni UX upload/chat
- osservabilita end-to-end (correlation id file/chat/user)
- centralizzazione regole di routing e trasformazione nel gateway
