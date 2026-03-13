# BoxedAI Improvements Proposals (BIP)

> Sezione: BIP - Proposte di miglioramento BoxedAI stack
> Formato: ogni BIP e un documento autonomo con contesto, proposta, impatto, criteri di accettazione e avanzamento

Le BIP servono a tracciare in modo operativo:
- cosa va fatto (piano, criteri, dipendenze)
- cosa e gia stato fatto (avanzamento con date)

---

## Indice

Legenda stato: `📋 Proposta` - `🔄 In Esecuzione` - `✅ Completata` - `🚫 Bloccata/Sospesa`

| BIP | Titolo | Categoria | Priorita | Stato |
| --- | ------ | --------- | -------- | ----- |
| [BIP-001](BIP-001-proxy-full-routing-be.md) | Proxy full-routing verso BE (completions + documenti) | Architettura / Integrazione | Alta | 🔄 In Esecuzione |
| [BIP-002](BIP-002-box-upload-intercept.md) | Box upload intercept (`/api/v1/files`) verso proxy | Integrazione Box / Proxy | Alta | 🔄 In Esecuzione |
| [BIP-003](BIP-003-proxy-upload-bridge-be-opc.md) | Proxy upload API + bridge BE + context inject OPC | Integrazione Proxy / BE / OPC | Alta | 🚫 Sospesa |
| [BIP-004](BIP-004-box-file-metadata-correlation-without-box-code.md) | Correlazione file/chat con lookup upload prima di completions | Integrazione Box Function / Metadata | Alta | 🔄 In Esecuzione |
| [BIP-005](BIP-005-openwebui-edge-gateway-scope.md) | Pivot architetturale a OpenWebUI Edge Gateway | Architettura / Scope | Alta | 🔄 In Esecuzione |

---

## Regole operative

- Nuove proposte: creare file `BIP-XXX-nome.md` con numerazione progressiva (`BIP-002`, `BIP-003`, ...).
- Ogni aggiornamento tecnico rilevante va registrato in `## Avanzamento` dentro il BIP.
- Quando cambia lo stato del BIP, aggiornare sia il file BIP sia questa tabella indice.

---

## Template minimo BIP

Ogni BIP deve contenere almeno:
- Contesto
- Proposta
- Impatto atteso
- Rischi
- Criteri di accettazione
- Dipendenze
- Avanzamento
