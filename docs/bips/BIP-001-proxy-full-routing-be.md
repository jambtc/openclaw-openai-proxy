# BIP-001 - Proxy full-routing verso BE (completions + documenti)

> Stato attuale: `🔄 In Esecuzione` - avvio 2026-03-11
> Owner: `proxy` team

## Contesto

Il comportamento target e:

`boxedai -> proxy -> be -> openclaw` (e ritorno)

Oggi il `proxy` e gia il punto di ingresso OpenAI-compatible per BoxedAI, ma il flusso non e ancora completo lato `be` soprattutto per la gestione documenti/file.

In particolare:

- le richieste chat/completions devono essere instradate verso `be`
- le richieste legate ai documenti devono passare nello stesso flow (oggi gap operativo)
- la coerenza sessione chat lato OpenClaw e gia coperta in BoxedAI tramite Filter Function `function/openclaw_session_bridge.py`

## Proposta

Portare il `proxy` a essere il gateway unico verso `be` per:

- completions/chat
- upload/document handling
- eventuali endpoint compatibili necessari a BoxedAI

Linee guida:

- mantenere compatibilita OpenAI lato BoxedAI
- centralizzare nel proxy routing, normalizzazione payload e session affinity
- delegare a `be` la logica applicativa (messaggi, storage documenti, integrazione OpenClaw)

## Impatto atteso

- Routing coerente e osservabile su un solo ingresso (`proxy`).
- Riduzione disallineamenti tra chat e documenti.
- Possibilita di estendere funzionalita documentali senza patch lato UI.

## Rischi

- Regressioni sul flusso chat se il mapping endpoint non e completo.
- Incompatibilita di formato tra payload OpenAI e contratti API `be`.
- Gestione file/documenti non idempotente o non allineata ai metadati chat/sessione.

## Criteri di accettazione

- Tutte le richieste BoxedAI previste dal prodotto transitano via `proxy`.
- Le chat completions funzionano end-to-end con flow `boxedai -> proxy -> be -> openclaw`.
- Le operazioni documentali (upload/uso in chat) funzionano nello stesso flow.
- Evidenza in log (proxy/be) del percorso request/response.
- Documentazione BIP aggiornata con step tecnici eseguiti.

## Dipendenze

- `be` disponibile su porta `8000` in dev (`scripts/dev_run.sh`).
- Infra `be` attiva: Postgres, MinIO, Keycloak.
- Contratti endpoint `be` confermati rispetto alle chiamate effettive di BoxedAI via proxy.
- Split operativo documentato in:
  - `BIP-002` (intercetto upload in Box)
  - `BIP-003` (upload bridge proxy -> be -> opc)

## Piano iniziale (da fare)

1. Mappare tutte le chiamate in uscita da BoxedAI che devono essere servite dal proxy.
2. Definire matrice endpoint `proxy -> be` (chat, files, eventuali metadata).
3. Implementare nel proxy l'instradamento completo verso `be` per completions.
4. Implementare nel proxy l'instradamento completo per documenti.
5. Eseguire test end-to-end su chat + documenti e fissare acceptance evidence.
6. Aggiornare README tecnico del proxy con il nuovo contratto operativo.

## Baseline gia verificata

- Session bridge in BoxedAI gia implementato e funzionante:
  - file: `function/openclaw_session_bridge.py` (nel repo `proxy`, caricato in BoxedAI come Function Filter)
  - logica: legge `__user__` e `__metadata__.chat_id`, poi imposta `body.user = sha256(user_id:chat_id)`
  - esito: testato e funzionante correttamente per coerenza sessione verso OpenClaw (`opc`)

## Avanzamento

### 2026-03-11 - Avvio BIP

- Creata cartella `docs/bips` nel proxy.
- Creato indice BIP (`docs/bips/README.md`) con numerazione progressiva.
- Formalizzato BIP-001 con obiettivo full-routing verso `be`.
- Consolidato contesto base in `docs/00-contesto-iniziale.md`:
  - flow target `boxedai -> proxy -> be -> openclaw`
  - conferma servizi `be` (Postgres, MinIO, Keycloak)
  - elenco porte esposte dei servizi coinvolti.

### 2026-03-11 - Baseline confermata (sessioni)

- Confermata implementazione gia esistente della Function BoxedAI `openclaw_session_bridge.py`.
- Confermato comportamento corretto: `user_id + chat_id -> session_key deterministica`, sessione coerente verso `opc`.

### 2026-03-11 - Split BIP upload documenti

- Aggiunto `BIP-002` per intercetto upload Box su `/api/v1/files`.
- Aggiunto `BIP-003` per endpoint upload bridge nel proxy e inject contesto su `opc` via `be`.

### 2026-03-11 - Step tecnico completato (proxy upload bridge)

- Completato primo step implementativo su `proxy`: endpoint multipart bridge verso `be`.
- Endpoint disponibili: `POST /v1/uploads/bridge` e alias `POST /uploads/bridge`.
- Restituzione risposta BE con `bridge_upload_id` per tracciamento.

### 2026-03-11 - Step 1 chiuso (verifica reale)

- Verificato E2E upload su BE remoto tramite proxy:
  - response con `bridge_upload_id` + `upload_id`
  - `status=uploaded`
- Verificato download file dal BE con endpoint `/api/v1/uploads/{upload_id}/download`.
- Step successivo aperto: inject automatico contesto documento su conversazione (`BIP-003`, fase 2).

### 2026-03-11 - Routing completions spostato su BE

- Aggiornato il proxy: `POST /v1/chat/completions` (e alias `/chat/completions`) ora inoltra a `be` (`/v1/chat/completions`) invece di chiamare `opc` direttamente.
- Mantenuto mapping modello configurato nel proxy (`model -> openclaw:<agent_id>`) prima dell'inoltro.
- Inoltro header auth/debug verso BE (`Authorization`, `X-Debug-User`) anche per le completions.
- Esteso lo stesso routing su endpoint OpenAI aggiuntivi:
  - `POST /v1/completions` (alias `/completions`) -> `be /v1/completions`
  - `POST /v1/responses` (alias `/responses`) -> `be /v1/responses`
- Aggiunta compatibilita runtime su `/v1/responses`:
  - se `be` risponde `404 Not Found` (upstream non disponibile), il proxy fa fallback a `be /v1/chat/completions`
  - il proxy traduce la risposta chat in shape `response` minimale per evitare blocchi client.
