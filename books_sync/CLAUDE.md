# CLAUDE.md — books_sync

Contexto específico do script `books_sync`. Veja o `CLAUDE.md` na raiz para infra, padrões de retorno e deploy.

---

## O que faz

Busca metadados de um livro (título, autores, ISBN, capa, etc.) e atualiza uma página do Notion com esses dados.

Trigger: automação do Notion dispara webhook → n8n workflow `GustavoBooks` (`YW1MqVhd6zR-ufOvrxJIR`) → `execSync` chamando este script.

---

## Entry point

```bash
/opt/venv/bin/python3 /home/node/scripts/books_sync/main.py --page-id <notion-page-id>
```

---

## Modos de execução

```bash
# Execução padrão: lê página, busca livro, atualiza Notion
python3 main.py --page-id <id>

# Dry-run: loga sem escrever no Notion
python3 main.py --page-id <id> --dry-run

# Verbose
python3 main.py --page-id <id> -v
```

---

## Estratégia de busca

1. Se `ISBN-10` ou `ISBN-13` estiver preenchido na página Notion:
   - Tenta Google Books por ISBN
   - Fallback: Open Library por ISBN
2. Senão, busca pelo título (`Name` da página):
   - Tenta Google Books por título (`intitle:"..."`)
   - Fallback: Open Library por título

---

## Campos Notion atualizados

| Campo | Tipo Notion | Fonte |
|---|---|---|
| `Author` | rich_text | autores separados por `, ` |
| `Pages` | number | pageCount (skip se 0) |
| `Description` | rich_text | descrição truncada em 2000 chars |
| `Publish Date` | date | publishedDate → YYYY-MM-DD |
| `ISBN-10` | rich_text | isbn10 (ou isbn13 se não tiver isbn10) |
| `Cover` | files (external URL) | imageLinks, HTTPS, sem `&edge=curl` |

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `NOTION_TOKEN` | Sim | Bearer token da integration do Notion |
| `GOOGLE_BOOKS_API_KEY` | Não* | API key do Google Books. *Sem ela, usa só Open Library |
| `OPENAPI_MCP_HEADERS` | Fallback | Usado para extrair `NOTION_TOKEN` se não estiver direto |

---

## APIs

| API | Endpoint | Auth | Delay |
|---|---|---|---|
| Google Books | `https://www.googleapis.com/books/v1/volumes` | `key` param | 0.3s |
| Open Library Books | `https://openlibrary.org/api/books` | sem auth | 0.5s |
| Open Library Search | `https://openlibrary.org/search.json` | sem auth | 0.5s |
| Notion | `https://api.notion.com/v1` | Bearer token | 0.4s |

---

## Saída (stdout)

```json
// Sucesso
{
  "ok": true,
  "found": true,
  "source": "google_books",
  "title": "Clean Code",
  "authors": "Robert C. Martin",
  "isbn": "0132350882",
  "updated_fields": ["Author", "Pages", "Description", "Publish Date", "ISBN-10", "Cover"]
}

// Não encontrado
{ "ok": false, "found": false, "error": "book_not_found", "query": "Clean Cod" }

// Erro de runtime
{ "ok": false, "error": "missing_notion_token" }
```

---

## Code node n8n (GustavoBooks workflow)

```javascript
const { execSync } = require('child_process');
const pageId = $json.body.data.id;
const result = execSync(
  `/opt/venv/bin/python3 /home/node/scripts/books_sync/main.py --page-id ${pageId}`,
  { encoding: 'utf8', timeout: 30000 }
);
return [{ json: JSON.parse(result.trim()) }];
```
