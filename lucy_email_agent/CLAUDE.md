# Gmail Morning Digest — Lucy Agent

## O que é

Workflow n8n que roda todo dia às 8h (America/Sao_Paulo), lê a inbox do Gmail, categoriza e resume os e-mails usando o modelo Gemini 2.0 Flash (via script Python centralizado) com a personalidade da Lucy (Cyberpunk Edgerunners), e entrega um digest formatado no Telegram.

---

## Workflow e Arquitetura

- **ID:** `FXeSRs23jMJvIUuj`
- **URL:** `https://n8n.gusstavo42-vps.cloud/workflow/FXeSRs23jMJvIUuj`
- **Status:** Inativo (aguardando credenciais)

## A Arquitetura do Workflow

A inteligência e o processamento de e-mails estão inteiramente isolados no script Python `main.py`. O n8n age apenas como orquestrador e entregador de mensagens.

1.  **Schedule Trigger**: Acorda o fluxo (ex: 8:00 AM).
2.  **Lucy Agent Python Script (Code Node)**: Executa o `/home/node/scripts/lucy_email_agent/main.py`.
    * O script se conecta ao IMAP do Gmail.
    * Busca e-mails marcados como `UNREAD`.
    * Consulta o Gemini 2.0 Flash para classificação e resumo.
    * Gera a mensagem formatada para o Telegram.
    * Retorna os dados estruturados no stdout.
3.  **Send Morning Digest (Telegram Node)**: Envia o texto de `{{ $json.digest_html }}`.
4.  **Split Emails for Labeling (Code Node)**: Itera sobre o array `emails` devolvido pelo Python, gerando itens separados para cada e-mail com a categoria resolvida para a ID de label do Gmail.
5.  **Gmail (Add Category Label)**: Aplica as labels no e-mail real.
6.  **Gmail (Archive Email)**: Remove o e-mail da Inbox, finalizando o loop.

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

- ### 2. Configurar Variáveis de Ambiente

O container n8n precisa ter acesso às credenciais para que o script `main.py` consiga ler o Gmail e se comunicar com o Gemini. Adicione ao `.env` do seu repositório:

```env
# Gemini
GEMINI_API_KEY=sua_chave_do_google_ai_studio
GEMINI_MODEL=gemini-2.0-flash

# Gmail (IMAP via Python)
GMAIL_USERNAME=seu_email@gmail.com
GMAIL_APP_PASSWORD=senha_de_app_de_16_letras
```

> **Como gerar a GMAIL_APP_PASSWORD (Senha de App):**
> 1. Acesse sua Conta Google -> Segurança.
> 2. Certifique-se de que a **Verificação em Duas Etapas** está ativa.
> 3. Busque por "Senhas de App" (App Passwords).
> 4. Crie uma senha com o nome "Lucy Agent".
> 5. Copie a senha de 16 letras gerada (sem espaços) e cole no `.env`.
- [ ] Configurar credencial Telegram Bot no n8n e setar o Chat ID
- [ ] Substituir/Atualizar o node de IA nativo pelo script Python via Code node
- [ ] Criar labels no Gmail se ainda não existirem
- [ ] Ativar o workflow no n8n