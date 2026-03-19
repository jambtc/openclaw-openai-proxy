# BIP-007 - Proxy dinamico `/v1/models` dal BE mantenendo il gateway nel mezzo

> Stato attuale: `🔄 In Esecuzione` - avvio 2026-03-19
> Owner: `gateway + box + be` team

## Contesto

Oggi il gateway espone `/v1/models` in modo statico leggendo `config.agents`.

Conseguenze pratiche:
- i modelli/agenti visibili in Box dipendono da YAML locale (`config.yaml`, `config.contabo.yaml`)
- se gli agenti cambiano lato `be`/OpenClaw, il gateway va aggiornato manualmente
- il mapping statico non scala bene se il modello reale diventa dinamico o per-utente
- se in Box si punta direttamente al `be` per vedere i modelli reali, si perde il reinject documentale provider-side del gateway

Questo ultimo punto e il piu importante: il gateway oggi intercetta correttamente il traffico browser-side (`/api/v1/files`, `/api/v1/chats/new`, `/api/chat/completions`), ma il reinject finale del `public_url` avviene quando Box chiama il provider OpenAI-compatible su `/v1/chat/completions`. Se Box punta direttamente al `be`, quella chiamata bypassa il gateway e il flusso documentale si rompe.

## Problema da risolvere

Dobbiamo ottenere contemporaneamente due proprieta:

1. Box deve vedere i modelli reali e aggiornati del `be`
2. Box deve continuare a parlare con il gateway, non con il `be` diretto

In altri termini:
- il gateway deve restare l'unico endpoint provider configurato in Box
- il gateway deve smettere di essere source-of-truth locale per `/v1/models`
- la source-of-truth dei modelli deve diventare il `be`

## Proposta

Evolvere il gateway in modo che `/v1/models` e `/models` diventino un pass-through controllato verso il `be`.

Comportamento target:
1. Box interroga `gateway /v1/models`
2. il gateway chiama `be /v1/models`
3. il gateway restituisce la lista modelli del `be` con eventuali minime normalizzazioni compatibili con Box
4. Box usa sempre il gateway anche per `/v1/chat/completions`, `/v1/completions`, `/v1/responses`
5. il gateway continua quindi a poter fare:
   - normalize model/agent quando serve
   - inject documentale provider-side
   - policy centralizzate di routing/logging

## Obiettivi

- eliminare la dipendenza operativa da `config.agents` come elenco statico di modelli
- evitare drift tra modelli visibili in Box e modelli realmente supportati dal `be`
- mantenere intatto il flusso documentale `upload -> chat correlation -> provider-side inject`
- supportare in futuro scenari con agenti dinamici o per-utente

## Non-obiettivi

- non cambiare in questa fase il contratto documentale del `be`
- non spostare la logica di auth agent-specific nel frontend Box
- non introdurre discovery complessa multi-backend

## Proposta tecnica

### 1. `/v1/models` proxy-to-BE

Sostituire il listato statico del gateway con una chiamata al `be`:
- `GET /v1/models` -> `be /v1/models`
- `GET /models` -> alias dello stesso comportamento

### 2. Compatibilita shape

Il gateway deve preservare la shape OpenAI-compatible restituita dal `be`.

Se necessario, puo aggiungere solo estensioni non distruttive usate da Box, ma senza alterare gli `id` modello reali se non strettamente necessario.

### 3. Normalizzazione model routing

L'attuale logica del gateway assume una mappa locale `model_id -> agent_id`.

Con modelli dinamici servono due modalita possibili:
- se il modello arriva gia in forma `openclaw:<agentId>` o `agent:<agentId>`, nessuna trasformazione
- se il `be` restituisce alias sintetici, il gateway deve usare una normalizzazione derivata dalla response di `/v1/models`, non da YAML statico

### 4. Fallback operativo

In caso di errore temporaneo del `be /v1/models`:
- il gateway deve restituire errore chiaro (`502`) oppure usare cache breve dell'ultimo elenco valido
- il fallback non deve riportare in vita automaticamente una lista YAML obsoleta senza evidenza operativa

### 5. Configurazione target in Box

Box deve puntare sempre e solo al gateway come provider OpenAI-compatible.

Configurazione corretta:
- Box provider base URL -> gateway
- Caddy pubblico Box -> gateway
- gateway upstream Box -> OpenWebUI
- gateway southbound completions/models -> `be`

Configurazione da evitare:
- Box provider base URL -> `be` diretto

## Impatto atteso

- un solo punto di ingresso logico tra Box e backend AI
- modelli aggiornati automaticamente dal `be`
- nessuna duplicazione manuale di agenti in YAML
- riduzione del rischio di mismatch tra runtime reale e configurazione esposta
- preservazione del reinject documentale senza workaround applicativi fragili

## Rischi

- il `be` oggi potrebbe restituire una lista modelli troppo minimale per il caso d'uso desiderato
- se il `be` non espone metadati sufficienti, il gateway potrebbe dover arricchire leggermente la response
- una cache modelli mal gestita puo introdurre incoerenza temporanea
- va verificato il comportamento di Box se l'elenco modelli cambia durante la sessione

## Criteri di accettazione

- Box punta al gateway come provider OpenAI-compatible
- Box vede in UI la lista modelli ottenuta dal `be` tramite il gateway
- il gateway non dipende piu da `config.agents` per esporre `/v1/models`
- upload file e correlazione chat continuano a funzionare
- il reinject provider-side del `public_url` continua a comparire nei log del gateway
- aggiungere/rimuovere un agente lato `be` si riflette in Box senza modifica YAML nel gateway

## Dipendenze

- `BIP-005` - Pivot architetturale a OpenWebUI Edge Gateway
- `BIP-006` - Piano tecnico Fase 1 Edge Gateway
- allineamento tra shape `/v1/models` del `be` e necessita UI di Box

## Decisione architetturale

Il gateway resta il boundary applicativo tra Box e backend AI.

Quindi:
- la discovery modelli deve arrivare dal `be`
- la decisione di routing, inject, logging e policy deve restare nel gateway

Questa separazione e coerente con il ruolo attuale del progetto come edge gateway, non come semplice reverse proxy passivo.

## Rollout suggerito

1. implementare proxy `/v1/models` -> `be`
2. mantenere temporaneamente il codice YAML solo come fallback disabilitato di default
3. configurare Box provider di nuovo verso il gateway
4. verificare che i log mostrino sia:
   - discover modelli via gateway
   - provider-side inject documentale
5. rimuovere dal config operativo contabo la dipendenza dall'elenco agenti statico

## Avanzamento

### 2026-03-19 - Formalizzazione proposta

- Identificata la causa del mismatch: Box puo vedere i modelli reali del `be` solo puntando direttamente al `be`, ma cosi bypassa il gateway sulla completion provider-side.
- Verificato che oggi il gateway espone `/v1/models` da `config.agents` statico.
- Verificato che il `be` espone gia `/v1/models` OpenAI-compatible.
- Definita la direzione corretta: proxy dinamico `/v1/models` dal `be` mantenendo il gateway come unico provider configurato in Box.

### 2026-03-19 - Implementazione minima gateway

- `GET /v1/models` e `GET /models` del gateway ora inoltrano al `be /v1/models`.
- Il gateway continua ad aggiungere l'estensione non standard `pipelines` usata da Box/OpenWebUI.
- La normalizzazione del campo `model` non fallisce piu sui model id dinamici non presenti in `config.agents`: se il model id non e prefissato ma non corrisponde a un alias statico locale, passa through invariato verso il `be`.
- Gli alias statici esistenti in YAML continuano a funzionare come compatibilita retroattiva.
- Restano da validare in runtime VPS: shape modelli restituita dal `be`, comportamento Box in UI, e conferma che il reinject documentale provider-side torna a comparire nei log quando Box punta di nuovo al gateway.
