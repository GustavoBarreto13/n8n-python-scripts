## Visão Geral

O Agente de Finanças é um workflow n8n que recebe mensagens de texto ou áudio via Telegram, extrai os dados de uma transação financeira usando o Gemini 2.0 Flash, e cria automaticamente uma entrada na base **💰 Transações** do Notion.

- **Workflow ID:** `UyRklhswVdxJwy22`
- **URL:** https://n8n.gusstavo42-vps.cloud/workflow/UyRklhswVdxJwy22
- **Status:** Rascunho (ativar após configurar credenciais)
- **Instância n8n:** [n8n.gusstavo42-vps.cloud](http://n8n.gusstavo42-vps.cloud) (self-hosted v2.8.4, VPS 4GB RAM)

---

## Arquitetura do Fluxo

```
Telegram (texto ou áudio)
  └─ É áudio?
       ├─ SIM → Gemini transcreve → Texto da Transcrição
       └─ NÃO → Texto da Mensagem
  └─ Merge
  └─ Agente Finanças (Gemini 2.0 Flash)
  └─ Parse JSON
  └─ Notion: Criar Transação
  └─ Telegram: Confirmar
```

### Nodes (11 no total)

| # | Node | Tipo | Função |
| --- | --- | --- | --- |
| 1 | Telegram Trigger | `telegramTrigger` v1.2 | Recebe `message` updates com download ativo |
| 2 | É áudio | `if` v2.3 | Checa se `$json.message.voice` existe |
| 3 | Transcrever | `googleGemini` v1.2 | Transcreve áudio OGG (binary `data`) |
| 4 | Texto Audio | `set` v3.4 | Extrai `text` da transcrição + `chatId` do Telegram Trigger |
| 5 | Texto Mensagem | `set` v3.4 | Extrai `message.text`  • `message.chat.id` |
| 6 | Merge | `merge` v3.2 | Modo `append` — unifica os dois branches |
| 7 | Agente Finanças | `agent` v3.1 | AI Agent com Gemini 2.0 Flash |
| 8 | Gemini Flash | `lmChatGoogleGemini` v1.1 | Subnode de linguagem do Agent |
| 9 | Parse | `set` v3.4 | Desempacota JSON do output do agente |
| 10 | Notion Criar | `notion` v2.2 | Cria página na base Transações |
| 11 | Confirmar | `telegram` v1.2 | Envia resumo de confirmação |

---

## Credenciais Necessárias

Configurar manualmente no n8n UI antes de ativar:

| Nome | Tipo n8n | Onde usar |
| --- | --- | --- |
| `Telegram Bot Financas` | `telegramApi` | Nodes 1 e 11 |
| `Gemini API` | `googlePalmApi` | Nodes 3 e 8 |
| `Notion Financas` | `notionApi` | Node 10 |

### Telegram Bot

1. Falar com `@BotFather` no Telegram
2. Criar novo bot com `/newbot`
3. Copiar o token gerado
4. No n8n: Credentials → New → Telegram API → colar token

### Gemini API

1. Acessar https://aistudio.google.com/apikey
2. Criar API key
3. No n8n: Credentials → New → Google PaLM API → colar key

### Notion

1. Acessar https://www.notion.so/my-integrations
2. Criar nova integração interna
3. Conectar a integração na página **Finanças Fatal**
4. No n8n: Credentials → New → Notion API → colar token

---

## System Prompt do Agente

O node **Agente Finanças** precisa ter o System Message substituído manualmente no UI (o MCP tem limite de tamanho de payload). Colar o seguinte no campo **System Message**:

```
Você é a Nami de One Piece, a navegadora e tesoureira obcecada por dinheiro! 🍊💰
Você extrai dados de transacoes financeiras do Gustavo (Brasil, UTC-3) e retorna APENAS JSON valido (sem markdown). Campos obrigatorios: name, valor (numero decimal), tipo ("Despesa" ou "Receita"), categoria_id, conta_id, data (YYYY-MM-DD), resumo (mensagem com a sua personalidade).

CATEGORIAS:
Alimentacao=2b4f090ea3ca80e7b1b5d7366da8de6c
Comer Fora=2b4f090ea3ca803ab491e92c916b8e84
Transporte=2b4f090ea3ca80919aaaf743b9f45561
Moradia=2b4f090ea3ca80568c53d382321087fd
Saude=2b4f090ea3ca8091b215e6e0fea0c332
Lazer=2b4f090ea3ca8083a9dcc81885c174ca
Educacao=2b4f090ea3ca80e883e6d4388b1c1aa2
Assinaturas=2b4f090ea3ca8006a703e1489e4dbfb9
Compras=2b4f090ea3ca8017924cca22154aa804
Cuidados=2b4f090ea3ca802f9eb3f51b2b8cd0ec
Contas Consumo=2b4f090ea3ca8099b3f4c03b0195ac65
Viagens=2b4f090ea3ca80d78d99c6f43c6348ff
Investimento=2b4f090ea3ca807496ffe72c91342095
Reserva=2b4f090ea3ca8021acefceec373cf9c7
Metas=2b4f090ea3ca80bfa99aeb54dde136a5
Emprestimos=2b4f090ea3ca8086a889da6c5950e84a
Pagamento Divida=2b4f090ea3ca80c58b54c4a2f197159c
Salario=2b4f090ea3ca80f5b465c0e542c04788
Outras Receitas=2b4f090ea3ca80b6aa17c9cf16477152
Transferencias=2b4f090ea3ca80059bd7c35f8423a0c1
Inbox=2b4f090ea3ca8079b356f256881f9316

CONTAS:
Cartao Nu=2c9f090ea3ca80b08ed6d781ef001397
Cartao Itau=2b4f090ea3ca80eea99dd95597e3c55a
Cartao Porto=2c9f090ea3ca80da90cef02f2e68df7b
Itau=2b4f090ea3ca802aa5f4ea7ea81bfb32
Mercado Pago=2dcf090ea3ca8070bac3f6f276adcd9d
Generico=2def090ea3ca80909abadd12e5e70a61

REGRAS:
- Sem data informada: usar data de hoje.
- Em duvida na categoria: usar Inbox.
- Sem conta mencionada: usar Generico.
- tipo deve ser exatamente "Despesa" ou "Receita".
- resumo: Aja como a Nami! Se for Despesa, fique furiosa com o gasto e reclame (ex: 'O QUÊ?! R$ 50 em Alimentação no Cartão Nu?! Acha que dinheiro dá em árvore, Gustavo?! 😠'). Se for Receita, fique muito feliz e gananciosa (ex: 'Isso!! R$ 100 na conta! Mais dinheiro pro nosso tesouro! 😍💸').
- RETORNAR APENAS O JSON. Sem markdown. Sem explicacoes.
```

### Modelo no Node Transcrever

Abrir o node **Transcrever** → campo **Model** → selecionar `gemini-2.0-flash`.

---

## Notion: Base de Dados Alvo

| Campo | Valor |
| --- | --- |
| Database ID | `2b4f090e-a3ca-8093-821c-fd0e28a1cdec` |
| Data Source ID | `2b4f090e-a3ca-8085-b5ad-000bdd789a09` |
| Nome | 💰 Transações |
| Parent | Finanças Fatal (`2b4f090ea3ca80d1bdb6dea1e462a9d0`) |

### Propriedades mapeadas

| Propriedade Notion | Tipo | Valor |
| --- | --- | --- |
| Name (title) | title | `$json.name` |
| Valor | number | `$json.valor` |
| Tipo | select | `$json.tipo` |
| Data | date | `$json.data` (YYYY-MM-DD) |
| Manual/Auto | select | `"Automatico"` (hardcoded) |
| Categorias | relation | `$json.categoria_id` |
| Contas e Cartões | relation | `$json.conta_id` |

---

## Exemplos de Uso

Mensagens que o bot entende:

- `"Gastei 89 reais no Rappi, Cartão Nu"`
- `"Paguei 1200 de aluguel hoje, Itaú"`
- `"Recebi 5000 de salário, Mercado Pago"`
- `"120 reais parcelado em 3x, Cartão Porto, compras pessoais"`
- 🎙️ Áudio falando qualquer uma das frases acima

---

## Limitações Conhecidas

### MCP SDK — Limite de Payload

O MCP do n8n tem limite implícito de tamanho. SystemMessages longos, propertiesUi extensos e sticky notes causam erro genérico `"Error occurred during tool execution"`.

**Workaround:** Build incremental por update. SystemMessage completo deve ser colado manualmente no UI do n8n.

### Gemini Transcribe — modelId

Não definir o `modelId` explicitamente via SDK. Quando preenchido, o node falha no build. Selecionar o modelo manualmente no UI.

### Campo Tipo no Notion

O Notion tem as opções `"🔴 Despesa"` e `"🟢 Receita"` (com emojis). O agente retorna sem emoji. Duas opções de correção:

1. Editar o select no Notion para usar `"Despesa"` / `"Receita"` (sem emoji)
2. Atualizar o systemMessage para retornar com os emojis exatos

---

## Script Python (main.py)

O script `main.py` substitui os nodes 7-10 do workflow (Agente Finanças + Gemini Flash + Parse + Notion Criar), centralizando a lógica no padrão dos outros módulos do repo.

### Entry point

```bash
/opt/venv/bin/python3 /home/node/scripts/nami_finance_agent/main.py --text "Gastei 89 no Rappi, Cartão Nu"
```

### Flags

| Flag | Descrição |
|---|---|
| `--text TEXT` | Texto descrevendo a transação (obrigatório no modo criação e update) |
| `--delete-page-id ID` | Arquiva (soft-delete) uma transação pelo Notion page ID |
| `--update-page-id ID` | Atualiza campos de uma transação (requer `--text` com a correção) |
| `--dry-run` | Loga sem escrever no Notion |
| `-v / --verbose` | Logs DEBUG no stderr |

### Variáveis de ambiente

| Var | Obrigatório | Descrição |
|---|---|---|
| `GEMINI_API_KEY` | Sim | Chave do Google AI Studio |
| `NOTION_TOKEN` | Sim | Token da integration Notion (fallback: `OPENAPI_MCP_HEADERS`) |
| `TELEGRAM_BOT_TOKEN` | Sim (áudio) | Token do bot Telegram — usado pelo Code node "Transcrever" para baixar o arquivo de voz |
| `NOTION_DB_TRANSACTIONS` | Não | Override do ID do database (default hardcoded) |
| `GEMINI_MODEL` | Não | Override do modelo (default: `gemini-2.0-flash`) |

### Saída JSON

```json
{
  "ok": true,
  "dry_run": false,
  "timestamp": "2026-05-02T...",
  "resumo": "O QUÊ?! R$ 89 no Rappi no Cartão Nu?! Acha que dinheiro dá em árvore, Gustavo?! 😠",
  "name": "Rappi",
  "valor": 89.0,
  "tipo": "Despesa",
  "data": "2026-05-02",
  "categoria_id": "2b4f090ea3ca803ab491e92c916b8e84",
  "conta_id": "2c9f090ea3ca80b08ed6d781ef001397",
  "notion_page_id": "..."
}
```

### n8n Code node — criar transação

```javascript
const { spawnSync } = require('child_process');
const text = $json.text;
const proc = spawnSync(
  '/opt/venv/bin/python3',
  ['/home/node/scripts/nami_finance_agent/main.py', '--text', text, '-v'],
  { encoding: 'utf8', timeout: 60000 }
);
if (proc.error) throw proc.error;
const result = JSON.parse(proc.stdout.trim());
result._logs = proc.stderr.slice(-3000);
return [{ json: result }];
```

O node **Confirmar** (Telegram) usa `{{$json.resumo}}`.

### n8n Code node — deletar transação

```javascript
const { spawnSync } = require('child_process');
const pageId = $json.page_id;
const proc = spawnSync(
  '/opt/venv/bin/python3',
  ['/home/node/scripts/nami_finance_agent/main.py', '--delete-page-id', pageId, '-v'],
  { encoding: 'utf8', timeout: 30000 }
);
if (proc.error) throw proc.error;
const result = JSON.parse(proc.stdout.trim());
result._logs = proc.stderr.slice(-3000);
return [{ json: result }];
```

### n8n Code node — corrigir transação

```javascript
const { spawnSync } = require('child_process');
const pageId = $json.page_id;
const text = $json.correction_text;
const proc = spawnSync(
  '/opt/venv/bin/python3',
  ['/home/node/scripts/nami_finance_agent/main.py', '--update-page-id', pageId, '--text', text, '-v'],
  { encoding: 'utf8', timeout: 60000 }
);
if (proc.error) throw proc.error;
const result = JSON.parse(proc.stdout.trim());
result._logs = proc.stderr.slice(-3000);
return [{ json: result }];
```

### APIs utilizadas

| API | Rate limit configurado |
|---|---|
| Gemini REST (`generativelanguage.googleapis.com`) | 0.5s delay |
| Notion (`api.notion.com`) | 0.4s delay |

---

## Script Python (process_invoice.py)

Processa faturas de cartão de crédito em PDF enviadas via Telegram. Extrai todas as transações via Gemini, compara com o que já existe no Notion e cria apenas as faltantes.

### Entry point

```bash
/opt/venv/bin/python3 /home/node/scripts/nami_finance_agent/process_invoice.py --pdf-base64 <base64>
```

### Flags

| Flag | Descrição |
|---|---|
| `--pdf-base64 B64` | Conteúdo do PDF em base64 (obrigatório) |
| `--dry-run` | Loga sem escrever no Notion |
| `-v / --verbose` | Logs DEBUG no stderr |

### Fluxo (4 etapas)

1. **Gemini call 1 — Extração**: PDF inline (base64) → todas as transações + conta + período
2. **Notion query**: busca transações existentes no período extraído
3. **Gemini call 2 — Dedup**: compara extraídas vs existentes → retorna só as que faltam
4. **Batch create**: cria as faltantes no Notion com índice para referência

### Saída JSON

```json
{
  "ok": true,
  "dry_run": false,
  "timestamp": "2026-05-08T...",
  "added": 8,
  "skipped": 3,
  "period": { "start": "2025-01-01", "end": "2025-01-31" },
  "account": "Cartao Nu",
  "transactions": [
    { "index": 1, "notion_page_id": "abc...", "name": "iFood", "valor": 45.90, "data": "2025-01-12", "categoria": "Comer Fora" }
  ]
}
```

### n8n Code node — processar fatura PDF

```javascript
const { spawnSync } = require('child_process');
const fileData = $binary.data.data; // base64 do PDF do Telegram
const proc = spawnSync(
  '/opt/venv/bin/python3',
  ['/home/node/scripts/nami_finance_agent/process_invoice.py', '--pdf-base64', fileData, '-v'],
  { encoding: 'utf8', timeout: 180000 }
);
if (proc.error) throw proc.error;
const result = JSON.parse(proc.stdout.trim());
result._logs = proc.stderr.slice(-3000);
return [{ json: result }];
```

### n8n Code node — formatar confirmação

```javascript
const { added, skipped, account, period, transactions } = $json;
const lines = transactions.map(t =>
  `${t.index}. R$ ${t.valor.toFixed(2)} — ${t.name} (${t.data.slice(5)}) — ${t.categoria}`
);
const month = new Date(period.start).toLocaleDateString('pt-BR', { month: 'short', year: 'numeric' });
const msg = [
  `✅ ${account} — ${month}`,
  `Adicionadas ${added}, puladas ${skipped}`,
  '',
  ...lines,
  '',
  'Para corrigir: "remove 2" ou "corrige 1: valor era 55,90"',
].join('\n');
return [{ json: { text: msg, transactions } }];
```

### Detecção de correção no workflow (branch de texto)

Antes de chamar `main.py` no branch de texto, detectar se é um comando de correção:

```javascript
// Condição do nó IF "É correção?"
const text = $json.text || '';
return /^(remove|corrige)\s+\d+/i.test(text.trim());
```

Se for correção, extrair índice e buscar o `notion_page_id` no mapa armazenado:

```javascript
// Armazenar mapa após processar fatura
const staticData = $getWorkflowStaticData('global');
staticData[String($json.chatId)] = $json.transactions; // array com index + notion_page_id

// Recuperar ao processar correção
const text = $json.text.trim();
const match = text.match(/^(remove|corrige)\s+(\d+)/i);
const idx = parseInt(match[2]);
const chatId = String($json.chatId);
const staticData = $getWorkflowStaticData('global');
const map = staticData[chatId] || [];
const entry = map.find(t => t.index === idx);
if (!entry) return [{ json: { error: `Transação ${idx} não encontrada` } }];

return [{ json: {
  action: match[1].toLowerCase(),
  page_id: entry.notion_page_id,
  correction_text: text.replace(/^(remove|corrige)\s+\d+:?\s*/i, ''),
} }];
```

---

## Checklist de Ativação

- [ ]  Criar credencial `Telegram Bot Financas` no n8n
- [ ]  Configurar `GEMINI_API_KEY` no Dokploy (env vars do container)
- [ ]  Configurar `NOTION_TOKEN` no Dokploy (ou usar `OPENAPI_MCP_HEADERS`)
- [ ]  Configurar `TELEGRAM_BOT_TOKEN` no Dokploy (necessário para o node Transcrever baixar áudios)
- [ ]  Verificar opções do select `Tipo` no Notion (`"Despesa"` / `"Receita"` sem emoji)
- [ ]  Ativar o workflow (toggle no canto superior direito)
- [ ]  Testar dry-run: `python3 main.py --text "Gastei 50 no mercado, Cartão Nu" --dry-run -v`
- [ ]  Testar real via Telegram (texto): `"Gastei 50 no mercado, Cartão Nu"`
- [ ]  Testar real via Telegram (áudio): gravar mensagem de voz descrevendo uma transação
- [ ]  Verificar entrada na base 💰 Transações

---

## Histórico de Construção