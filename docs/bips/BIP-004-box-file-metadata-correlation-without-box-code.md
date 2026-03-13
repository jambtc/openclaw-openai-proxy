# BIP-004 - Correlazione file/chat con lookup upload prima di completions

> Stato attuale: `🔄 In Esecuzione` - avvio 2026-03-13
> Owner: `boxedai + proxy` team

## Obiettivo

Definire un workaround operativo per:

- correlare i file caricati in BoxedAI con `user_id/chat_id/session_id`
- recuperare `public_url` da BE prima della completion
- arricchire/sostituire il contenuto del messaggio utente con `public_url`

con intercetto upload a livello edge gateway e sfruttando i dati runtime disponibili in completion.

In questo modo la completion vede gia il riferimento documento finale (URL pubblico), senza usare inject.

## Decisione tecnica (vincolo)

Richiesta prodotto aggiornata:
- la Function deve intercettare anche `POST /api/v1/files`
- il file non deve essere persistito nel dominio Box
- il flusso target e: `box -> proxy -> be` (RAG doc), poi completion con contenuto arricchito

Valutazione:
- con una Function Filter Open WebUI, **non e possibile** intercettare direttamente `POST /api/v1/files` (route HTTP backend).
- quindi una "function super-avanzata" da sola **non basta**.

Conseguenza:
- serve uno strato di intercetto a monte del router files di Box:
  1. fork Box (FE/BE) con reroute upload
  2. oppure reverse proxy che intercetta `/api/v1/files*` e delega a servizio bridge compatibile.

## Scope

- Incluso: preprocessing in Function Filter prima di inoltrare `chat/completions`.
- Incluso: lookup `GET /api/v1/uploads` su metadati salvati.
- Incluso: dichiarazione prerequisito intercetto route upload (`/api/v1/files*`) fuori dalla Function.
- Escluso: inject su `opc` (al momento non previsto).

## Identificativi target

I campi da correlare restano:

- `user_id`
- `chat_id`
- `session_id`

## Punto chiave

La Function Filter di Open WebUI **non intercetta** direttamente `POST /api/v1/files`.
Quella chiamata e interna ai router files di Box.

Conferme da codice:

- upload FE: `src/lib/apis/files/index.ts` chiama `POST /api/v1/files/?process=...`
- upload BE: `backend/open_webui/routers/files.py` endpoint `@router.post("/")`
- le Function ricevono dati durante chat completion (`__metadata__`, `__user__`, `__files__`) in `backend/open_webui/functions.py`.

## Flusso reale (riassunto)

1. Utente clicca `+` e carica file.
2. FE chiama `POST /api/v1/files/?process=true` (multipart).
3. Box salva file e ritorna `file.id`, `file.user_id`, `filename`, `meta`, `path`.
4. FE attende `GET /api/v1/files/{id}/process/status?stream=true`.
5. Al primo invio messaggio su chat nuova:
   - FE crea chat: `POST /api/v1/chats/new`
   - riceve `chat.id`
   - poi invia `POST /api/chat/completions` con:
     - `chat_id`
     - `session_id` (socket id)
     - `files`
6. Backend Box costruisce `metadata` includendo `chat_id`, `session_id`, `files`, `user_id` e li passa alle Function (`__metadata__`, `__user__`, `__files__`).

## Risposta alla domanda "chat nuova senza session_id?"

- Durante **upload** file: corretto, `chat_id/session_id` non sono affidabili/definitivi.
- Durante la **prima chat completion**: `chat_id` e `session_id` sono gia presenti nel payload runtime.

Quindi la correlazione robusta va fatta li, non sull'upload puro.

## Workaround consigliato (dopo intercetto upload)

Usare la Function gia presente (`openclaw_session_bridge.py`) estendendola in modo compatibile:

1. In `inlet`, oltre a `body.user`, leggere:
   - `__user__.id`
   - `__metadata__.chat_id`
   - `__metadata__.session_id`
   - `__metadata__.files` (o `body.files` fallback)
2. Per ogni file, creare/aggiornare record di correlazione locale:
   - chiave primaria consigliata: `(user_id, file_id)`
   - campi: `chat_id`, `session_id`, `message_id`, `filename`, `content_type`, `size`, `ts_first_seen`
3. Prima di inoltrare la completion, interrogare BE con:
   - `GET /api/v1/uploads`
   - filtro tramite metadati gia salvati (es. `metadata_contains` con chiavi correlazione)
   - obiettivo: recuperare `public_url` del documento associato al `file_id`
4. Se lookup positivo:
   - aggiornare il payload completion sostituendo i riferimenti file con `public_url`
   - oppure appendere al messaggio utente un blocco contestuale con `nome file + public_url`
5. Solo dopo il lookup+rewrite, inoltrare la richiesta a `chat/completions`.
6. Se `chat_id` assente (caso edge/local), salvare record provvisorio e completarlo alla prima richiesta con `chat_id`.

## Flusso operativo aggiornato (corrente)

1. Intercetto `POST /api/v1/files` (fork Box o reverse proxy), senza persistenza finale in Box.
2. Upload va a `proxy -> be /api/v1/uploads`.
3. Il layer di intercetto restituisce al FE una risposta compatibile con schema file Box (id/url/status), includendo metadata tecnici necessari.
4. FE prosegue normale su `chats/new` e poi `chat/completions`.
5. Function `inlet` riceve `__user__`, `__metadata__`, `__files__`.
6. Function salva/aggiorna correlazione file.
7. Function chiama `GET /api/v1/uploads` usando metadati correlati.
8. Function estrae `public_url`.
9. Function riscrive/arricchisce il messaggio utente con `public_url`.
10. Richiesta inoltrata a completion.

## Dati minimi da salvare

- `file_id`
- `user_id`
- `chat_id` (quando disponibile)
- `session_id` (quando disponibile)
- `message_id` (se disponibile)
- `filename`
- `content_type`
- `size`
- `upload_status/process_status`
- timestamp creazione/aggiornamento correlazione
- `metadata_lookup_key` (chiave usata per query verso `/api/v1/uploads`)
- `public_url` (cache ultimo valore noto)

## Limiti del workaround

- Non si intercetta il multipart in tempo reale via Function (serve patch Box o reverse proxy dedicato).
- `session_id` puo variare tra connessioni socket; per stabilita funzionale usare `chat_id` come riferimento principale.
- In temporary chat (`local:*`) la persistenza chat e diversa: va trattata separatamente.

## Criteri di accettazione

- Nessun file documento resta nel path storage Box del flow chat target.
- Ogni `POST /api/v1/files` del FE viene intercettata e instradata a `proxy -> be`.
- Il FE riceve risposta compatibile (nessuna regressione UI upload/status).
- Su chat nuova con file allegato, alla prima completion esiste un record con `file_id + user_id + chat_id`.
- Prima di completion viene eseguita una `GET /api/v1/uploads` con metadati di correlazione.
- Nel payload inoltrato a completion compare `public_url` al posto del riferimento file locale/id.
- Evidenza in log/tracing del mapping file -> chat/sessione.

## Riferimenti tecnici (repo locale)

- Box FE upload: `/var/www/open-webui/src/lib/apis/files/index.ts`
- Box FE create chat: `/var/www/open-webui/src/lib/apis/chats/index.ts`
- Box FE send completion payload (chat_id/session_id/files): `/var/www/open-webui/src/lib/components/chat/Chat.svelte`
- Box BE upload endpoint: `/var/www/open-webui/backend/open_webui/routers/files.py`
- Box BE metadata per functions: `/var/www/open-webui/backend/open_webui/main.py`
- Function runtime params: `/var/www/open-webui/backend/open_webui/functions.py`
