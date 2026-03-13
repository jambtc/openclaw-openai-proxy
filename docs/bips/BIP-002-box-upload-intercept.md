# BIP-002 - Box upload intercept (`/api/v1/files`) verso proxy

> Stato attuale: `🔄 In Esecuzione` - 2026-03-11
> Owner: `box + proxy` team

## Contesto

Nel frontend BoxedAI, il click sul pulsante `+` usa upload interno su:

- `POST /api/v1/files/?process=true`
- `content-type: multipart/form-data`

Dallo studio del codice (`backend/open_webui/routers/files.py`) risulta che questo flow:
- salva il file su storage locale Box
- crea record file interno Box
- opzionalmente processa il file lato retrieval

Questo percorso non passa dai filtri chat usati per le completions, quindi non e intercettabile con la sola Filter Function chat.

## Proposta

Aggiungere un intercetto obbligatorio del flow `POST /api/v1/files` (non best-effort), con due opzioni implementative:

1. Fork Box (FE/BE) e reroute nativo della route upload.
2. Reverse proxy davanti a Box che intercetta `/api/v1/files*` e delega a bridge compatibile.

In entrambi i casi:
- il documento non deve finire nello storage Box del flow target
- il caricamento deve passare da `proxy -> be`
- il FE deve continuare a ricevere shape compatibile con API files Box

Dettaglio operativo minimo:

1. Intercettare il multipart in ingresso e costruire payload bridge verso proxy con file + metadata minimi.
2. Inviare il documento al proxy su endpoint dedicato (nuovo), ad esempio:
   - `POST /v1/uploads/bridge`
3. Trasmettere metadata necessari alla correlazione sessione:
   - `box_user_id`
   - `box_chat_id` (se disponibile)
   - `box_file_id`
   - filename/content_type/size
4. Restituire al FE response compatibile (`id`, `url`, `status`, `meta`) e gestire `process/status` in modo coerente.

## Impatto atteso

- Tutti i documenti caricati da Box vengono propagati nel flow `box -> proxy -> be -> opc`.
- Si riduce il gap attuale in cui il documento resta solo nel dominio Box.
- Preparazione per context injection automatica su stessa sessione.

## Rischi

- Aumento latenza upload lato Box se relay sincrono.
- Fallimenti di rete proxy/be durante upload.
- Metadata incompleti (mancanza `chat_id`) che impediscono collegamento sessione.

## Criteri di accettazione

- Ogni upload su `POST /api/v1/files/?process=true` viene intercettato e instradato a `proxy`.
- Proxy riceve multipart valido e risponde con tracking id.
- Il file non viene persistito nello storage documentale Box per il flow target.
- FE upload non regressa (stessa UX, stessi campi base risposta).
- Stato relay tracciabile in log con correlazione `box_file_id`.

## Dipendenze

- Definizione endpoint upload bridge nel proxy (BIP-003).
- Contratto payload/response proxy-be condiviso.

## Avanzamento

### 2026-03-11 - Analisi iniziale

- Confermato endpoint upload reale di Box: `/api/v1/files/?process=true`.
- Confermato che il flusso e gestito nel backend Box (`routers/files.py`) e non dai filtri chat.
- Definita proposta di hook relay verso proxy.

### 2026-03-13 - Cambio requisito (hard intercept)

- Requisito confermato: il file non deve terminare in Box, deve passare `box -> proxy -> be`.
- Decisione: intercetto route upload non ottenibile con sola Function; serve fork Box o reverse proxy su `/api/v1/files*`.
- BIP portata in esecuzione.
