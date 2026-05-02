# CLAUDE.md — anime_sync

Contexto específico do script `anime_sync`. Veja o `CLAUDE.md` na raiz para infra, padrões de retorno e deploy.

---

## O que faz

Sincroniza animes "Currently Airing" do Notion com dados de Jikan/AniList/TMDB: atualiza metadados da Season page e cria/atualiza páginas de Episode com thumbnails, datas de exibição e sinopses.

---

## Entry point

```bash
/opt/venv/bin/python3 /home/node/scripts/anime_sync/main.py
```

Chamado pelo workflow n8n `MyAnimeBoxd` (ID: `N_l9odG0de5B1LlSXQTdO`) via `execSync`.

---

## Estrutura

Tudo em um único arquivo `main.py` (~914 linhas). Seções principais:

- **CONFIG** — delays de rate limit, blacklist de MAL IDs, constantes
- **HTTP helper** — `http_request()` com retry/backoff automático (429, 5xx)
- **Dataclasses** — `AnimeInput`, `Episode`, `SyncStats`
- **NotionClient** — query, create, update de Season e Episode pages
- **Jikan API** — `jikan_get_full()`, `jikan_get_episodes()`
- **AniList API** — `anilist_get_schedule_and_banner()` (schedule futuro + banner)
- **ARM + TMDB** — `arm_get_tmdb()` traduz MAL ID → TMDB ID; `TMDBClient` busca thumbnails
- **Merge/Normalize** — combina Jikan + AniList, normaliza para Notion
- **`process_anime()`** — orquestra o pipeline completo para 1 anime
- **`main()`** — parse de args, inicializa clients, loop por animes, imprime JSON

---

## Modos de execução

```bash
# Batch: sincroniza todos os Currently Airing do Notion
python3 main.py

# Webhook: sincroniza um anime específico (recebe page_id do Notion)
python3 main.py --page-id <notion_page_uuid>

# Dry-run: loga mas não escreve no Notion
python3 main.py --dry-run

# Verbose: logs em nível DEBUG
python3 main.py -v
```

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `NOTION_TOKEN` | Sim | Bearer token da integration do Notion |
| `TMDB_TOKEN` | Não | Bearer token da TMDB v4 API — sem ela, sem thumbnails |
| `NOTION_DB_ANIMES` | Não | ID do database (default: `22ef090e-a3ca-8047-a25d-c116c21ef2d8`) |
| `OPENAPI_MCP_HEADERS` | Fallback | Usado p/ extrair `NOTION_TOKEN` se não configurado diretamente |

---

## APIs utilizadas

| API | Base URL | Auth | Rate limit configurado |
|---|---|---|---|
| Jikan (MAL) | `https://api.jikan.moe/v4` | Nenhuma | 1.2s entre chamadas |
| AniList | `https://graphql.anilist.co` | Nenhuma | 0.8s entre chamadas |
| ARM | `https://arm.haglund.dev/api/v2` | Nenhuma | 0.5s entre chamadas |
| TMDB | `https://api.themoviedb.org/3` | Bearer `TMDB_TOKEN` | 0.3s entre chamadas |
| Notion | `https://api.notion.com/v1` | Bearer `NOTION_TOKEN` | 0.4s entre chamadas |

---

## Saída (stdout)

JSON estruturado — última linha impressa, lida pelo Code node do n8n:

```json
{
  "ok": true,
  "dry_run": false,
  "timestamp": "2026-04-25T...",
  "animes_processed": 5,
  "animes_skipped": 1,
  "episodes_created": 42,
  "episodes_updated": 8,
  "episodes_skipped": 120,
  "errors": []
}
```

`ok: false` quando há erros por anime (script ainda termina com exit 0 para não quebrar o `execSync`).

---

## Blacklist

MAL IDs em `BLACKLIST_MAL_IDS` (linha ~61 do `main.py`) só atualizam metadados — sem sync de episódios. One Piece (21) está na blacklist por ter 1100+ episódios.

---

## Code node n8n

Timeout de 120s — cada anime leva ~5-10s por causa dos rate limits.

```javascript
const { execSync } = require('child_process');
const result = execSync(
  '/opt/venv/bin/python3 /home/node/scripts/anime_sync/main.py',
  { encoding: 'utf8', timeout: 120000 }
);
return [{ json: JSON.parse(result) }];
```

---

## TODOs abertos

- [ ] Multi-season edge cases (animes com 3+ temporadas no TMDB)
- [ ] Paginação para listas grandes (>100 animes airing)
