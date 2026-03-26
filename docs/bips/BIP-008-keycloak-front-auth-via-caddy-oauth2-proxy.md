# BIP-008 - Keycloak-first auth via Caddy + oauth2-proxy + trusted headers

> Stato attuale: `📋 Proposta` - creazione 2026-03-26
> Owner: `gateway + box + infra` team

## Contesto

L'entrypoint pubblico di BoxedAI non e piu Open WebUI diretto ma il gateway applicativo.

Flusso attuale semplificato:
- browser -> Caddy -> gateway -> Open WebUI
- per le route AI/documenti: gateway -> be -> opc

Il requisito di prodotto e avere Keycloak come unico punto di autenticazione visibile all'utente, evitando la login page nativa di Open WebUI.

In questo scenario non basta ragionare su Open WebUI come applicazione isolata. Il vero boundary pubblico del sistema e il gateway, quindi l'autenticazione deve stare davanti al gateway stesso.

## Proposta

Adottare un modello **Keycloak-first** con autenticazione a monte tramite:
- `Caddy` come reverse proxy pubblico
- `oauth2-proxy` come authenticating proxy OIDC verso Keycloak
- `Open WebUI` configurato in trusted-header mode
- `gateway` mantenuto come front-door applicativa del sistema

Schema target:

```text
Browser
   |
   v
Caddy
   |
   +--> oauth2-proxy <--> Keycloak
   |
   v
openwebui-edge-gateway
   |
   v
Open WebUI
```

Flusso documentale e AI invariato:

```text
Browser -> Caddy -> gateway -> Open WebUI
gateway -> be -> opc
```

## Obiettivo

Ottenere una UX di login completamente centrata su Keycloak:
- l'utente non deve atterrare sulla login page di Open WebUI
- l'identita deve essere verificata prima che la richiesta raggiunga il gateway/Open WebUI
- Open WebUI deve ricevere un'identita gia autenticata tramite header fidati

## Decisione architetturale

Il gateway resta il boundary applicativo del sistema.

Quindi:
- `Caddy` protegge il dominio pubblico Box/gateway
- `oauth2-proxy` esegue il flow OIDC con Keycloak
- il gateway continua a orchestrare routing Box / be / opc
- Open WebUI non avvia direttamente il login OIDC, ma consuma trusted headers

Questa scelta e coerente con l'architettura attuale, che ha gia spostato la front-door pubblica dal solo Open WebUI al gateway.

## Componenti coinvolti

### 1. Caddy

Ruolo:
- reverse proxy pubblico TLS terminator
- enforcement dell'autenticazione a monte tramite `oauth2-proxy`
- inoltro delle richieste autenticate al gateway

Nota:
- in questa architettura `Caddy` sostituisce il ruolo spesso mostrato negli esempi con `Nginx`
- non serve introdurre `Nginx` se in produzione e gia presente `Caddy`

### 2. oauth2-proxy

Ruolo:
- client OIDC verso Keycloak
- gestione sessione/cookie utente lato proxy
- forwarding degli header identita verso upstream autenticato

### 3. Keycloak

Ruolo:
- Identity Provider centrale
- login page unica visibile all'utente
- gestione policy IAM, MFA, realm, gruppi e claim

### 4. Gateway

Ruolo invariato:
- intercetto upload, chat, completions, ws, pass-through Box
- boundary applicativo tra browser e servizi interni

Nuovo requisito:
- deve preservare correttamente gli header trusted ricevuti dal layer auth verso Open WebUI
- deve scartare eventuali header spoofati provenienti dal browser pubblico

### 5. Open WebUI

Ruolo:
- applicazione UI e orchestrazione FE/BE interna
- trusted-header consumer per login/auto-provisioning utente

Configurazione attesa:
- `WEBUI_AUTH_TRUSTED_EMAIL_HEADER`
- eventuali altri trusted header coerenti con il proxy auth scelto
- disabilitazione del login locale se il rollout lo consente

## Flusso richieste target

1. L'utente apre `https://boxedai-...`
2. `Caddy` verifica la sessione tramite `oauth2-proxy`
3. Se non autenticato, il browser viene rediretto a Keycloak
4. Dopo il login, Keycloak rimanda a `oauth2-proxy`
5. `oauth2-proxy` ristabilisce la sessione e passa gli header trusted a monte
6. `Caddy` inoltra la richiesta autenticata al gateway
7. Il gateway inoltra a Open WebUI mantenendo gli header trusted previsti
8. Open WebUI effettua auto-login / auto-registration usando l'header trusted

## Trust boundary

Questo e il punto piu delicato della soluzione.

Regola fondamentale:
- **nessun header trusted deve poter arrivare direttamente dal browser pubblico a Open WebUI**

Conseguenze pratiche:
- solo `oauth2-proxy` / `Caddy` devono poter impostare gli header trusted
- il gateway deve ripulire o sovrascrivere eventuali header identita provenienti dall'esterno
- Open WebUI deve essere raggiungibile solo da rete interna / overlay controllata
- non deve esistere un accesso pubblico alternativo a Open WebUI che bypassi `Caddy` e `gateway`

## Vantaggi attesi

- esperienza login Keycloak-first reale
- eliminazione pratica della login page Open WebUI dal percorso utente
- governance IAM centralizzata
- coerenza con architettura gateway-first gia adottata
- migliore separazione tra autenticazione, routing applicativo e logica documentale

## Rischi

- configurazione piu articolata rispetto all'OIDC nativo Open WebUI
- rischio sicurezza se gli header trusted non sono isolati correttamente
- troubleshooting piu complesso per callback, cookie e redirect cross-service
- rollout VPS da eseguire con attenzione per evitare lockout dell'accesso

## Criteri di accettazione

- l'utente arriva al dominio BoxedAI e viene mandato direttamente a Keycloak se non autenticato
- la login page nativa di Open WebUI non fa piu parte del flusso standard
- dopo il login, l'utente entra in BoxedAI senza ulteriore login locale
- il gateway continua a funzionare come front-door per upload/completions/document flow
- Open WebUI riceve identita valida via trusted headers
- gli header trusted non sono spoofabili dal browser
- Open WebUI non e esposto pubblicamente in modo diretto

## Configurazione target ad alto livello

### Keycloak

- client OIDC dedicato a `oauth2-proxy`
- redirect URI verso l'endpoint callback del proxy auth
- claim email coerente e stabile

### oauth2-proxy

- provider `keycloak-oidc`
- issuer URL Keycloak
- client id / secret
- cookie session sicuri
- forwarding header identita verso upstream

### Caddy

- protezione del virtual host BoxedAI tramite `oauth2-proxy`
- proxy upstream verso `openwebui-edge-gateway`
- eventuale gestione esplicita degli header forwarded / auth

### Gateway

- pass-through corretto degli header trusted verso Open WebUI
- hardening contro spoofing header da lato pubblico

### Open WebUI

- trusted-header auth abilitata
- login locale ridotto o disabilitato
- eventuale auto-registration abilitata se coerente con il dominio aziendale

## Non-obiettivi

- non definire qui il dettaglio finale di ogni singolo parametro env
- non sostituire il gateway con il proxy auth
- non cambiare il flow documentale del gateway
- non introdurre provider auth multipli nel primo rollout

## Rollout suggerito

1. preparare stack `Caddy + oauth2-proxy + Keycloak + gateway + Open WebUI` in staging
2. validare callback, cookie, logout, redirect e trusted headers
3. verificare che upload/chat/completions continuino a passare dal gateway
4. chiudere l'accesso pubblico diretto a Open WebUI
5. rollout progressivo in VPS con piano di fallback operativo

## Dipendenze

- disponibilita di `Caddy` come reverse proxy pubblico della VPS
- disponibilita di `Keycloak` operativo e raggiungibile
- disponibilita di `oauth2-proxy` nel deployment
- verifica del forwarding headers nel gateway

## Avanzamento

### 2026-03-26 - Formalizzazione proposta

- Scelta architetturale orientata solo alla soluzione B: Keycloak-first davanti al gateway.
- Esclusa dal BIP la soluzione OIDC nativa Open WebUI, perche non soddisfa il requisito di UX completamente centrata su Keycloak.
- Confermato che in produzione si usa `Caddy`, quindi la soluzione proposta deve essere espressa in termini di `Caddy + oauth2-proxy`, non `Nginx`.
- Confermato che il boundary pubblico reale del sistema e il gateway, quindi il layer auth va posto davanti al gateway e non pensato solo davanti a Open WebUI.
