# CLAUDE.md — spotify_sync

Contexto específico do script `spotify_sync`. Veja o `CLAUDE.md` na raiz para infra, padrões de retorno e deploy.

---

## O que faz

Sincroniza o histórico de reproduções do Spotify para uma tabela particionada no BigQuery.

Duas fontes complementam-se numa única tabela `streaming_history`:

| Modo | Origem | Frequência | Source tag |
|---|---|---|---|
| `--import-history <dir>` | JSONs do GDPR (`Streaming_History_Audio_*.json`) | Manual, único | `gdpr_import` |
| polling (default) | Spotify API `/me/player/recently-played` | Horário (n8n Schedule) | `api_recently_played` |

A API só expõe ~24h. O GDPR é a única forma de obter histórico estendido — daí a abordagem híbrida.

---

## Workflow n8n

**Spotify History Sync** — ID: `KftB75QdQHXbIzgj`

| Trigger | Tipo | Destino |
|---|---|---|
| Schedule Trigger | A cada 1h | Code node → `main.py -v` |

---

## Entry points

```bash
# Polling (n8n schedule)
/opt/venv/bin/python3 /home/node/scripts/spotify_sync/main.py -v

# Import histórico (executar UMA vez, manualmente no container)
/opt/venv/bin/python3 /home/node/scripts/spotify_sync/main.py \
  --import-history "/home/node/scripts/spotify_data/Spotify Extended Streaming History" -v

# Dry-run (não escreve no BigQuery nem no state)
python3 main.py --dry-run
python3 main.py --import-history <dir> --dry-run
```

---

## Setup OAuth (uma vez, no PC do usuário)

1. Criar app em `developer.spotify.com/dashboard`
   - Redirect URI: `http://127.0.0.1:8888/callback`
   - Scope: `user-read-recently-played`
2. Definir `SPOTIFY_CLIENT_ID` e `SPOTIFY_CLIENT_SECRET` no `.env` (raiz do repo)
3. Rodar: `python spotify_sync/get_token.py`
4. Copiar o `refresh_token` impresso → env var `SPOTIFY_REFRESH_TOKEN` no Dokploy

O `refresh_token` é de longa duração; usado a cada execução para obter um `access_token` (TTL 1h).

---

## BigQuery — Schema

Tabela `${GCP_PROJECT_ID}.${BQ_DATASET}.${BQ_TABLE}` (default: `streaming_history`):

- Particionada por `DATE(ts)`
- Clusterizada por `master_metadata_album_artist_name`, `spotify_track_uri`
- Chave de deduplicação no MERGE: `(ts, spotify_track_uri)`
- Campos NULL no polling: `ms_played`, `reason_end`, `shuffle`, `skipped`, `offline*`, `incognito_mode`, `platform`, `conn_country`, `ip_addr` (a API não retorna)

A tabela é **criada automaticamente** na primeira execução (`ensure_table`) caso não exista.

---

## Service Account

- Roles: `BigQuery Data Editor` + `BigQuery Job User`
- JSON em `/opt/n8n-python-scripts/spotify_sync/.gcp-sa.json` no host VPS
- Bind mount: `/home/node/scripts/spotify_sync/.gcp-sa.json` no container
- Env var: `GOOGLE_APPLICATION_CREDENTIALS=/home/node/scripts/spotify_sync/.gcp-sa.json`

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | Sim (polling) | Spotify Developer App |
| `SPOTIFY_CLIENT_SECRET` | Sim (polling) | Spotify Developer App |
| `SPOTIFY_REFRESH_TOKEN` | Sim (polling) | Obtido via `get_token.py` |
| `GCP_PROJECT_ID` | Sim | Projeto GCP |
| `BQ_DATASET` | Sim | Dataset BigQuery (ex: `spotify`) |
| `BQ_TABLE` | Sim | Tabela (ex: `streaming_history`) |
| `GOOGLE_APPLICATION_CREDENTIALS` | Sim | Path do JSON do service account |

---

## Saída (stdout)

```json
// Polling: novas reproduções
{"ok": true, "mode": "polling", "dry_run": false, "fetched": 50, "new_plays": 7, "inserted": 7, "last_ts": "2026-05-22T14:30:00Z"}

// Polling: sem novas
{"ok": true, "mode": "polling", "dry_run": false, "fetched": 50, "new_plays": 0, "last_ts": "2026-05-22T14:30:00Z"}

// Import histórico
{"ok": true, "mode": "import_history", "dry_run": false, "files_processed": 9, "rows_inserted": 47823, "elapsed_s": 12.4}

// Erros
{"ok": false, "error": "spotify_token_refresh_failed"}
{"ok": false, "error": "missing_env:SPOTIFY_REFRESH_TOKEN"}
{"ok": false, "error": "no_files_found", "dir": "..."}
```

---

## Code node n8n (polling, schedule 1h)

```javascript
const { spawnSync } = require('child_process');

const proc = spawnSync(
  '/opt/venv/bin/python3',
  ['/home/node/scripts/spotify_sync/main.py', '-v'],
  { encoding: 'utf8', timeout: 60000 }
);

if (proc.error) throw proc.error;

const result = JSON.parse(proc.stdout.trim());
result._logs = proc.stderr.slice(-3000);
return [{ json: result }];
```

Schedule de 1h é confortável: a janela do `recently-played` é ~24h, então mesmo se o workflow falhar por algumas horas, nada é perdido.

---

## Lógica do polling

1. Lê `last_ts` de `.last_sync.json` (ou `1970-01-01T00:00:00Z` na primeira run)
2. Refresh do `access_token` (Basic auth com `client_id:client_secret`)
3. GET `/v1/me/player/recently-played?limit=50`
4. Filtra `items` onde `played_at > last_ts`
5. Se nenhum novo: retorna sem tocar no BigQuery
6. Caso contrário: carrega num staging table temporário + `MERGE INTO` na tabela final em `(ts, spotify_track_uri)`
7. Atualiza `.last_sync.json` com `max(played_at)` retornado

O staging table evita problemas de streaming buffer e garante idempotência mesmo em retries do n8n.

---

## Lógica do import-history

1. Lê todos `Streaming_History_Audio_*.json` do diretório
2. Normaliza cada entrada (adiciona `source="gdpr_import"`, `ingested_at`)
3. `load_table_from_json` com `WRITE_TRUNCATE` (permite re-execução)

⚠️ `WRITE_TRUNCATE` apaga TUDO — incluindo polls anteriores. Rodar **antes** de ativar o workflow de polling.

---

## Heartbeat n8n

Polling termina em segundos; sem ajuste necessário.
Import histórico pode demorar — rodar **fora do n8n**, diretamente no container.

---

## Dependências

- `requests` (já no `requirements.txt`)
- `google-cloud-bigquery>=3.20.0` — precisa estar no `Dockerfile` do repo n8n (`RUN /opt/venv/bin/pip install google-cloud-bigquery>=3.20.0`)
