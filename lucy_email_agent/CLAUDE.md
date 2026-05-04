# Gmail Morning Digest — Lucy Agent

## O que é

Workflow n8n que roda todo dia às 8h (America/Sao_Paulo), lê a inbox do Gmail, categoriza e resume os e-mails usando o modelo Gemini 2.0 Flash (via script Python centralizado) com a personalidade da Lucy (Cyberpunk Edgerunners), e entrega um digest formatado no Telegram.

---

## Workflow e Arquitetura

- **ID:** `FXeSRs23jMJvIUuj`
- **URL:** `https://n8n.gusstavo42-vps.cloud/workflow/FXeSRs23jMJvIUuj`
- **Status:** Inativo (aguardando credenciais)

O fluxo substituiu nodes pesados de IA (do `@n8n/n8n-nodes-langchain`) por uma chamada otimizada ao script `main.py` via `spawnSync`, garantindo consistência com os outros módulos do repositório.

### Nodes em ordem

```
Schedule (8h)
  → Fetch Unread Inbox Emails   (Gmail: getAll, INBOX + unread)
  → Aggregate All Emails        (agrega itens em lista única)
  → Prepare Emails for AI       (extrai emailId, from, subject, snippet, gmailCategory)
  → Lucy Agent Python Script    (Code Node chama main.py via spawnSync)
  → Send Morning Digest         (Telegram: envia o digest_html)
  → Split Emails for Labeling   (expande o array de e-mails processado)
  → Add Category Label          (Gmail: aplica label baseada no ID retornado)
  → Archive Email               (Gmail: remove INBOX label)
```

---

## Script Python (main.py)

O script `main.py` centraliza a chamada à API do Gemini, a formatação HTML do Telegram e a aplicação da Persona.

### Chamada no n8n

O Code Node "Lucy Agent Python Script" usa o seguinte código:

```javascript
const { spawnSync } = require('child_process');

const emailsPayload = JSON.stringify($json.emails || []);

const proc = spawnSync(
  '/opt/venv/bin/python3',
  ['/home/node/scripts/lucy_email_agent/main.py', '--emails-json', emailsPayload, '-v'],
  { encoding: 'utf8', timeout: 60000 }
);

if (proc.error) throw proc.error;

const result = JSON.parse(proc.stdout.trim());
result._logs = proc.stderr.slice(-3000);
return [{ json: result }];
```

### Output Esperado do Script

O script devolve no `stdout` um JSON estruturado:

```json
{
  "ok": true,
  "timestamp": "2026-05-04T21:17:48.165747+00:00",
  "digest_html": "🕸️ <b>LUCY — Net Scan Matinal</b>\n...",
  "emails": [
    {
      "emailId": "19db1b8e02b9308b",
      "category": "Promotions",
      "priority": "low",
      "summary": "Oferta irrelevante de crédito.",
      "action": "arquivar"
    }
  ]
}
```

---

## Credenciais necessárias

| Serviço | Tipo | Configuração |
|---|---|---|
| **Gmail OAuth2** | Credencial n8n | Necessário para Fetch, Add Label e Archive |
| **Telegram Bot** | Credencial n8n | Necessário para Send Morning Digest |
| **Gemini API** | Variável de Ambiente | `GEMINI_API_KEY` deve estar configurada no `.env` ou nas variáveis do Dokploy |

### Gmail OAuth2 — Google Cloud setup
1. Criar projeto em [console.cloud.google.com](https://console.cloud.google.com)
2. Ativar **Gmail API**
3. Configurar **OAuth Consent Screen** (External, adicionar seu Gmail como test user)
4. Criar **OAuth Client ID** (Web application)
5. Redirect URI: `https://n8n.gusstavo42-vps.cloud/rest/oauth2-credential/callback`
6. No n8n: Credentials → New → Gmail OAuth2

### Gemini API
O script usa requisições HTTP REST direto para a API do Google (não usa o node nativo do n8n).
A chave deve ser gerada no [Google AI Studio](https://aistudio.google.com/apikey) e injetada no VPS.

### Telegram Bot
1. Falar com [@BotFather](https://t.me/BotFather) → `/newbot` → copiar token
2. No n8n: Credentials → New → Telegram API → colar token
3. Substituir o placeholder do `Chat ID` no node **Send Morning Digest**. (Para pegar o ID: `/getUpdates` via API).

---

## Gmail Labels

O workflow aplica labels por categoria após o envio. As labels precisam existir no Gmail e ter os IDs mapeados no node **Add Category Label**.

### Categorias do Script → Labels
`Work` | `Finance` | `Shopping` | `Travel` | `Newsletter` | `Social` | `Promotions` | `Personal` | `Other`

*Nota:* `Promotions` costuma ser `CATEGORY_PROMOTIONS` por padrão no Gmail.

---

## Persona — Lucy

> *"Você é Lucy — uma netrunner fria e eficiente de Night City. Você vasculha a rede toda manhã, filtra o ruído e entrega só o que importa — sem drama, sem enrolação."*

- **Summaries**: super curtos, 1 linha máxima, sem floreio.
- **Overview**: irônico e lacônico.
- **Inbox zerada**: O script detecta automaticamente se o input for vazio e retorna `🌙 Inbox limpa. Até parece Night City numa segunda de manhã.` sem gastar token da IA.

---

## Checklist de ativação

- [ ] Configurar credencial Gmail OAuth2 no n8n
- [ ] Configurar `GEMINI_API_KEY` no Dokploy (variáveis do container)
- [ ] Configurar credencial Telegram Bot no n8n e setar o Chat ID
- [ ] Substituir/Atualizar o node de IA nativo pelo script Python via Code node
- [ ] Criar labels no Gmail se ainda não existirem
- [ ] Ativar o workflow no n8n