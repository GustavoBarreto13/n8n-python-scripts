# CLAUDE.md — gustavoboxd

Contexto específico do script `gustavoboxd`. Veja o `CLAUDE.md` na raiz para infra, padrões de retorno e deploy.

---

## Workflow n8n

**GustavoBoxd — Webhook (OMDB)** — ID: `0CvDslbxVNVeg0uv`

Dois triggers no mesmo workflow:

| Trigger | Tipo | Destino |
|---|---|---|
| Notion Webhook | POST `/webhook/gustavoboxd` | `Enriquecer Filme (OMDB)` |
| Schedule Trigger | Diário às 08:00 | `Code in JavaScript` |

---

## O que faz

Dois modos operados pelo mesmo script:

1. **Webhook** (`--page-id`): Dado o ID de uma página Notion, lê o título do filme, chama o OMDB e atualiza os metadados (diretor, gênero, poster, etc.). Cover da página vem do TMDB (backdrop) com fallback pro poster do OMDB.
2. **Letterboxd sync** (`--yesterday`): Busca o RSS do usuário no Letterboxd, filtra apenas filmes com `watchedDate` = ontem, e cria/atualiza páginas no banco de dados Notion + enriquecimento via OMDB.

---

## Entry points

```bash
# Modo webhook (trigger: automação do Notion)
/opt/venv/bin/python3 /home/node/scripts/gustavoboxd/main.py --page-id <notion-page-id> -v

# Modo sync diário (trigger: Schedule às 08:00)
/opt/venv/bin/python3 /home/node/scripts/gustavoboxd/main.py --yesterday -v
```

---

## Modos de execução

```bash
# Webhook: enriquece página com dados do OMDB
python3 main.py --page-id <id>

# Sync: importa filmes assistidos ontem do Letterboxd
python3 main.py --yesterday

# Sync: importa os últimos 50 filmes (sem filtro de data)
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
| `Date` | date | `letterboxd:watchedDate` (YYYY-MM-DD) |
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
| TMDB | `https://api.themoviedb.org/3` | Bearer token v4 | 0.3s |
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

Ambos os nodes estão no workflow **`0CvDslbxVNVeg0uv`**.

### `Enriquecer Filme (OMDB)` — trigger: Notion Webhook

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

### `Code in JavaScript` — trigger: Schedule (08:00 diário)

```javascript
const { spawnSync } = require('child_process');

const proc = spawnSync(
  '/opt/venv/bin/python3',
  ['/home/node/scripts/gustavoboxd/main.py', '--yesterday', '-v'],
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
| `Date` | Date |
| `Letterboxd URL` | URL |
| `Review` | Text |
