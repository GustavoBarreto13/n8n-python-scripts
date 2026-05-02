#!/usr/bin/env python3
"""
main.py (mal_sync/main.py)
==========================
Sincroniza animes atualizados no MAL → Notion.

Substitui os 13 nodes do workflow n8n `MAL Sync` (RA5ojUMIbU7Kma6M).

Critério de sync: `list_status.updated_at` do MAL. Toda entrada cujo
`updated_at > last_sync_time` é escrita no Notion (update se já existe,
create caso contrário). Sem diff campo a campo.

Uso:
    python3 main.py            # delta sync (padrão)
    python3 main.py --full     # ignora last_sync_time, processa todas
    python3 main.py --dry-run  # loga, não escreve
    python3 main.py -v         # logs DEBUG

Saída: JSON em uma linha no stdout. Logs no stderr.

Variáveis de ambiente:
    MAL_CLIENT_ID       - obrigatório
    MAL_CLIENT_SECRET   - obrigatório
    NOTION_TOKEN        - obrigatório
    NOTION_DB_ANIMES    - opcional (default hardcoded)

Bootstrap: rode `python3 get_token.py` localmente uma vez para gerar
mal_sync/.last_sync.json com os tokens iniciais. Após isso main.py rotaciona
o refresh_token automaticamente a cada refresh.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests


def _load_dotenv() -> None:
    """Load key=value pairs from /home/node/scripts/.env into os.environ."""
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

NOTION_DB_ANIMES = os.getenv(
    "NOTION_DB_ANIMES", "22ef090e-a3ca-8047-a25d-c116c21ef2d8"
)
NOTION_VERSION = "2022-06-28"

STATE_FILE = Path(__file__).parent / ".last_sync.json"

# Notion allows 3 req/s — sleep entre cada chamada de escrita.
NOTION_DELAY = 0.4

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0

MAL_OAUTH_URL = "https://myanimelist.net/v1/oauth2/token"
MAL_API_BASE = "https://api.myanimelist.net/v2"


# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("mal_sync")
_last_http_error: Optional[str] = None


def get_last_http_error() -> Optional[str]:
    global _last_http_error
    err = _last_http_error
    _last_http_error = None
    return err


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
    json_body: Optional[dict] = None,
    data: Optional[dict] = None,
    timeout: int = 30,
) -> Optional[dict]:
    """Wraps requests with retry on 429/5xx/network errors. Returns parsed JSON or None."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                timeout=timeout,
            )

            if resp.status_code == 429:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(f"429 em {url} — retry em {wait:.1f}s (tentativa {attempt})")
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(f"{resp.status_code} em {url} — retry em {wait:.1f}s")
                time.sleep(wait)
                continue

            if resp.status_code == 404:
                log.debug(f"404 em {url}")
                return None

            if 400 <= resp.status_code < 500:
                global _last_http_error
                body_preview = (resp.text or "")[:500]
                _last_http_error = f"{resp.status_code} {method}: {body_preview}"
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
# STATE (persisted in .last_sync.json)
# ============================================================


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"State file corrompido ({e}) — começando do zero")
        return {}


def save_state(state: dict) -> None:
    """Atomic write: tmp file + rename, evita corrupção em crash."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


# ============================================================
# MAL OAUTH
# ============================================================


class MALAuth:
    """OAuth com refresh automático. MAL rotaciona o refresh_token a cada chamada."""

    def __init__(self, state: dict):
        self.client_id = os.getenv("MAL_CLIENT_ID")
        self.client_secret = os.getenv("MAL_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise RuntimeError("MAL_CLIENT_ID e MAL_CLIENT_SECRET são obrigatórios")

        # Prefer state file (rotated tokens). Fall back to env on first run.
        self.access_token = state.get("mal_access_token")
        self.refresh_token = state.get("mal_refresh_token") or os.getenv("MAL_REFRESH_TOKEN")
        self.expires_at = state.get("mal_expires_at")  # ISO string

        if not self.refresh_token:
            raise RuntimeError(
                "MAL_REFRESH_TOKEN não configurado e não há token salvo em .last_sync.json"
            )

        self._state = state

    def _is_expired(self) -> bool:
        if not self.access_token or not self.expires_at:
            return True
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        # Refresh 5min antes do vencimento real, evita usar token expirado em corrida.
        return datetime.now(timezone.utc) >= exp - timedelta(minutes=5)

    def get_access_token(self) -> str:
        if self._is_expired():
            self._refresh()
        return self.access_token  # type: ignore[return-value]

    def _refresh(self) -> None:
        log.info("MAL: refreshing access token")
        body = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        # OAuth endpoint expects application/x-www-form-urlencoded.
        resp = http_request(
            "POST",
            MAL_OAUTH_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        if not resp or "access_token" not in resp:
            err = get_last_http_error() or "resposta vazia"
            raise RuntimeError(f"MAL OAuth refresh falhou: {err}")

        self.access_token = resp["access_token"]
        # MAL rotaciona o refresh_token a cada chamada — guardar o novo é crítico.
        self.refresh_token = resp.get("refresh_token", self.refresh_token)
        expires_in = int(resp.get("expires_in", 2592000))  # default 30 dias
        new_exp = datetime.now(timezone.utc).timestamp() + expires_in
        self.expires_at = datetime.fromtimestamp(new_exp, tz=timezone.utc).isoformat()

        # Persist immediately — perder o refresh_token novo significa re-auth manual.
        self._state["mal_access_token"] = self.access_token
        self._state["mal_refresh_token"] = self.refresh_token
        self._state["mal_expires_at"] = self.expires_at
        save_state(self._state)
        log.info(f"MAL: token renovado, expira em {self.expires_at}")

    def auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self.get_access_token()}"}


# ============================================================
# MAL FETCH
# ============================================================


def fetch_mal_list(auth: MALAuth, last_sync: Optional[str], full: bool) -> list[dict]:
    """
    Fetch user's animelist do MAL e aplica delta filter.

    Retorna entradas normalizadas: mal_id, title, mal_status, score,
    episodes_watched, updated_at. A lista vem ordenada por list_updated_at
    (mais recente primeiro), então paramos cedo quando passamos do cutoff.
    """
    url = (
        f"{MAL_API_BASE}/users/@me/animelist"
        "?fields=list_status{score,num_episodes_watched,status,updated_at}"
        "&nsfw=true&limit=100&sort=list_updated_at"
    )

    cutoff_ts = 0.0
    if last_sync and not full:
        try:
            cutoff_ts = datetime.fromisoformat(last_sync.replace("Z", "+00:00")).timestamp()
        except ValueError:
            log.warning(f"last_sync_time inválido ({last_sync}) — fazendo full sync")

    resp = http_request("GET", url, headers=auth.auth_header())
    if not resp:
        raise RuntimeError("MAL animelist falhou")

    out: list[dict] = []
    for entry in resp.get("data", []):
        node = entry.get("node") or {}
        status = entry.get("list_status") or {}
        if not node.get("id"):
            continue

        updated_at = status.get("updated_at")
        entry_ts = 0.0
        if updated_at:
            try:
                entry_ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass

        # Delta filter: lista vem ordenada desc por updated_at, então qualquer
        # entrada <= cutoff significa que todas as próximas também são — break.
        if cutoff_ts and entry_ts <= cutoff_ts:
            break

        out.append({
            "mal_id": str(node["id"]),
            "title": node.get("title") or "?",
            "mal_status": status.get("status") or "plan_to_watch",
            "score": status.get("score") or 0,
            "episodes_watched": status.get("num_episodes_watched") or 0,
            "updated_at": updated_at,
        })

    log.info(f"MAL delta: {len(out)} animes atualizados (cutoff={last_sync or 'none'})")
    return out


# ============================================================
# NOTION
# ============================================================


class NotionClient:
    BASE = "https://api.notion.com/v1"

    def __init__(self, token: str, dry_run: bool = False):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self.dry_run = dry_run

    def batch_find_pages_by_mal_ids(self, mal_ids: list[str]) -> dict[str, str]:
        """
        Busca múltiplos MAL_IDs em uma única query Notion usando OR filter.
        Retorna dict {mal_id -> page_id} para os que existirem.
        """
        if not mal_ids:
            return {}

        url = f"{self.BASE}/databases/{NOTION_DB_ANIMES}/query"
        # Notion aceita até 100 condições num OR filter
        chunk_size = 100
        result: dict[str, str] = {}

        for i in range(0, len(mal_ids), chunk_size):
            chunk = mal_ids[i:i + chunk_size]
            or_conditions = [
                {
                    "and": [
                        {"property": "MAL_ID", "rich_text": {"equals": mid}},
                        {"property": "Type", "select": {"equals": "Season"}},
                    ]
                }
                for mid in chunk
            ]
            body: dict = {
                "filter": {"or": or_conditions} if len(or_conditions) > 1 else or_conditions[0],
                "page_size": len(chunk),
            }
            resp = http_request("POST", url, headers=self.headers, json_body=body)
            time.sleep(NOTION_DELAY)
            if not resp:
                continue
            for page in resp.get("results", []):
                props = page.get("properties", {})
                mal_id_prop = props.get("MAL_ID", {})
                rich_texts = mal_id_prop.get("rich_text", [])
                if rich_texts:
                    mid = rich_texts[0].get("plain_text", "")
                    if mid:
                        result[mid] = page["id"]

        return result

    def update_anime(self, page_id: str, mal_status: str, episodes_watched: int, score: Optional[int]) -> bool:
        if self.dry_run:
            log.info(f"[DRY] update {page_id} status={mal_status} eps={episodes_watched} score={score}")
            return True
        url = f"{self.BASE}/pages/{page_id}"
        props: dict = {
            "MAL Status": {"select": {"name": mal_status}},
            "Episodes Watched": {"number": episodes_watched},
        }
        if score and score > 0:
            props["Rating"] = {"number": score}
        body = {"properties": props}
        resp = http_request("PATCH", url, headers=self.headers, json_body=body)
        time.sleep(NOTION_DELAY)
        return resp is not None

    def create_anime(self, mal_id: str, title: str, mal_status: str, episodes_watched: int, score: Optional[int]) -> Optional[str]:
        """Returns the created page_id, or None on failure."""
        if self.dry_run:
            log.info(f"[DRY] create {title} (MAL={mal_id})")
            return f"dry-run-{mal_id}"
        url = f"{self.BASE}/pages"
        props: dict = {
            "Name": {"title": [{"text": {"content": title}}]},
            "MAL_ID": {"rich_text": [{"text": {"content": mal_id}}]},
            "MAL Status": {"select": {"name": mal_status}},
            "Episodes Watched": {"number": episodes_watched},
        }
        if score and score > 0:
            props["Rating"] = {"number": score}
        body = {
            "parent": {"database_id": NOTION_DB_ANIMES},
            "properties": props,
        }
        resp = http_request("POST", url, headers=self.headers, json_body=body)
        time.sleep(NOTION_DELAY)
        if resp is None:
            return None
        return resp.get("id")


# ============================================================
# MAIN
# ============================================================


def resolve_notion_token() -> Optional[str]:
    """Lê NOTION_TOKEN ou extrai do OPENAPI_MCP_HEADERS (mesma lógica do anime_sync)."""
    tok = os.getenv("NOTION_TOKEN")
    if tok:
        return tok
    mcp_headers = os.getenv("OPENAPI_MCP_HEADERS", "")
    m = re.search(r'Bearer\s+(\S+?)["\s]', mcp_headers)
    if m:
        return m.group(1).rstrip('"')
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Ignora last_sync_time e processa todas as entradas")
    parser.add_argument("--dry-run", action="store_true", help="Não escreve no Notion")
    parser.add_argument("--limit", type=int, default=0, help="Processa no máximo N entries por run (0 = sem limite)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    notion_token = resolve_notion_token()
    if not notion_token:
        log.error("NOTION_TOKEN não configurado")
        print(json.dumps({"ok": False, "error": "missing_notion_token"}))
        return 1

    state = load_state()
    last_sync = state.get("last_sync_time")

    # Marca o início *antes* das chamadas pra não perder updates que aconteçam durante o run.
    run_start = datetime.now(timezone.utc).isoformat()

    try:
        auth = MALAuth(state)
    except RuntimeError as e:
        log.error(str(e))
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    notion = NotionClient(notion_token, dry_run=args.dry_run)

    try:
        mal_entries = fetch_mal_list(auth, last_sync, args.full)
        if args.limit and len(mal_entries) > args.limit:
            log.info(f"--limit {args.limit}: truncando {len(mal_entries)} → {args.limit} entries")
            mal_entries = mal_entries[:args.limit]
    except RuntimeError as e:
        log.error(str(e))
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    updated = 0
    created = 0
    errors: list[str] = []

    if not mal_entries:
        result = {
            "ok": True,
            "dry_run": args.dry_run,
            "timestamp": run_start,
            "mal_entries_after_delta": 0,
            "updated": 0,
            "created": 0,
            "errors": [],
            "message": "no MAL updates",
        }
        # Atualiza last_sync mesmo sem entradas — evita rever as mesmas no próximo run.
        if not args.dry_run:
            state["last_sync_time"] = run_start
            save_state(state)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    created_page_ids: list[str] = []

    # Batch lookup: uma query Notion pra todos os MAL_IDs de uma vez
    all_mal_ids = [e["mal_id"] for e in mal_entries]
    existing_pages = notion.batch_find_pages_by_mal_ids(all_mal_ids)
    log.info(f"Notion batch lookup: {len(existing_pages)} existentes de {len(all_mal_ids)} entries")

    for entry in mal_entries:
        mal_id = entry["mal_id"]
        title = entry["title"]
        score = entry["score"] if entry["score"] > 0 else None

        try:
            page_id = existing_pages.get(mal_id)
            if page_id:
                ok = notion.update_anime(
                    page_id,
                    entry["mal_status"],
                    entry["episodes_watched"],
                    score,
                )
                if ok:
                    updated += 1
                    log.info(f"  ✓ update: {title} [{entry['mal_status']} | eps={entry['episodes_watched']}]")
                else:
                    err = get_last_http_error() or "unknown"
                    errors.append(f"update_failed {title}: {err}")
            else:
                new_page_id = notion.create_anime(
                    mal_id,
                    title,
                    entry["mal_status"],
                    entry["episodes_watched"],
                    score,
                )
                if new_page_id:
                    created += 1
                    created_page_ids.append(new_page_id)
                    log.info(f"  ✓ create: {title} (page={new_page_id})")
                else:
                    err = get_last_http_error() or "unknown"
                    errors.append(f"create_failed {title}: {err}")
        except Exception as e:
            log.exception(f"Erro em {title}")
            errors.append(f"{title}: {e}")

    # Persist last_sync apenas se não for dry-run (do contrário um dry-run "consumiria" o delta).
    if not args.dry_run:
        state["last_sync_time"] = run_start
        save_state(state)

    result = {
        "ok": not bool(errors),
        "dry_run": args.dry_run,
        "timestamp": run_start,
        "mal_entries_after_delta": len(mal_entries),
        "updated": updated,
        "created": created,
        "created_page_ids": created_page_ids,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False))
    log.info(f"✅ Fim: updated={updated} created={created} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
