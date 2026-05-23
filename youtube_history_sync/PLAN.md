# PLAN — `youtube_history_sync` — Takeout do YouTube → BigQuery

## Context

Espelhar a arquitetura do `spotify_sync` para o YouTube. Diferente do Spotify, a **YouTube Data API v3 não expõe histórico de visualização** (restrição de privacidade do Google) — logo, não há modo polling possível. A única fonte de histórico é o **Google Takeout** (`takeout.google.com` → "YouTube e YouTube Music" → "histórico" → formato JSON).

O Takeout gera `watch-history.json`: array de objetos com `header`, `title`, `titleUrl`, `subtitles[]` (canal: `name` + `url`), `time` (ISO 8601), e ocasionalmente `details` (ex: `From Google Ads`). Entradas de vídeos removidos vêm sem `titleUrl`.

Implementação será feita depois. Esta pasta hoje só guarda este plano.

---

## Arquivos a criar

### 1. `youtube_history_sync/main.py`

Mesma estrutura de `spotify_sync/main.py`:

- `_load_dotenv()` lendo `Path(__file__).parent.parent / ".env"` com `os.environ.setdefault`
- `setup_logging(verbose)` → stderr, formato `%(asctime)s %(levelname)-7s %(message)s`
- `http_request()` com `MAX_RETRIES=3`, `RETRY_BACKOFF=2.0`, tratamento 429/5xx (usado apenas no modo `--enrich`)
- `bigquery` importado com try/except `ImportError` (permite `--help` sem a lib)
- `argparse`:
  - `--import-history <dir>` (obrigatório — diretório com `watch-history.json`)
  - `--enrich` (opcional, chama YouTube Data API para metadados de cada vídeo)
  - `-v/--verbose`, `--dry-run`
- `main()` retorna exit code; `print(json.dumps(result, ensure_ascii=False))` no final

**Modo `--import-history`** (único modo):
1. Lê `watch-history.json` (ou todos `watch-history*.json` se o Takeout veio dividido)
2. Para cada entrada:
   - Pula se `header != "YouTube"` e `header != "YouTube Music"` (filtra ruído de outras seções)
   - Extrai `video_id` do `titleUrl` (regex `[?&]v=([A-Za-z0-9_-]{11})`); pula entradas sem `titleUrl` (vídeos removidos)
   - Normaliza:
     - `watched_at` (TIMESTAMP, parse ISO 8601 do campo `time`)
     - `video_id`, `video_url` (= `titleUrl`)
     - `title` (sem o prefixo `Watched ` que o Takeout adiciona)
     - `channel_name` (de `subtitles[0].name`), `channel_url` (de `subtitles[0].url`)
     - `is_youtube_music` (= `header == "YouTube Music"`)
     - `source = "takeout_import"`, `ingested_at = now UTC`
3. Se `--enrich`: agrupa `video_id`s únicos, chama `videos.list?part=snippet,contentDetails,statistics&id=...` (50 por request, com paginação manual); enriquece com `duration_iso`, `duration_seconds` (parse ISO 8601), `category_id`, `tags`, `view_count`, `like_count`, `published_at`
4. `ensure_table` (auto-cria com partição+cluster se não existir)
5. `bigquery.Client().load_table_from_json` com `WRITE_TRUNCATE` na tabela `${BQ_TABLE_YOUTUBE}` (default: `watch_history_youtube`)
6. Saída JSON: `{"ok": true, "mode": "import_history", "files_processed": N, "rows_inserted": N, "enriched": bool, "elapsed_s": N}`

**Schema BigQuery** (`${GCP_PROJECT_ID}.${BQ_DATASET}.${BQ_TABLE_YOUTUBE}`):
- Particionada por `DATE(watched_at)`
- Clusterizada por `channel_name`, `video_id`
- Campos base:
  - `watched_at` TIMESTAMP REQUIRED
  - `video_id` STRING
  - `video_url` STRING
  - `title` STRING
  - `channel_name` STRING
  - `channel_url` STRING
  - `is_youtube_music` BOOL
  - `source` STRING REQUIRED
  - `ingested_at` TIMESTAMP REQUIRED
- Campos `--enrich` (NULL se não habilitado):
  - `duration_iso` STRING, `duration_seconds` INT64
  - `category_id` STRING
  - `tags` STRING REPEATED
  - `view_count` INT64, `like_count` INT64
  - `published_at` TIMESTAMP

### 2. `youtube_history_sync/CLAUDE.md`

Documentação no formato do `spotify_sync/CLAUDE.md`:
- O que faz (carga única; **sem polling** — API do YouTube não expõe histórico)
- Como exportar o Takeout (passo a passo)
- Entry points (Windows local + dry-run)
- Schema BigQuery
- Env vars (`YOUTUBE_API_KEY` opcional para `--enrich`; reusa `GCP_PROJECT_ID`, `BQ_DATASET`, `BQ_TABLE_YOUTUBE`, `GOOGLE_APPLICATION_CREDENTIALS`)
- Avisos: `WRITE_TRUNCATE` apaga tudo; re-rodar Takeout periodicamente (trimestral) para atualizar
- Sem Code node n8n (execução manual)

---

## Arquivos a modificar (quando implementar)

### `.gitignore`
Adicionar:
```
youtube_history_sync/.last_sync.json
youtube_data/
```

### `CLAUDE.md` (raiz)
- Adicionar `youtube_history_sync/` na árvore de pastas
- Adicionar env vars: `YOUTUBE_API_KEY` (opcional), `BQ_TABLE_YOUTUBE` (ex: `watch_history_youtube`)
- **Sem entrada** na tabela de workflows n8n (não há workflow — execução manual)

### `requirements.txt`
Nenhuma alteração (`requests` e `google-cloud-bigquery` já presentes).

---

## Fluxo de uso (manual, no PC do Gustavo)

1. Gerar Takeout em [takeout.google.com](https://takeout.google.com) → selecionar apenas "YouTube e YouTube Music" → **conteúdo histórico → JSON** → exportar
2. Baixar o `.zip` e descompactar em `youtube_data/` na raiz do repo
3. Rodar:
   ```powershell
   $env:GOOGLE_APPLICATION_CREDENTIALS = "C:\Users\gusta\Documents\GitHub\n8n-python-scripts\spotify_sync\.gcp-sa.json"
   python youtube_history_sync\main.py `
     --import-history "youtube_data\Takeout\YouTube e YouTube Music\histórico" -v
   ```
4. (Opcional) Enriquecer com metadados — precisa `YOUTUBE_API_KEY` no `.env` (criar em console.cloud.google.com → APIs → YouTube Data API v3 → Credenciais):
   ```powershell
   python youtube_history_sync\main.py --import-history "..." --enrich -v
   ```

---

## Fora do escopo

- Workflow n8n (não aplicável — sem polling)
- Tabela separada para likes/subscriptions/playlists (poderia ser feito via API; iteração futura)
- Deduplicação incremental entre Takeouts (re-rodar com `WRITE_TRUNCATE` é o padrão)
- Service Account GCP separado (reusa o do `spotify_sync` se estiver no mesmo projeto)

---

## Verificação end-to-end (a rodar quando implementar)

1. **Import**:
   ```powershell
   python youtube_history_sync\main.py --import-history "..." -v
   ```
   Esperado: `{"ok": true, "mode": "import_history", "rows_inserted": N}`

2. **Conferência BigQuery**:
   ```sql
   SELECT COUNT(*), MIN(watched_at), MAX(watched_at)
   FROM `<project>.<dataset>.watch_history_youtube`;
   ```

3. **Top canais**:
   ```sql
   SELECT channel_name, COUNT(*) plays
   FROM `<project>.<dataset>.watch_history_youtube`
   WHERE channel_name IS NOT NULL
   GROUP BY channel_name
   ORDER BY plays DESC LIMIT 100;
   ```

4. **Distribuição mensal**:
   ```sql
   SELECT DATE_TRUNC(DATE(watched_at), MONTH) mes, COUNT(*) plays
   FROM `<project>.<dataset>.watch_history_youtube`
   GROUP BY mes ORDER BY mes;
   ```

---

## Status

- [x] Plano salvo em `youtube_history_sync/PLAN.md`
- [ ] `main.py` implementado
- [ ] `CLAUDE.md` do módulo escrito
- [ ] `.gitignore` e `CLAUDE.md` raiz atualizados
- [ ] Takeout exportado pelo Gustavo
- [ ] Teste local rodado com sucesso
- [ ] (Opcional) `--enrich` testado com `YOUTUBE_API_KEY`
- [ ] Documentação Obsidian atualizada
