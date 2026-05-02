# CLAUDE.md — mal_sync

Contexto específico do script `mal_sync`. Veja o `CLAUDE.md` na raiz para infra, padrões de retorno e deploy.

---

## O que faz

Sincroniza animes atualizados na lista do MyAnimeList → database "Animes" do Notion.

Critério de sync: `list_status.updated_at` do MAL. Toda entrada cujo timestamp for maior que o `last_sync_time` salvo é escrita no Notion (update se já existe, create caso contrário). Sem diff campo a campo — se o MAL diz que mudou, o Notion recebe.

Substitui os 13 nodes do workflow n8n `MAL Sync` (ID: `RA5ojUMIbU7Kma6M`).

---

## Entry point

```bash
/opt/venv/bin/python3 /home/node/scripts/mal_sync/main.py
```

Chamado pelo workflow `MAL Sync` via `execSync`.

---

## Modos de execução

```bash
# Delta sync (padrão): processa só o que mudou desde a última run
python3 main.py

# Full: ignora last_sync_time, processa toda a lista
python3 main.py --full

# Dry-run: loga, não escreve no Notion (também não atualiza o state)
python3 main.py --dry-run

# Verbose
python3 main.py -v
```

---

## State (`mal_sync/.last_sync.json`)

Arquivo gerado em runtime, gitignored. Schema:

```json
{
  "last_sync_time": "2026-04-26T13:44:00+00:00",
  "mal_access_token": "...",
  "mal_refresh_token": "...",
  "mal_expires_at": "2026-05-26T13:44:00+00:00"
}
```

Escrita atômica (tmp + rename). **Crítico:** o `mal_refresh_token` rotaciona a cada chamada do OAuth — perder esse arquivo significa re-autenticar manualmente para gerar um novo refresh token.

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `MAL_CLIENT_ID` | Sim | App MAL — Client ID |
| `MAL_CLIENT_SECRET` | Sim | App MAL — Client Secret |
| `MAL_REFRESH_TOKEN` | Sim (1ª run) | Refresh token inicial. Após o primeiro run, o script lê do `.last_sync.json` |
| `NOTION_TOKEN` | Sim | Bearer token da integration do Notion |
| `NOTION_DB_ANIMES` | Não | Override do DB ID (default: `22ef090e-a3ca-8047-a25d-c116c21ef2d8`) |
| `OPENAPI_MCP_HEADERS` | Fallback | Usado pra extrair `NOTION_TOKEN` se não estiver direto |

---

## APIs

| API | Endpoint | Auth | Rate limit |
|---|---|---|---|
| MAL OAuth | `https://myanimelist.net/v1/oauth2/token` | client_id + client_secret + refresh_token | — |
| MAL animelist | `https://api.myanimelist.net/v2/users/@me/animelist` | Bearer access_token | — |
| Notion | `https://api.notion.com/v1` | Bearer `NOTION_TOKEN` | 0.4s entre chamadas |

---

## Fluxo

1. Carrega `.last_sync.json` (state + tokens MAL).
2. `MALAuth`: refresh do access_token se vencido (margem de 5min). Persiste imediatamente o novo refresh_token.
3. `GET /users/@me/animelist?sort=list_updated_at` — lista vem ordenada desc por updated_at.
4. **Delta filter:** itera e para no primeiro entry com `updated_at <= last_sync_time` (lista é ordenada → break).
5. Pra cada delta: query Notion por `MAL_ID equals <id>` → se existe `page_id`, PATCH; senão, POST nova página.
6. Persiste `last_sync_time = run_start` no state (a menos que `--dry-run`).

---

## Saída (stdout)

```json
{
  "ok": true,
  "dry_run": false,
  "timestamp": "2026-04-26T13:44:00+00:00",
  "mal_entries_after_delta": 3,
  "updated": 2,
  "created": 1,
  "errors": []
}
```

Quando não há nada pra sincronizar:

```json
{ "ok": true, ..., "mal_entries_after_delta": 0, "message": "no MAL updates" }
```

---

## Code node n8n

```javascript
const { execSync } = require('child_process');
const result = execSync(
  '/opt/venv/bin/python3 /home/node/scripts/mal_sync/main.py',
  { encoding: 'utf8', timeout: 120000 }
);
const data = JSON.parse(result.trim().split('\n').pop());
return [{ json: data }];
```

Timeout 120s — folga grande mesmo com lista de 100+ animes na primeira run.

---

## TODOs abertos

- [ ] Paginação MAL (>100 animes — atualmente o workflow original também não paginava)
- [ ] Sync reverso (Notion → MAL) caso necessário
- [ ] Detectar deletes (anime removido do MAL) — hoje fica órfão no Notion
