# CLAUDE.md — gustavoboxd

Contexto específico do script `gustavoboxd`. Veja o `CLAUDE.md` na raiz para infra, padrões de retorno e deploy.

---

## O que faz

Dois modos:
1. **Webhook** (`--page-id`): Dado o ID de uma página Notion, lê o título do filme, chama o OMDB e atualiza os metadados (diretor, gênero, poster, etc.)
2. **Letterboxd sync** (sem args): Busca o RSS do usuário no Letterboxd, cria/atualiza páginas no banco de dados Notion com os filmes assistidos + enriquecimento via OMDB

---

## Entry points

```bash
# Modo webhook (trigger: automação do Notion)
/opt/venv/bin/python3 /home/node/scripts/gustavoboxd/main.py --page-id <notion-page-id>

# Modo sync (trigger: Schedule no n8n)
/opt/venv/bin/python3 /home/node/scripts/gustavoboxd/main.py
```

---

## Modos de execução

```bash
# Webhook: enriquece página com dados do OMDB
python3 main.py --page-id <id>

# Sync: importa últimos 50 filmes do Letterboxd
python3 main.py

# Dry-run: loga sem escrever no Notion
python3 main.py --dry-run
python3 main.py --page-id <id> --dry-run

# Verbose
python3 main.py -v
```

---

## Campos Notion

### Propriedades preenchidas pelo OMDB (webhook + sync de novos filmes)

| Campo | Tipo Notion | Fonte |
|---|---|---|
| `Name` | title | OMDB `Title` |
| `Year` | rich_text | Extraído de `Released` (YYYY) |
| `Director` | multi_select | OMDB `Director` (CSV) |
| `Actors` | multi_select | OMDB `Actors` (CSV, máx 10) |
| `Genre` | multi_select | OMDB `Genre` (CSV) |
| `Country` | multi_select | OMDB `Country` (CSV) |
| `Language` | multi_select | OMDB `Language` (CSV) |
| `Overview` | rich_text | OMDB `Plot` (truncado em 2000 chars) |
| `Released Date` | date | OMDB `Released` → YYYY-MM-DD |
| `Poster` | files (external) | OMDB `Poster` URL |
| Cover da página | cover | Mesmo URL do `Poster` |

### Propriedades preenchidas pelo Letterboxd (modo sync)

| Campo | Tipo Notion | Fonte |
|---|---|---|
| `Name` | title | `letterboxd:filmTitle` do RSS |
| `Year` | rich_text | `letterboxd:filmYear` do RSS |
| `Rating` | number | `letterboxd:memberRating` (0.5–5.0) |
| `Watched Date` | date | `letterboxd:watchedDate` (YYYY-MM-DD) |
| `Letterboxd URL` | url | `<link>` do item RSS — usado como chave de dedup |
| `Review` | rich_text | `<description>` sem HTML (truncado em 2000 chars) |

---

## Deduplicação (modo sync)

Antes de criar cada entrada, o script faz uma query no banco de dados Notion filtrando por `Letterboxd URL`. Se já existir, atualiza (rating, review, watched date podem ter mudado). Se não existir, cria e chama OMDB para enriquecer.

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `NOTION_TOKEN` | Sim | Bearer token da integration do Notion |
| `OMDB_API_KEY` | Sim (webhook) / Opcional (sync) | API key do OMDB. Sem ela, sync cria entradas sem metadados |
| `NOTION_DB_GUSTAVOBOXD` | Sim (sync) | ID do banco de dados Notion de filmes |
| `LETTERBOXD_USERNAME` | Sim (sync) | Username do Letterboxd (ex: `gustavob`) |
| `OPENAPI_MCP_HEADERS` | Fallback | Usado para extrair `NOTION_TOKEN` se não estiver direto |

---

## APIs

| API | Endpoint | Auth | Delay |
|---|---|---|---|
| OMDB | `http://www.omdbapi.com` | `apikey` param | 0.3s |
| Letterboxd RSS | `https://letterboxd.com/{username}/rss/` | sem auth | — |
| Notion | `https://api.notion.com/v1` | Bearer token | 0.4s |

---

## Saída (stdout)

```json
// Webhook: sucesso
{
  "ok": true,
  "dry_run": false,
  "title": "Inception",
  "year": "2010",
  "director": ["Christopher Nolan"],
  "genre": ["Action", "Adventure", "Sci-Fi"],
  "released_date": "2010-07-16"
}

// Webhook: não encontrado no OMDB
{ "ok": false, "error": "omdb_not_found", "title": "Titulo Inexistente", "dry_run": false }

// Sync: sucesso
{
  "ok": true,
  "dry_run": false,
  "created": 5,
  "updated": 12,
  "skipped": 0,
  "errors": []
}

// Sync: erro de configuração
{ "ok": false, "error": "missing_letterboxd_username", "dry_run": false }
```

---

## Code nodes n8n

### Webhook (trigger: Notion cria página)

```javascript
const { spawnSync } = require('child_process');
const pageId = items[0].json.body.data.id;

const proc = spawnSync(
  '/opt/venv/bin/python3',
  ['/home/node/scripts/gustavoboxd/main.py', '--page-id', pageId, '-v'],
  { encoding: 'utf8', timeout: 60000 }
);

if (proc.error) throw proc.error;
const result = JSON.parse(proc.stdout.trim());
result._logs = proc.stderr.slice(-3000);
return [{ json: result }];
```

### Sync Letterboxd (trigger: Schedule — ex: diário)

```javascript
const { spawnSync } = require('child_process');

const proc = spawnSync(
  '/opt/venv/bin/python3',
  ['/home/node/scripts/gustavoboxd/main.py', '-v'],
  { encoding: 'utf8', timeout: 120000 }
);

if (proc.error) throw proc.error;
const result = JSON.parse(proc.stdout.trim());
result._logs = proc.stderr.slice(-3000);
return [{ json: result }];
```

---

## Configuração do banco de dados Notion

O banco precisa ter as seguintes propriedades criadas manualmente:

| Propriedade | Tipo |
|---|---|
| `Name` | Title (padrão) |
| `Year` | Text |
| `Director` | Multi-select |
| `Actors` | Multi-select |
| `Genre` | Multi-select |
| `Country` | Multi-select |
| `Language` | Multi-select |
| `Overview` | Text |
| `Released Date` | Date |
| `Poster` | Files & media |
| `Rating` | Number |
| `Watched Date` | Date |
| `Letterboxd URL` | URL |
| `Review` | Text |
