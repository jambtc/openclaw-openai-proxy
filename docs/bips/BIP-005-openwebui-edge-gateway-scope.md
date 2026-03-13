# BIP-005 - Pivot architetturale a OpenWebUI Edge Gateway

> Stato attuale: `🔄 In Esecuzione` - avvio 2026-03-13
> Owner: `proxy + box + be` team

## Contesto

Il progetto e nato come OpenAI-compatible proxy per inoltrare completions verso BE/OpenClaw.

Nuovo requisito prodotto:
- intercettare il flusso upload Box su `POST /api/v1/files`
- evitare che il documento resti nel dominio Box
- far transitare i documenti nel dominio RAG (`proxy -> be`)

Questo requisito supera il perimetro del solo proxy OpenAI e richiede controllo route HTTP Box.

## Perche il modello attuale non basta

- Le Function Box operano nel flusso chat/completions (`inlet/outlet`), non sui router HTTP files.
- Quindi una Function (anche avanzata) non puo sostituire da sola `/api/v1/files`.
- Serve un componente L7 davanti a Box per routing applicativo e trasformazioni payload.

## Proposta

Evolvere `openclaw-openai-proxy` in **OpenWebUI Edge Gateway**
(nome repository target suggerito: `openwebui-edge-gateway`).

Ruolo del gateway:
- front-door API per Box FE
- intercetto selettivo di route sensibili (`/api/v1/files*` prioritario)
- pass-through trasparente delle route non coinvolte
- orchestrazione verso BE per upload/metadata/doc URLs
- arricchimento pre-completion quando necessario

## Scope incrementale

Fase 1 (prioritaria):
1. intercetto `/api/v1/files*`
2. upload instradato a `proxy -> be`
3. risposta compatibile verso FE Box

Fase 2:
1. lookup `GET /api/v1/uploads` con metadata correlati
2. riscrittura messaggio con `public_url` prima della completion

Fase 3 (opzionale):
1. estendere ad altre route Box critiche
2. policy centralizzate (auth, ratelimit, audit)

## Impatto atteso

- allineamento completo al requisito documentale RAG
- riduzione disallineamento tra upload UI e backend documentale effettivo
- governance centralizzata di routing e trasformazioni

## Rischi

- maggiore complessita operativa (edge routing + compatibilita API Box)
- rischio regressione FE se la shape risposta upload non e compatibile
- gestione streaming/SSE/WebSocket da preservare nel pass-through

## Criteri di accettazione

- upload Box intercettato su `/api/v1/files*` e non persistito nel dominio documentale Box target
- upload effettivo visibile su BE con metadata correlati
- FE non mostra regressioni nel workflow upload/process/status
- completions ricevono riferimenti documento (`public_url`) coerenti

## Dipendenze

- `BIP-002` (intercetto route upload)
- `BIP-004` (lookup upload + rewrite pre-completion)
- allineamento deployment DNS/reverse-proxy per mettere il gateway davanti a Box

## Avanzamento

### 2026-03-13 - Formalizzazione scope

- Confermato requisito hard-intercept su `/api/v1/files`.
- Confermato limite strutturale Function-only.
- Definito pivot ufficiale verso Edge Gateway.
