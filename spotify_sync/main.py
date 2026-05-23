#!/usr/bin/env python3
"""
spotify_sync/main.py
====================
Sincroniza o histórico de reproduções do Spotify para o BigQuery.

Dois modos:
  --import-history <dir>  : Carga histórica via JSONs do GDPR (Streaming_History_Audio_*.json)
  (sem args)              : Polling — busca as últimas 50 reproduções via API e faz MERGE

Uso:
    python3 main.py --import-history /home/node/scripts/spotify_data/...
    python3 main.py -v
    python3 main.py --dry-run

Saída: JSON em uma linha no stdout. Logs no stderr.

Variáveis de ambiente:
    SPOTIFY_CLIENT_ID              - obrigatório (polling)
    SPOTIFY_CLIENT_SECRET          - obrigatório (polling)
    SPOTIFY_REFRESH_TOKEN          - obrigatório (polling)
    GCP_PROJECT_ID                 - obrigatório
    BQ_DATASET                     - obrigatório (ex: "spotify")
    BQ_TABLE                       - obrigatório (ex: "streaming_history")
    GOOGLE_APPLICATION_CREDENTIALS - path do service account JSON
"""

import argparse
import base64
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

try:
    from google.cloud import bigquery
except ImportError:
    bigquery = None  # validado em runtime; permite --help sem a lib instalada


def _load_dotenv() -> None:
    env_path = Path(__file__).parent.parent / ".env"
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()

# ============================================================
# CONFIG
# ============================================================

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_RECENT_URL = "https://api.spotify.com/v1/me/player/recently-played"

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0
HTTP_TIMEOUT = 30

STATE_FILE = Path(__file__).parent / ".last_sync.json"

# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("spotify_sync")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


# ============================================================
# HTTP HELPER (retry + backoff)
# ============================================================


def http_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
    json_body: Optional[dict] = None,
    timeout: int = HTTP_TIMEOUT,
) -> Optional[dict]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method, url,
                headers=headers, params=params,
                data=data, json=json_body, timeout=timeout,
            )

            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", RETRY_BACKOFF * (2 ** (attempt - 1))))
                log.warning(f"429 em {url} — retry em {wait:.1f}s (tentativa {attempt})")
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(f"{resp.status_code} em {url} — retry em {wait:.1f}s")
                time.sleep(wait)
                continue

            if 400 <= resp.status_code < 500:
                body_preview = (resp.text or "")[:500]
                log.error(f"{resp.status_code} {method} {url}: {body_preview}")
                return None

            return resp.json() if resp.content else {}

        except requests.RequestException as e:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            log.warning(f"Erro {e} em {url} — retry em {wait:.1f}s")
            time.sleep(wait)

    log.error(f"Falha definitiva em {url} após {MAX_RETRIES} tentativas")
    return None


# ============================================================
# BIGQUERY SCHEMA
# ============================================================


def bq_schema() -> list:
    assert bigquery is not None
    SF = bigquery.SchemaField
    return [
        SF("ts", "TIMESTAMP", mode="REQUIRED"),
        SF("platform", "STRING"),
        SF("ms_played", "INT64"),
        SF("conn_country", "STRING"),
        SF("ip_addr", "STRING"),
        SF("master_metadata_track_name", "STRING"),
        SF("master_metadata_album_artist_name", "STRING"),
        SF("master_metadata_album_album_name", "STRING"),
        SF("spotify_track_uri", "STRING"),
        SF("episode_name", "STRING"),
        SF("episode_show_name", "STRING"),
        SF("spotify_episode_uri", "STRING"),
        SF("audiobook_title", "STRING"),
        SF("audiobook_uri", "STRING"),
        SF("audiobook_chapter_uri", "STRING"),
        SF("audiobook_chapter_title", "STRING"),
        SF("reason_start", "STRING"),
        SF("reason_end", "STRING"),
        SF("shuffle", "BOOL"),
        SF("skipped", "BOOL"),
        SF("offline", "BOOL"),
        SF("offline_timestamp", "TIMESTAMP"),
        SF("incognito_mode", "BOOL"),
        SF("source", "STRING", mode="REQUIRED"),
        SF("ingested_at", "TIMESTAMP", mode="REQUIRED"),
    ]


def table_ref() -> str:
    project = os.environ["GCP_PROJECT_ID"]
    dataset = os.environ["BQ_DATASET"]
    table = os.environ["BQ_TABLE"]
    return f"{project}.{dataset}.{table}"


def ensure_table(client) -> None:
    """Cria a tabela particionada/clusterizada caso não exista."""
    assert bigquery is not None
    ref = table_ref()
    try:
        client.get_table(ref)
        log.debug(f"Tabela {ref} já existe")
        return
    except Exception:
        log.info(f"Criando tabela {ref}")

    table = bigquery.Table(ref, schema=bq_schema())
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="ts"
    )
    table.clustering_fields = ["master_metadata_album_artist_name", "spotify_track_uri"]
    client.create_table(table)


# ============================================================
# NORMALIZAÇÃO
# ============================================================


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _offline_ts_to_iso(val) -> Optional[str]:
    if val in (None, 0, "0", ""):
        return None
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    # Spotify às vezes grava em milissegundos (13 dígitos); normaliza para segundos.
    if n > 10_000_000_000:
        n //= 1000
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, ValueError, OverflowError):
        return None


def normalize_gdpr_row(entry: dict, ingested_at: str) -> dict:
    return {
        "ts": entry.get("ts"),
        "platform": entry.get("platform"),
        "ms_played": entry.get("ms_played"),
        "conn_country": entry.get("conn_country"),
        "ip_addr": entry.get("ip_addr"),
        "master_metadata_track_name": entry.get("master_metadata_track_name"),
        "master_metadata_album_artist_name": entry.get("master_metadata_album_artist_name"),
        "master_metadata_album_album_name": entry.get("master_metadata_album_album_name"),
        "spotify_track_uri": entry.get("spotify_track_uri"),
        "episode_name": entry.get("episode_name"),
        "episode_show_name": entry.get("episode_show_name"),
        "spotify_episode_uri": entry.get("spotify_episode_uri"),
        "audiobook_title": entry.get("audiobook_title"),
        "audiobook_uri": entry.get("audiobook_uri"),
        "audiobook_chapter_uri": entry.get("audiobook_chapter_uri"),
        "audiobook_chapter_title": entry.get("audiobook_chapter_title"),
        "reason_start": entry.get("reason_start"),
        "reason_end": entry.get("reason_end"),
        "shuffle": entry.get("shuffle"),
        "skipped": entry.get("skipped"),
        "offline": entry.get("offline"),
        "offline_timestamp": _offline_ts_to_iso(entry.get("offline_timestamp")),
        "incognito_mode": entry.get("incognito_mode"),
        "source": "gdpr_import",
        "ingested_at": ingested_at,
    }


def normalize_api_row(item: dict, ingested_at: str) -> dict:
    """Mapeia um item de /me/player/recently-played para o schema."""
    track = item.get("track") or {}
    album = track.get("album") or {}
    artists = track.get("artists") or []
    artist_name = artists[0].get("name") if artists else None
    context = item.get("context") or {}
    return {
        "ts": item.get("played_at"),
        "platform": None,
        "ms_played": None,
        "conn_country": None,
        "ip_addr": None,
        "master_metadata_track_name": track.get("name"),
        "master_metadata_album_artist_name": artist_name,
        "master_metadata_album_album_name": album.get("name"),
        "spotify_track_uri": track.get("uri"),
        "episode_name": None,
        "episode_show_name": None,
        "spotify_episode_uri": None,
        "audiobook_title": None,
        "audiobook_uri": None,
        "audiobook_chapter_uri": None,
        "audiobook_chapter_title": None,
        "reason_start": context.get("type") if context else None,
        "reason_end": None,
        "shuffle": None,
        "skipped": None,
        "offline": None,
        "offline_timestamp": None,
        "incognito_mode": None,
        "source": "api_recently_played",
        "ingested_at": ingested_at,
    }


# ============================================================
# STATE
# ============================================================


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


# ============================================================
# SPOTIFY OAUTH
# ============================================================


@dataclass
class SpotifyAuth:
    client_id: str
    client_secret: str
    refresh_token: str


def _spotify_auth() -> SpotifyAuth:
    missing = [k for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN")
               if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"missing_env:{','.join(missing)}")
    return SpotifyAuth(
        os.environ["SPOTIFY_CLIENT_ID"],
        os.environ["SPOTIFY_CLIENT_SECRET"],
        os.environ["SPOTIFY_REFRESH_TOKEN"],
    )


def refresh_spotify_token(auth: SpotifyAuth) -> str:
    basic = base64.b64encode(f"{auth.client_id}:{auth.client_secret}".encode()).decode()
    log.info("Renovando access_token via refresh_token")
    resp = http_request(
        "POST", SPOTIFY_TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "refresh_token", "refresh_token": auth.refresh_token},
    )
    if not resp or "access_token" not in resp:
        raise RuntimeError("spotify_token_refresh_failed")
    return resp["access_token"]


# ============================================================
# MODO: IMPORT HISTORY
# ============================================================


def import_history(json_dir: Path, dry_run: bool) -> dict:
    if bigquery is None:
        return {"ok": False, "error": "missing_dep", "details": "google-cloud-bigquery não instalado"}

    files = sorted(json_dir.glob("Streaming_History_Audio_*.json"))
    if not files:
        return {"ok": False, "error": "no_files_found", "dir": str(json_dir)}

    ingested_at = _iso_now()
    rows: list = []
    for fp in files:
        log.info(f"Lendo {fp.name}")
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        for entry in data:
            rows.append(normalize_gdpr_row(entry, ingested_at))

    log.info(f"Total linhas normalizadas: {len(rows)} de {len(files)} arquivo(s)")

    if dry_run:
        return {
            "ok": True, "mode": "import_history", "dry_run": True,
            "files_processed": len(files), "rows_inserted": 0,
            "rows_normalized": len(rows),
        }

    client = bigquery.Client(project=os.environ["GCP_PROJECT_ID"])
    ensure_table(client)
    ref = table_ref()

    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        schema=bq_schema(),
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    log.info(f"Carregando {len(rows)} linhas em {ref} (WRITE_TRUNCATE)")
    t0 = time.time()
    job = client.load_table_from_json(rows, ref, job_config=job_config)
    job.result()
    elapsed = round(time.time() - t0, 2)

    return {
        "ok": True, "mode": "import_history", "dry_run": False,
        "files_processed": len(files), "rows_inserted": len(rows),
        "elapsed_s": elapsed,
    }


# ============================================================
# MODO: POLLING
# ============================================================


def merge_rows(client, rows: list) -> int:
    """MERGE em (ts, spotify_track_uri), idempotente."""
    assert bigquery is not None
    ref = table_ref()

    # Usa load job em staging temp + MERGE para evitar streaming insert buffer issues.
    project, dataset, table = ref.split(".")
    staging = f"{project}.{dataset}._spotify_staging_{int(time.time())}"
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        schema=bq_schema(),
    )
    client.load_table_from_json(rows, staging, job_config=job_config).result()

    merge_sql = f"""
    MERGE `{ref}` T
    USING `{staging}` S
    ON T.ts = S.ts AND COALESCE(T.spotify_track_uri, '') = COALESCE(S.spotify_track_uri, '')
    WHEN NOT MATCHED THEN
      INSERT ROW
    """
    query_job = client.query(merge_sql)
    query_job.result()
    inserted = query_job.num_dml_affected_rows or 0

    client.delete_table(staging, not_found_ok=True)
    return int(inserted)


def poll_recently_played(dry_run: bool) -> dict:
    if bigquery is None:
        return {"ok": False, "error": "missing_dep", "details": "google-cloud-bigquery não instalado"}

    try:
        auth = _spotify_auth()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    state = load_state()
    last_ts = state.get("last_ts", "1970-01-01T00:00:00Z")
    log.info(f"last_ts atual: {last_ts}")

    try:
        access_token = refresh_spotify_token(auth)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    resp = http_request(
        "GET", SPOTIFY_RECENT_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"limit": 50},
    )
    if resp is None:
        return {"ok": False, "error": "spotify_recent_failed"}

    items = resp.get("items") or []
    log.info(f"Spotify retornou {len(items)} item(s)")

    new_items = [i for i in items if (i.get("played_at") or "") > last_ts]
    if not new_items:
        return {
            "ok": True, "mode": "polling", "dry_run": dry_run,
            "fetched": len(items), "new_plays": 0, "last_ts": last_ts,
        }

    ingested_at = _iso_now()
    rows = [normalize_api_row(i, ingested_at) for i in new_items]
    new_last_ts = max(i["played_at"] for i in items if i.get("played_at"))

    if dry_run:
        return {
            "ok": True, "mode": "polling", "dry_run": True,
            "fetched": len(items), "new_plays": len(new_items),
            "last_ts_would_be": new_last_ts,
        }

    client = bigquery.Client(project=os.environ["GCP_PROJECT_ID"])
    ensure_table(client)
    inserted = merge_rows(client, rows)

    save_state({"last_ts": new_last_ts, "updated_at": ingested_at})

    return {
        "ok": True, "mode": "polling", "dry_run": False,
        "fetched": len(items), "new_plays": len(new_items),
        "inserted": inserted, "last_ts": new_last_ts,
    }


# ============================================================
# MAIN
# ============================================================


def main() -> int:
    ap = argparse.ArgumentParser(description="Spotify history → BigQuery")
    ap.add_argument("--import-history", metavar="DIR",
                    help="Modo carga histórica: diretório com Streaming_History_Audio_*.json")
    ap.add_argument("--dry-run", action="store_true", help="Não escreve no BigQuery / state")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)

    try:
        if args.import_history:
            result = import_history(Path(args.import_history), args.dry_run)
        else:
            result = poll_recently_played(args.dry_run)
    except Exception as e:
        log.exception("Erro inesperado")
        result = {"ok": False, "error": "unexpected", "details": str(e)}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
