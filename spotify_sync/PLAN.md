# spotify_sync — Histórico do Spotify no BigQuery

## Context

Gustavo solicitou os dados estendidos de streaming do Spotify (via GDPR data request) e recebeu arquivos JSON com todo o histórico de reproduções desde 2015. Quer:

1. Carregar o histórico completo no **BigQuery** (projeto/dataset já existem)
2. Manter a base atualizada com **polling automático** via API do Spotify (`/me/player/recently-played`) rodando de hora em hora pelo n8n
3. Seguir o **padrão arquitetural** já estabelecido neste repo (scripts Python chamados via `spawnSync` em Code nodes do n8n)

A API do Spotify não expõe o histórico estendido — apenas as últimas 50 reproduções (~24h). Por isso a abordagem híbrida: carga inicial via JSON GDPR + polling contínuo via API. Resultado: histórico completo desde 2015 + atualização horária daqui pra frente, tudo numa única tabela particionada no BigQuery, pronta pra análise.

---

## Arquitetura

Uma pasta `spotify_sync/` seguindo o padrão do `ticktick_notion_sync` (script principal + helper de OAuth).

```
spotify_sync/
├── main.py              # Dois modos: --import-history ou polling (default)
├── get_token.py         # OAuth Authorization Code flow (rodado UMA vez localmente)
├── CLAUDE.md            # Documentação específica
├── .gcp-sa.json         # Service Account JSON (gitignored)
├── .last_sync.json      # Estado do polling (gitignored)
└── .spotify_token.json  # (opcional) cache do access_token (gitignored)
```

### Fluxo de dados

**Modo 1 — Carga histórica (executado manualmente, uma vez):**
```
spotify_data/*.json (GDPR) → main.py --import-history → BigQuery (load_table_from_json, WRITE_TRUNCATE)
```

**Modo 2 — Polling contínuo (n8n Schedule Trigger, hourly):**
```
Spotify API /me/player/recently-played → main.py → filtra > last_ts → MERGE no BigQuery → atualiza .last_sync.json
```

---

## BigQuery Schema

Tabela `<GCP_PROJECT_ID>.<BQ_DATASET>.streaming_history`:

```sql
CREATE TABLE `<project>.<dataset>.streaming_history` (
  ts                                TIMESTAMP   NOT NULL,
  platform                          STRING,
  ms_played                         INT64,
  conn_country                      STRING,
  ip_addr                           STRING,
  master_metadata_track_name        STRING,
  master_metadata_album_artist_name STRING,
  master_metadata_album_album_name  STRING,
  spotify_track_uri                 STRING,
  episode_name                      STRING,
  episode_show_name                 STRING,
  spotify_episode_uri               STRING,
  audiobook_title                   STRING,
  audiobook_uri                     STRING,
  audiobook_chapter_uri             STRING,
  audiobook_chapter_title           STRING,
  reason_start                      STRING,
  reason_end                        STRING,
  shuffle                           BOOL,
  skipped                           BOOL,
  offline                           BOOL,
  offline_timestamp                 TIMESTAMP,
  incognito_mode                    BOOL,
  source                            STRING NOT NULL,   -- "gdpr_import" | "api_recently_played"
  ingested_at                       TIMESTAMP NOT NULL
)
PARTITION BY DATE(ts)
CLUSTER BY master_metadata_album_artist_name, spotify_track_uri;
```

**Chave de deduplicação:** `(ts, spotify_track_uri)` — usada no `MERGE` do polling.

**Campos NULL no polling:** a API `recently-played` não retorna `ms_played`, `reason_start`, `reason_end`, `shuffle`, `skipped`, `offline*`, `incognito_mode`. Essas colunas ficam `NULL` para registros com `source = 'api_recently_played'`.

---

## Spotify OAuth Setup

### Passo único (no PC do usuário)

1. Criar app em `developer.spotify.com/dashboard`
   - Redirect URI: `http://127.0.0.1:8888/callback`
   - Scope necessário: `user-read-recently-played`

2. Executar localmente:
   ```bash
   python spotify_sync/get_token.py
   ```
   - Sobe servidor HTTP local na porta 8888
   - Abre browser → usuário autoriza o app
   - Captura `code` no callback → troca por `refresh_token`
   - Imprime o `refresh_token` no terminal

3. Copiar o `refresh_token` para o Dokploy como env var `SPOTIFY_REFRESH_TOKEN`

### Refresh flow no main.py

A cada execução do polling:
```
POST https://accounts.spotify.com/api/token
  grant_type=refresh_token
  refresh_token=$SPOTIFY_REFRESH_TOKEN
  Authorization: Basic base64(client_id:client_secret)
→ access_token (TTL 1h, gerado a cada run, sem cache entre execuções)
```

---

## Env Vars (Dokploy)

```bash
# Spotify
SPOTIFY_CLIENT_ID=xxx
SPOTIFY_CLIENT_SECRET=xxx
SPOTIFY_REFRESH_TOKEN=xxx

# BigQuery
GCP_PROJECT_ID=meu-projeto-gcp
BQ_DATASET=spotify
BQ_TABLE=streaming_history
GOOGLE_APPLICATION_CREDENTIALS=/home/node/scripts/spotify_sync/.gcp-sa.json
```

### Service Account

- Roles mínimas: `BigQuery Data Editor` + `BigQuery Job User`
- JSON baixado do GCP Console → upload via `scp` para `/opt/n8n-python-scripts/spotify_sync/.gcp-sa.json` no VPS
- Bind mount reflete automaticamente no container em `/home/node/scripts/spotify_sync/.gcp-sa.json`

---

## Lógica dos modos

### Modo `--import-history <dir>`

```python
def import_history(json_dir: Path) -> dict:
    rows = []
    for json_file in sorted(json_dir.glob("Streaming_History_Audio_*.json")):
        with open(json_file, encoding="utf-8") as f:
            data = json.load(f)
        for entry in data:
            rows.append(normalize_gdpr_row(entry))

    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_TRUNCATE",
        schema=BQ_SCHEMA,
    )
    job = client.load_table_from_json(rows, table_ref, job_config=job_config)
    job.result()
    return {"ok": True, "mode": "import_history",
            "files_processed": ..., "rows_inserted": len(rows)}
```

- Usa `load_table_from_json` (load job, sem custo de streaming insert)
- `WRITE_TRUNCATE` permite reexecução limpa caso necessário
- `normalize_gdpr_row` adiciona `source="gdpr_import"` e `ingested_at=now()`

### Modo polling (default)

```python
def poll_recently_played() -> dict:
    state = load_state()  # {"last_ts": "2026-05-22T10:00:00Z"} ou {} na primeira run
    last_ts = state.get("last_ts", "1970-01-01T00:00:00Z")

    access_token = refresh_spotify_token()
    items = spotify_get(
        "https://api.spotify.com/v1/me/player/recently-played?limit=50",
        access_token,
    )["items"]

    new_items = [i for i in items if i["played_at"] > last_ts]
    if not new_items:
        return {"ok": True, "mode": "polling", "fetched": len(items), "new_plays": 0}

    rows = [normalize_api_row(i) for i in new_items]
    merge_rows(rows)  # MERGE INTO ... ON (ts, spotify_track_uri)

    new_last_ts = max(i["played_at"] for i in items)
    save_state({"last_ts": new_last_ts})

    return {"ok": True, "mode": "polling",
            "fetched": len(items), "new_plays": len(new_items),
            "last_ts": new_last_ts}
```

**MERGE statement (idempotência):**
```sql
MERGE INTO `<project>.<dataset>.streaming_history` T
USING UNNEST(@rows) S
ON T.ts = S.ts AND T.spotify_track_uri = S.spotify_track_uri
WHEN NOT MATCHED THEN INSERT ROW
```

---

## Padrão de retorno JSON

Sucesso (import):
```json
{"ok": true, "mode": "import_history", "files_processed": 9, "rows_inserted": 47823, "elapsed_s": 12.4}
```

Sucesso (polling):
```json
{"ok": true, "mode": "polling", "fetched": 50, "new_plays": 7, "last_ts": "2026-05-22T14:30:00Z"}
```

Erro:
```json
{"ok": false, "error": "spotify_token_refresh_failed", "details": "..."}
```

---

## Padrões da casa a seguir (validados na exploração)

Espelhar de [gustavoboxd/main.py](../gustavoboxd/main.py) e [series_sync/main.py](../series_sync/main.py):

- `_load_dotenv()` lendo `Path(__file__).parent.parent / ".env"` com `os.environ.setdefault`
- `setup_logging(verbose)` → logging em **stderr** com formato `%(asctime)s %(levelname)-7s %(message)s`
- `http_request()` com retry/backoff (MAX_RETRIES=3, RETRY_BACKOFF=2.0, trata 429/5xx)
- `main()` retornando `int` (exit code) e `print(json.dumps(result, ensure_ascii=False))` como **última ação**
- `argparse` com `-v/--verbose`, `--dry-run`, e flag de modo (`--import-history <dir>`)
- Dataclasses para estruturar payloads intermediários

---

## Arquivos a criar/modificar

### Novos
- `spotify_sync/main.py`
- `spotify_sync/get_token.py`
- `spotify_sync/CLAUDE.md`

### Modificados
- `requirements.txt` → adicionar `google-cloud-bigquery>=3.20.0`
- `.gitignore` → adicionar:
  ```
  spotify_sync/.gcp-sa.json
  spotify_sync/.last_sync.json
  spotify_sync/.spotify_token.json
  ```
- `CLAUDE.md` (raiz) → adicionar entrada de `spotify_sync` na estrutura de pastas, nas env vars, e novo workflow n8n na tabela

### Externo (repo do n8n, não este)
- Dockerfile do container n8n → adicionar `RUN /opt/venv/bin/pip install google-cloud-bigquery>=3.20.0` + rebuild

---

## Workflow n8n

**Novo workflow:** `Spotify History Sync`

```
[Schedule Trigger (every 1 hour)]
        ↓
[Code: spawnSync spotify_sync/main.py -v]
```

Code node:
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

## Verificação end-to-end

### 1. Setup local (OAuth)
```bash
cd c:\Users\gusta\Documents\GitHub\n8n-python-scripts
python -m venv .venv && .venv\Scripts\activate
pip install requests google-cloud-bigquery
python spotify_sync/get_token.py
# → copiar refresh_token impresso
```

### 2. Configurar Dokploy
- Adicionar as 7 env vars listadas acima
- Upload do `.gcp-sa.json` via scp em `/opt/n8n-python-scripts/spotify_sync/`
- Rebuild do container n8n com a lib `google-cloud-bigquery` no Dockerfile

### 3. Testar import histórico (manual, uma vez)
```bash
docker exec -it <n8n_container> bash
/opt/venv/bin/python3 /home/node/scripts/spotify_sync/main.py \
  --import-history /home/node/scripts/spotify_data \
  -v
```
Esperado: JSON com `ok: true, rows_inserted: N`. Conferir no BigQuery:
```sql
SELECT COUNT(*), MIN(ts), MAX(ts), source
FROM `<project>.spotify.streaming_history`
GROUP BY source;
```

### 4. Testar polling manual
```bash
/opt/venv/bin/python3 /home/node/scripts/spotify_sync/main.py -v
```
Esperado na primeira run: `new_plays: 50` (todas as 50 últimas tracks). Segunda run logo em seguida: `new_plays: 0`.

### 5. Ativar workflow n8n
- Criar workflow `Spotify History Sync` no n8n
- Schedule Trigger 1h + Code node
- Conferir execuções na aba "Executions" após algumas horas

### 6. Smoke test no BigQuery
```sql
SELECT DATE(ts) AS dia, COUNT(*) AS plays
FROM `<project>.spotify.streaming_history`
WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY dia ORDER BY dia DESC;
```

---

## Documentação no Obsidian (passo final, pós-implementação)

Conforme regra do `CLAUDE.md` raiz: após implementar, atualizar o vault do Obsidian com:
- Página descrevendo o script `spotify_sync` (arquitetura, env vars, modos de execução)
- Atualizar índice geral de scripts do repo
