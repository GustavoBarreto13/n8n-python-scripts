# CLAUDE.md — series_sync

Contexto específico do script `series_sync`. Veja o `CLAUDE.md` na raiz para infra, padrões de retorno e deploy.

---

## O que faz

Busca metadados de uma série de TV no TMDB e sincroniza com o Notion:
- Atualiza a página principal da série (`Type=Show`): overview, status, contagens, poster, backdrop como cover
- Cria/atualiza páginas de temporadas (`Type=Season`): sinopse, poster, air date
- Cria/atualiza páginas de episódios (`Type=Episode`): nome, overview, still, release date, cover

Trigger: automação do Notion dispara webhook → n8n workflow `Series Boxd` (`Wqa6x53ZRkpI7bw6`) → `execSync` chamando este script.

---

## Entry point

```bash
# Single show (webhook)
/opt/venv/bin/python3 /home/node/scripts/series_sync/main.py --page-id <notion-page-id>

# Batch: todas as Returning Series (schedule diário)
/opt/venv/bin/python3 /home/node/scripts/series_sync/main.py
```

---

## Modos de execução

```bash
# Webhook mode: atualiza uma série específica
python3 main.py --page-id <id>

# Batch mode: processa todas as "Returning Series" com TMDB ID preenchido
python3 main.py

# Dry-run: loga sem escrever no Notion
python3 main.py --page-id <id> --dry-run

# Verbose
python3 main.py --page-id <id> -v
```

---

## Lógica de skip de episódios

- **Skip**: episódio existe no Notion E tem `Release Date` E tem still (`Poster` preenchido) E `Release Date < hoje`
- **Update**: episódio existe mas falta still, falta data, ou data é futura
- **Create**: episódio não existe no Notion

---

## Campos Notion atualizados

### Show (Type=Show)
| Campo | Tipo | Fonte TMDB |
|---|---|---|
| `Overview` | rich_text | `overview` |
| `Episode Count` | number | `number_of_episodes` |
| `Seasons Count` | number | `number_of_seasons` |
| `Release Date` | date | `first_air_date` |
| `Series Status` | select | `status` (ex: "Returning Series", "Ended") |
| `ID` | number | `id` |
| `Poster` | files | `poster_path` |
| `Network` | select | `networks[0].name` |
| `Type` | select | "Show" (fixo) |
| cover | page cover | `backdrop_path` |

### Season (Type=Season) — title: `"ShowName - Season N"`
| Campo | Tipo | Fonte |
|---|---|---|
| `Parent item` | relation | show page_id |
| `Type` | select | "Season" |
| `Season Number` | number | `season_number` |
| `Overview` | rich_text | `overview` |
| `ID` | number | tmdb_series_id |
| `Poster` | files | `poster_path` |
| `Air Date` | date | `air_date` |

### Episode (Type=Episode) — title: `"ShowName - S01E01 - Título"`
| Campo | Tipo | Fonte |
|---|---|---|
| `Parent item` | relation | season page_id |
| `Related Series` | relation | show page_id |
| `Type` | select | "Episode" |
| `Episode Number` | number | `episode_number` |
| `Season Number` | number | `season_number` |
| `Overview` | rich_text | `overview` |
| `Poster` | files | `still_path` |
| `Release Date` | date | `air_date` |
| cover | page cover | `still_path` |

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `NOTION_TOKEN` | Sim | Bearer token da integration do Notion |
| `TMDB_TOKEN` | Sim | Bearer token TMDB v4 (JWT) — configurar no Dokploy |
| `NOTION_DB_SERIES` | Não | ID do database (default: `196f090e-a3ca-80d7-adae-ee1e3da03edf`) |
| `OPENAPI_MCP_HEADERS` | Fallback | Usado para extrair `NOTION_TOKEN` se não estiver direto |

O `TMDB_TOKEN` é o Bearer token JWT do TMDB (v4 auth). Está hardcoded no workflow n8n antigo — extrair dali e configurar no Dokploy.

---

## APIs

| API | Endpoint | Auth | Delay |
|---|---|---|---|
| TMDB Search | `GET /search/tv` | Bearer | 0.3s |
| TMDB Show | `GET /tv/{id}` | Bearer | 0.3s |
| TMDB Season | `GET /tv/{id}/season/{n}` | Bearer | 0.3s |
| Notion | `https://api.notion.com/v1` | Bearer token | 0.4s |

---

## Saída (stdout)

```json
// Single mode (--page-id): sucesso
{
  "ok": true,
  "dry_run": false,
  "title": "Breaking Bad",
  "tmdb_id": 1396,
  "seasons_processed": 5,
  "episodes_created": 62,
  "episodes_updated": 3,
  "episodes_skipped": 0,
  "errors": []
}

// Batch mode: sucesso
{
  "ok": true,
  "dry_run": false,
  "seasons_processed": 12,
  "episodes_created": 8,
  "episodes_updated": 5,
  "episodes_skipped": 340,
  "errors": []
}

// Erro de runtime
{ "ok": false, "error": "missing_tmdb_api_key" }
```

---

## Code node n8n (webhook mode)

```javascript
const { execSync } = require('child_process');
const pageId = $json.body.data.id;
const result = execSync(
  `/opt/venv/bin/python3 /home/node/scripts/series_sync/main.py --page-id ${pageId}`,
  { encoding: 'utf8', timeout: 120000 }
);
return [{ json: JSON.parse(result.trim()) }];
```

**Nota**: usar `timeout: 120000` (2 min). Uma série com muitas temporadas pode demorar devido aos rate limits do TMDB (0.3s/req) e Notion (0.4s/req).

---

## Heartbeat n8n

Para batch mode (schedule diário), configurar no Dokploy:
```
N8N_RUNNERS_HEARTBEAT_INTERVAL=120
```

---

## Bugs corrigidos vs workflow n8n original

1. **Backdrop nunca aplicado** — `Update Backdrop` estava desconectado no workflow
2. **Update de episódio criava duplicata** — PATCH no page_id existente, nunca POST
3. **Cover do episódio usava campo errado** — `still_path` em vez de `property_poster[0]`
4. **Season `Air Date` nunca populado** — campo enviado no upsert
5. **Tratar Entrada usava `items[0]`** — batch mode processa todos via paginação Notion
