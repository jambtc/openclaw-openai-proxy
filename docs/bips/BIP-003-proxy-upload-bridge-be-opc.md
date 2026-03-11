# BIP-003 - Proxy upload API + bridge BE + context inject OPC

> Stato attuale: `🔄 In Esecuzione` - avvio 2026-03-11
> Owner: `proxy + be` team

## Contesto

Obiettivo funzionale:
- ricevere documento dal relay Box
- inoltrarlo a `be`
- usare la risposta JSON di `be` per creare contesto nella sessione OpenClaw (`opc`) senza azione manuale utente

Situazione attuale:
- `proxy` non espone endpoint upload documentale applicativo (ha solo upload pipeline `.py`)
- `be` espone gia API upload (`/api/v1/uploads...`) e API inject WS verso OpenClaw (`/api/v1/conversations/{conversation_id}/inject`)

## Proposta

Implementare nel proxy un endpoint bridge upload, ad esempio:
- `POST /v1/uploads/bridge` (multipart)

Flow proposto:

1. `proxy` riceve multipart da Box relay con metadata correlazione.
2. `proxy` inoltra multipart a `be` su `POST /api/v1/uploads`.
3. `be` risponde con JSON upload (id, urls, metadata, ecc.).
4. `proxy` costruisce messaggio di contesto documento (titolo, id upload, tipo, link utile).
5. `proxy` invoca endpoint `be` per inject su sessione/conversazione:
   - `POST /api/v1/conversations/{conversation_id}/inject`
6. `be` esegue `chat.inject` su `opc` via WS e persiste evento.

Nota:
- per rispettare il flow `box -> proxy -> be -> opc`, la chiamata "nascosta" a `opc` va orchestrata dal proxy tramite endpoint `be`, non bypassando `be`.

## Contratto minimo payload (bozza)

Campi request Box -> proxy:
- `file` (multipart file)
- `box_user_id`
- `box_chat_id` (o equivalente id conversazione)
- `box_file_id`
- `metadata_json` (opzionale)

Campi response proxy -> Box:
- `status`
- `bridge_upload_id`
- `be_upload_id`
- `context_injected` (true/false)
- `warnings` (eventuali)

## Impatto atteso

- Documenti realmente disponibili al dominio `be/opc` nella stessa sessione chat.
- Contesto documento iniettato in modo trasparente prima della richiesta utente successiva.
- Tracciabilita completa da upload UI fino a inject su opc.

## Rischi

- Mapping non affidabile tra `box_chat_id` e `be conversation_id`.
- Duplicazione inject su retry upload (serve idempotenza).
- Timeout tra upload file e inject context.
- Sicurezza: endpoint upload bridge da proteggere (trusted source o token interno).

## Criteri di accettazione

- `proxy` accetta multipart upload documentale e lo inoltra con successo a `be`.
- `be` ritorna JSON upload valido e persistito.
- Viene eseguita inject su conversazione/sessione corretta in `opc`.
- In una chat Box, dopo upload file, `opc` risponde con awareness del documento senza re-upload manuale.
- Log correlati disponibili con chiave unica (`box_file_id` + `be_upload_id` + session key/hash).

## Dipendenze

- BIP-002 (hook relay upload in Box).
- Allineamento identificatori conversazione tra Box e BE (nuovo mapping o metadata affidabile).
- Endpoint/protezione interna tra proxy e BE.
- Riferimento OpenAPI BE: `docs/bips/references/BE-openapi-uploads.md`.

## Avanzamento

### 2026-03-11 - Analisi iniziale

- Identificato gap: in proxy manca endpoint upload applicativo.
- Confermato in `be` presenza API upload e API inject verso OpenClaw.
- Definito flow target in 6 step con orchestrazione `proxy -> be -> opc`.

### 2026-03-11 - Allineamento OpenAPI BE

- Analizzato file OpenAPI fornito: `/home/sergio/Scaricati/swagger-backend.json`.
- Confermato endpoint upload target: `POST /api/v1/uploads` (`multipart/form-data`).
- Confermato endpoint inject target: `POST /api/v1/conversations/{conversation_id}/inject`.
- Aggiunto riferimento operativo: `docs/bips/references/BE-openapi-uploads.md`.
- Estratto schema ridotto: `docs/bips/references/BE-openapi-uploads-schemas.json`.

### 2026-03-11 - Step 1 implementato (proxy upload bridge)

- Implementato nuovo client backend nel proxy: `openclaw_openai_proxy/backend.py`.
- Estesa config proxy con sezione `backend`:
  - `base_url`
  - `timeout_seconds`
- Aggiornati file config:
  - `config.example.yaml`
  - `config.yaml`
- Implementato endpoint bridge upload:
  - `POST /v1/uploads/bridge`
  - alias `POST /uploads/bridge`
- Comportamento endpoint:
  - accetta `multipart/form-data`
  - inoltra multipart a `be` su `POST /api/v1/uploads`
  - propaga header `Authorization` e `X-Debug-User` se presenti
  - ritorna payload BE con `bridge_upload_id` aggiunto
- Verifica locale:
  - controllo sintassi Python: `ok`
  - test E2E non ancora eseguito (richiede `be` attivo e chiamata reale multipart verso proxy).

### 2026-03-11 - Step 1 validato E2E su BE remoto

- Configurato uso BE remoto: `https://be-boxedai-contabo.theia-innovation.com`.
- Eseguito test reale su `POST /v1/uploads/bridge` con file di prova multipart.
- Risultato positivo:
  - `bridge_upload_id` restituito dal proxy
  - `upload_id` restituito dal BE
  - `status=uploaded`
- Verificato download file da endpoint BE:
  - `GET /api/v1/uploads/{upload_id}/download` raggiungibile e contenuto corretto.
- Correzione tecnica applicata durante test:
  - fix forwarding multipart nel proxy con passthrough raw body + content-type originale (boundary invariato), evitando errori di encoding multipart.
