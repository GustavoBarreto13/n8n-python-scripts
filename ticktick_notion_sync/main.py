#!/usr/bin/env python3
"""
main.py (ticktick_notion_sync/main.py)
======================================
Sync bidirecional entre TickTick e a database "Tasks" do Notion.

Modos (via flags, mutuamente exclusivos):
    --direction tt-to-notion   Espelha mudanças do TickTick → Notion
    --direction notion-to-tt   Espelha mudanças do Notion → TickTick
    --bootstrap                Match por título + popula TickTick_ID em tasks existentes

Outros flags:
    --full                     Ignora o cutoff de last_sync_time
    --dry-run                  Loga, não escreve em nada
    -v / --verbose             Logs DEBUG

O mapeamento ticktick_id ↔ notion_page_id mora no próprio Notion: as
propriedades TickTick_ID e TickTick_Project_ID. Loop prevention usa um cache
local (`.sync_cache.json`) com os timestamps da última sync por entrada.
Tokens OAuth do TickTick e o last_sync_time ficam em `.last_sync.json`
(escrita atômica). Bootstrap inicial: rode `python3 get_token.py` uma vez.

Saída: JSON em uma única linha no stdout. Logs no stderr.

Variáveis de ambiente:
    TICKTICK_CLIENT_ID      - obrigatório
    TICKTICK_CLIENT_SECRET  - obrigatório
    NOTION_TOKEN            - obrigatório (fallback: OPENAPI_MCP_HEADERS)
    NOTION_DB_TASKS         - opcional, override do DB ID
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

NOTION_DB_TASKS = os.getenv(
    "NOTION_DB_TASKS", "17ef090ea3ca81829678e3ca4b82a8a6"
)
NOTION_VERSION = "2022-06-28"

STATE_FILE = Path(__file__).parent / ".last_sync.json"
CACHE_FILE = Path(__file__).parent / ".sync_cache.json"

NOTION_DELAY = 0.4
TICKTICK_DELAY = 0.2

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0

# Janela em que um update no destino logo após escrita do mesmo lado
# é tratado como eco e ignorado (loop prevention).
RECENT_SYNC_WINDOW = timedelta(minutes=10)

TICKTICK_OAUTH_URL = "https://ticktick.com/oauth/token"
TICKTICK_API_BASE = "https://api.ticktick.com/open/v1"

# Projetos TickTick que NÃO devem ser sincronizados.
IGNORED_PROJECTS = {
    "6861fbd9dca24397f378985b",  # 📅Google Calendar
    "6840ec738f0872956c61e890",  # 🗒Notion
}

# TickTick projectId → Notion Tag page ID. None = sem tag (Inbox/Misc).
PROJECT_TAG_MAP: dict[str, Optional[str]] = {
    "69e7f75bebd7ba0000000170": "249f090e-a3ca-801f-9900-e41c1b4d9aa1",  # Art / Hobbies
    "69e7f722ebd7ba0000000158": "17ef090e-a3ca-817b-83e4-d2646ea5bd5c",  # Knowledge
    "69e7f6fcebd7ba0000000136": "18ef090e-a3ca-80c2-8d7f-dddc3941cfab",  # Saúde
    "69e7f6d4ebd7ba0000000128": "249f090e-a3ca-8007-9533-f30ff0c10a7c",  # Finanças
    "69e7f665ebd7ba00000000d2": "18ef090e-a3ca-8091-816f-fcd510922d01",  # Pessoal
    "688fa749ebcebb000000056c": "34bf090e-a3ca-81fe-b23b-f315e52e650a",  # Lista de Compras
    "69e6693a8f08e6e745ec0032": "18ef090e-a3ca-8091-816f-fcd510922d01",  # 🎂 Aniversários → Pessoal
    "69f3cac2ebd7f6000000034e": "18ef090e-a3ca-80de-8ea8-ca9712491d19",  # 💼Work
    "6785ccb7c71c710000000221": None,                                    # 🏐Misc
    "inbox": None,
}

# Notion Tag page ID → TickTick projectId (default quando criando do Notion).
# Quando uma tag mapeia pra múltiplos projetos no PROJECT_TAG_MAP (ex: Pessoal),
# escolhemos o mais "principal" — Pessoal-Social, não Aniversários.
TAG_PROJECT_MAP: dict[str, str] = {
    "249f090e-a3ca-801f-9900-e41c1b4d9aa1": "69e7f75bebd7ba0000000170",  # Art / Hobbies
    "17ef090e-a3ca-817b-83e4-d2646ea5bd5c": "69e7f722ebd7ba0000000158",  # Knowledge
    "18ef090e-a3ca-80c2-8d7f-dddc3941cfab": "69e7f6fcebd7ba0000000136",  # Saúde
    "249f090e-a3ca-8007-9533-f30ff0c10a7c": "69e7f6d4ebd7ba0000000128",  # Finanças
    "18ef090e-a3ca-8091-816f-fcd510922d01": "69e7f665ebd7ba00000000d2",  # Pessoal-Social
    "34bf090e-a3ca-81fe-b23b-f315e52e650a": "688fa749ebcebb000000056c",  # Lista de Compras
    "18ef090e-a3ca-80de-8ea8-ca9712491d19": "69f3cac2ebd7f6000000034e",  # Work
}

# TickTick priority (0/1/3/5) → Notion select option name.
PRIORITY_TT_TO_NOTION = {
    0: "🧀Medium",  # default quando o TickTick não tem prioridade definida
    1: "🧊Low",
    3: "🧀Medium",
    5: "🚨High",
}

# Notion Priority → TickTick numeric priority.
PRIORITY_NOTION_TO_TT = {
    "💡Idea": 0,
    "🧊Low": 1,
    "🧀Medium": 3,
    "🚨High": 5,
    "☠️Mandatory": 5,
}

# Notion Tasks DB property names.
PROP_NAME = "Name"
PROP_TAGS = "Tags"
PROP_LABELS = "Labels"
PROP_PRIORITY = "Priority"
PROP_DUE = "Due"
PROP_DESCRIPTION = "Description"
PROP_STATUS = "Status"
PROP_COMPLETED = "Completed"
PROP_TICKTICK_ID = "TickTick_ID"
PROP_TICKTICK_PROJECT_ID = "TickTick_Project_ID"

# Status options agrupados (para preservar Backlog/Blocked/Doing/Homolog quando
# o TickTick continua "active" e o Notion já tem um valor refinado).
NOTION_STATUS_DONE = "Done"
NOTION_STATUS_TODO_DEFAULT = "To Do"
NOTION_STATUS_TODO_GROUP = {"Backlog", "Blocked", "To Do"}
NOTION_STATUS_IN_PROGRESS_GROUP = {"Doing", "Homolog"}


# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("ticktick_notion_sync")
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
# STATE & CACHE
# ============================================================


def _load_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"{path.name} corrompido ({e}) — começando do zero")
        return {}


def _save_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def load_state() -> dict:
    return _load_json_file(STATE_FILE)


def save_state(state: dict) -> None:
    _save_json_atomic(STATE_FILE, state)


def load_cache() -> dict:
    return _load_json_file(CACHE_FILE)


def save_cache(cache: dict) -> None:
    _save_json_atomic(CACHE_FILE, cache)


# ============================================================
# TICKTICK OAUTH
# ============================================================


class TickTickAuth:
    """OAuth com refresh automático. Padrão idêntico ao MALAuth."""

    def __init__(self, state: dict):
        self.client_id = os.getenv("TICKTICK_CLIENT_ID")
        self.client_secret = os.getenv("TICKTICK_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise RuntimeError("TICKTICK_CLIENT_ID e TICKTICK_CLIENT_SECRET são obrigatórios")

        self.access_token = state.get("ticktick_access_token") or os.getenv("TICKTICK_ACCESS_TOKEN")
        self.refresh_token = state.get("ticktick_refresh_token") or os.getenv("TICKTICK_REFRESH_TOKEN")
        self.expires_at = state.get("ticktick_expires_at") or os.getenv("TICKTICK_EXPIRES_AT")

        if not self.refresh_token and not self.access_token:
            raise RuntimeError(
                "Nenhum token TickTick disponível. Configure TICKTICK_ACCESS_TOKEN (ou TICKTICK_REFRESH_TOKEN) "
                "no ambiente, ou rode `python3 get_token.py` localmente e copie o .last_sync.json pro VPS."
            )

        self._state = state

    def _is_expired(self) -> bool:
        if not self.access_token:
            return True
        if not self.expires_at:
            return False  # sem data de expiração, assume válido
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= exp - timedelta(minutes=5)

    def get_access_token(self) -> str:
        if self._is_expired():
            if not self.refresh_token:
                raise RuntimeError(
                    "Access token expirado e TICKTICK_REFRESH_TOKEN não disponível. "
                    "Rode `python3 get_token.py` novamente para renovar."
                )
            self._refresh()
        return self.access_token  # type: ignore[return-value]

    def _refresh(self) -> None:
        log.info("TickTick: refreshing access token")
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        resp = http_request(
            "POST",
            TICKTICK_OAUTH_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        if not resp or "access_token" not in resp:
            err = get_last_http_error() or "resposta vazia"
            raise RuntimeError(f"TickTick OAuth refresh falhou: {err}")

        self.access_token = resp["access_token"]
        # TickTick pode rotacionar o refresh_token — preserva o novo se vier.
        self.refresh_token = resp.get("refresh_token", self.refresh_token)
        expires_in = int(resp.get("expires_in", 15552000))  # default 180 dias
        new_exp = datetime.now(timezone.utc).timestamp() + expires_in
        self.expires_at = datetime.fromtimestamp(new_exp, tz=timezone.utc).isoformat()

        self._state["ticktick_access_token"] = self.access_token
        self._state["ticktick_refresh_token"] = self.refresh_token
        self._state["ticktick_expires_at"] = self.expires_at
        save_state(self._state)
        log.info(f"TickTick: token renovado, expira em {self.expires_at}")

    def auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self.get_access_token()}"}


# ============================================================
# TICKTICK CLIENT
# ============================================================


class TickTickClient:
    BASE = TICKTICK_API_BASE

    def __init__(self, auth: TickTickAuth, dry_run: bool = False):
        self.auth = auth
        self.dry_run = dry_run

    def _headers(self) -> dict:
        h = self.auth.auth_header()
        h["Content-Type"] = "application/json"
        return h

    def list_projects(self) -> list[dict]:
        resp = http_request("GET", f"{self.BASE}/project", headers=self._headers())
        time.sleep(TICKTICK_DELAY)
        return resp if isinstance(resp, list) else []

    def get_project_data(self, project_id: str) -> Optional[dict]:
        """Retorna {project, tasks, columns} — apenas tasks NÃO completadas."""
        resp = http_request(
            "GET",
            f"{self.BASE}/project/{project_id}/data",
            headers=self._headers(),
        )
        time.sleep(TICKTICK_DELAY)
        return resp

    def get_task(self, project_id: str, task_id: str) -> Optional[dict]:
        resp = http_request(
            "GET",
            f"{self.BASE}/project/{project_id}/task/{task_id}",
            headers=self._headers(),
        )
        time.sleep(TICKTICK_DELAY)
        return resp

    def create_task(self, payload: dict) -> Optional[dict]:
        if self.dry_run:
            log.info(f"[DRY] TT create: {payload.get('title')!r} project={payload.get('projectId')}")
            return {"id": f"dry-{payload.get('title','x')}", "projectId": payload.get("projectId")}
        resp = http_request("POST", f"{self.BASE}/task", headers=self._headers(), json_body=payload)
        time.sleep(TICKTICK_DELAY)
        return resp

    def update_task(self, task_id: str, payload: dict) -> Optional[dict]:
        if self.dry_run:
            log.info(f"[DRY] TT update {task_id}: {payload.get('title')!r}")
            return {"id": task_id}
        resp = http_request(
            "POST",
            f"{self.BASE}/task/{task_id}",
            headers=self._headers(),
            json_body=payload,
        )
        time.sleep(TICKTICK_DELAY)
        return resp

    def complete_task(self, project_id: str, task_id: str) -> bool:
        if self.dry_run:
            log.info(f"[DRY] TT complete {task_id} (project {project_id})")
            return True
        resp = http_request(
            "POST",
            f"{self.BASE}/project/{project_id}/task/{task_id}/complete",
            headers=self._headers(),
        )
        time.sleep(TICKTICK_DELAY)
        # /complete retorna 200 com body vazio no sucesso → http_request retorna {}.
        return resp is not None


# ============================================================
# NOTION CLIENT
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

    def query_db(self, body: dict) -> list[dict]:
        """Pagina cursor-based, retorna todas as páginas que casam com o filtro."""
        url = f"{self.BASE}/databases/{NOTION_DB_TASKS}/query"
        results: list[dict] = []
        cursor: Optional[str] = None
        while True:
            req = dict(body)
            if cursor:
                req["start_cursor"] = cursor
            resp = http_request("POST", url, headers=self.headers, json_body=req)
            time.sleep(NOTION_DELAY)
            if not resp:
                break
            results.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
            if not cursor:
                break
        return results

    def find_by_ticktick_id(self, ticktick_id: str) -> Optional[dict]:
        body = {
            "filter": {
                "property": PROP_TICKTICK_ID,
                "rich_text": {"equals": ticktick_id},
            },
            "page_size": 1,
        }
        results = self.query_db(body)
        return results[0] if results else None

    def find_by_title(self, title: str) -> Optional[dict]:
        """Usado no bootstrap pra match por título quando ainda não há TickTick_ID."""
        body = {
            "filter": {"property": PROP_NAME, "title": {"equals": title}},
            "page_size": 1,
        }
        results = self.query_db(body)
        return results[0] if results else None

    def list_edited_after(self, since_iso: Optional[str]) -> list[dict]:
        """Tasks editadas desde `since_iso`. Se since_iso for None, retorna todas."""
        body: dict = {"page_size": 100}
        if since_iso:
            body["filter"] = {
                "timestamp": "last_edited_time",
                "last_edited_time": {"on_or_after": since_iso},
            }
            body["sorts"] = [{"timestamp": "last_edited_time", "direction": "ascending"}]
        return self.query_db(body)

    def list_unmapped(self) -> list[dict]:
        """Tasks sem TickTick_ID — usado no bootstrap pra propagar Notion → TickTick."""
        body = {
            "filter": {"property": PROP_TICKTICK_ID, "rich_text": {"is_empty": True}},
            "page_size": 100,
        }
        return self.query_db(body)

    def create_page(self, properties: dict) -> Optional[dict]:
        if self.dry_run:
            title = _extract_title_text(properties.get(PROP_NAME))
            log.info(f"[DRY] Notion create: {title!r}")
            return {"id": f"dry-{title}", "last_edited_time": _now_iso()}
        body = {
            "parent": {"database_id": NOTION_DB_TASKS},
            "properties": properties,
        }
        resp = http_request("POST", f"{self.BASE}/pages", headers=self.headers, json_body=body)
        time.sleep(NOTION_DELAY)
        return resp

    def update_page(self, page_id: str, properties: dict) -> Optional[dict]:
        if self.dry_run:
            log.info(f"[DRY] Notion update {page_id}: keys={list(properties)}")
            return {"id": page_id, "last_edited_time": _now_iso()}
        body = {"properties": properties}
        resp = http_request(
            "PATCH",
            f"{self.BASE}/pages/{page_id}",
            headers=self.headers,
            json_body=body,
        )
        time.sleep(NOTION_DELAY)
        return resp

    def get_page_blocks(self, page_id: str) -> list[dict]:
        """GET /blocks/{page_id}/children — cursor-paged, returns all block objects."""
        url = f"{self.BASE}/blocks/{page_id}/children"
        results: list[dict] = []
        cursor: Optional[str] = None
        while True:
            params: dict = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = http_request("GET", url, headers=self.headers, params=params)
            time.sleep(NOTION_DELAY)
            if not resp:
                break
            results.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
            if not cursor:
                break
        return results

    def delete_block(self, block_id: str) -> bool:
        """DELETE /blocks/{block_id}. Returns True on success."""
        if self.dry_run:
            log.debug(f"[DRY] Notion delete block {block_id}")
            return True
        resp = http_request("DELETE", f"{self.BASE}/blocks/{block_id}", headers=self.headers)
        time.sleep(NOTION_DELAY)
        return resp is not None

    def append_blocks(self, page_id: str, children: list[dict]) -> Optional[dict]:
        """PATCH /blocks/{page_id}/children — appends block objects to the page body."""
        if self.dry_run:
            log.debug(f"[DRY] Notion append {len(children)} blocks to {page_id}")
            return {"results": []}
        resp = http_request(
            "PATCH",
            f"{self.BASE}/blocks/{page_id}/children",
            headers=self.headers,
            json_body={"children": children},
        )
        time.sleep(NOTION_DELAY)
        return resp


# ============================================================
# FIELD TRANSFORMATIONS
# ============================================================


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_brt_date() -> str:
    """Data de hoje no fuso BRT (UTC-3), formato YYYY-MM-DD."""
    brt = datetime.now(timezone.utc) - timedelta(hours=3)
    return brt.date().isoformat()


def _extract_title_text(prop: Optional[dict]) -> str:
    if not prop:
        return ""
    arr = prop.get("title") or []
    return "".join(item.get("plain_text", "") for item in arr)


def _extract_rich_text(prop: Optional[dict]) -> str:
    if not prop:
        return ""
    arr = prop.get("rich_text") or []
    return "".join(item.get("plain_text", "") for item in arr)


def _extract_select_name(prop: Optional[dict]) -> Optional[str]:
    if not prop:
        return None
    sel = prop.get("select")
    return sel.get("name") if sel else None


def _extract_status_name(prop: Optional[dict]) -> Optional[str]:
    if not prop:
        return None
    sel = prop.get("status")
    return sel.get("name") if sel else None


def _extract_multi_select(prop: Optional[dict]) -> list[str]:
    if not prop:
        return []
    arr = prop.get("multi_select") or []
    return [item["name"] for item in arr if item.get("name")]


def _extract_relation_ids(prop: Optional[dict]) -> list[str]:
    if not prop:
        return []
    arr = prop.get("relation") or []
    return [item["id"] for item in arr if item.get("id")]


def _extract_date_start(prop: Optional[dict]) -> Optional[str]:
    if not prop:
        return None
    d = prop.get("date")
    return d.get("start") if d else None


def map_priority_tt_to_notion(priority: int) -> str:
    return PRIORITY_TT_TO_NOTION.get(priority, "🧀Medium")


def map_priority_notion_to_tt(name: Optional[str]) -> int:
    if not name:
        return 0
    return PRIORITY_NOTION_TO_TT.get(name, 0)


def normalize_id(s: str) -> str:
    """Notion devolve UUIDs com hífens; comparação precisa ser tolerante."""
    return (s or "").replace("-", "").lower()


def ticktick_due_to_notion_date(due: Optional[str]) -> Optional[dict]:
    """TickTick manda dueDate ISO 8601 com offset (ex: 2026-04-23T09:00:00.000+0000).
    Notion aceita o mesmo formato. Se for null, devolve None pra limpar o campo."""
    if not due:
        return None
    return {"start": due}


def notion_due_to_ticktick(date_prop: Optional[dict]) -> Optional[str]:
    if not date_prop:
        return None
    d = date_prop.get("date")
    if not d or not d.get("start"):
        return None
    start = d["start"]
    # Notion pode mandar "2026-04-23" (sem hora). TickTick aceita ISO completo.
    if "T" not in start:
        return f"{start}T09:00:00+0000"
    return start


def ticktick_items_to_notion_blocks(items: list[dict]) -> list[dict]:
    """Converts TickTick checklist items[] to Notion to_do block dicts, sorted by sortOrder."""
    sorted_items = sorted(items, key=lambda x: x.get("sortOrder", 0))
    blocks = []
    for item in sorted_items:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        checked = int(item.get("status", 0)) in (1, 2)
        blocks.append({
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": [{"type": "text", "text": {"content": title[:2000]}}],
                "checked": checked,
            },
        })
    return blocks


def extract_todo_blocks(blocks: list[dict]) -> list[dict]:
    """Filters a raw Notion block list to only to_do typed blocks."""
    return [b for b in blocks if b.get("type") == "to_do"]


def notion_blocks_to_ticktick_items(todo_blocks: list[dict]) -> list[dict]:
    """Converts Notion to_do blocks to TickTick items[] payload dicts."""
    items = []
    for i, block in enumerate(todo_blocks):
        td = block.get("to_do") or {}
        rich_text = td.get("rich_text") or []
        title = "".join(rt.get("plain_text", "") for rt in rich_text).strip()
        if not title:
            continue
        checked = bool(td.get("checked", False))
        items.append({
            "id": block.get("id", f"notion-{i}"),
            "title": title,
            "status": 2 if checked else 0,
            "sortOrder": i * 1000,
        })
    return items


def sync_checklist_tt_to_notion(
    notion: "NotionClient",
    page_id: str,
    items: list[dict],
    dry_run: bool,
) -> bool:
    """
    Replaces all to_do blocks on a Notion page with blocks built from TickTick items[].
    Strategy: fetch existing → delete to_do blocks → append new ones.
    Non-to_do blocks are left untouched. Returns True on full success.
    """
    existing = notion.get_page_blocks(page_id)
    todo_existing = extract_todo_blocks(existing)

    for block in todo_existing:
        bid = block.get("id")
        if bid and not notion.delete_block(bid):
            log.warning(f"Failed to delete block {bid} on page {page_id}")

    if not items:
        return True

    new_blocks = ticktick_items_to_notion_blocks(items)
    if not new_blocks:
        return True

    resp = notion.append_blocks(page_id, new_blocks)
    if resp is None:
        log.error(f"append_blocks failed for page {page_id}")
        return False
    return True


def ticktick_to_notion_props(task: dict, project_id: str) -> dict:
    """Constrói o dict `properties` pra Notion a partir de uma task TickTick."""
    title = task.get("title") or ""
    content = task.get("content") or task.get("desc") or ""
    priority = int(task.get("priority") or 0)
    status = int(task.get("status") or 0)
    tags = task.get("tags") or []
    due = task.get("dueDate")

    props: dict = {
        PROP_NAME: {"title": [{"text": {"content": title[:2000]}}]},
        PROP_TICKTICK_ID: {"rich_text": [{"text": {"content": task.get("id") or ""}}]},
        PROP_TICKTICK_PROJECT_ID: {"rich_text": [{"text": {"content": project_id}}]},
        PROP_PRIORITY: {"select": {"name": map_priority_tt_to_notion(priority)}},
        PROP_LABELS: {"multi_select": [{"name": t} for t in tags if t]},
    }

    if content:
        props[PROP_DESCRIPTION] = {"rich_text": [{"text": {"content": content[:2000]}}]}

    if due:
        props[PROP_DUE] = {"date": ticktick_due_to_notion_date(due)}

    tag_id = PROJECT_TAG_MAP.get(project_id)
    if tag_id:
        props[PROP_TAGS] = {"relation": [{"id": tag_id}]}

    if status == 2:
        props[PROP_STATUS] = {"status": {"name": NOTION_STATUS_DONE}}
        props[PROP_COMPLETED] = {"date": {"start": _today_brt_date()}}
    # status=0 (active): NÃO seta status — preserva qualquer valor já existente
    # no Notion (Backlog/Doing/Homolog). Para create, _ensure_active_status() abaixo seta To Do.

    return props


def _ensure_active_status(props: dict) -> None:
    """Seta Status=To Do se ainda não houver — usado em creates."""
    props.setdefault(PROP_STATUS, {"status": {"name": NOTION_STATUS_TODO_DEFAULT}})


def notion_to_ticktick_payload(page: dict) -> dict:
    """Constrói o body POST /task a partir de uma página Notion. Não inclui id/projectId."""
    props = page.get("properties") or {}
    title = _extract_title_text(props.get(PROP_NAME))
    description = _extract_rich_text(props.get(PROP_DESCRIPTION))
    priority_name = _extract_select_name(props.get(PROP_PRIORITY))
    due = notion_due_to_ticktick(props.get(PROP_DUE))
    labels = _extract_multi_select(props.get(PROP_LABELS))

    payload: dict = {
        "title": title or "(Sem título)",
        "priority": map_priority_notion_to_tt(priority_name),
    }
    if description:
        payload["content"] = description
    if due:
        payload["dueDate"] = due
    if labels:
        payload["tags"] = labels

    return payload


def resolve_ticktick_project_for_notion_page(page: dict) -> str:
    """Tag → TickTick projectId. Sem tag conhecida → 'inbox'."""
    props = page.get("properties") or {}
    tag_ids = _extract_relation_ids(props.get(PROP_TAGS))
    for tid in tag_ids:
        norm = normalize_id(tid)
        for k, v in TAG_PROJECT_MAP.items():
            if normalize_id(k) == norm:
                return v
    return "inbox"


# ============================================================
# LOOP PREVENTION
# ============================================================


def should_skip_sync(
    cache_entry: Optional[dict],
    direction: str,
    source_modified: Optional[str],
) -> tuple[bool, str]:
    """
    Retorna (skip?, reason). direction = "ticktick" (TT é a source) ou "notion".
    Regras:
      1. Se a source não mudou desde a última sync → skip.
      2. Se a última sync foi do lado oposto há menos de RECENT_SYNC_WINDOW
         e o modified do destino bate com o que a gente escreveu → skip (eco).
    """
    if not cache_entry:
        return False, "no-cache"

    field = "ticktick_modified" if direction == "ticktick" else "notion_modified"
    if source_modified and cache_entry.get(field) == source_modified:
        return True, "unchanged"

    last_by = cache_entry.get("last_synced_by")
    last_at = cache_entry.get("last_synced_at")
    opposite = "notion" if direction == "ticktick" else "ticktick"
    if last_by == opposite and last_at:
        try:
            ts = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - ts < RECENT_SYNC_WINDOW:
                return True, f"recent-echo-from-{opposite}"
        except ValueError:
            pass

    return False, "ok"


def update_cache_entry(
    cache: dict,
    ticktick_id: str,
    *,
    last_synced_by: str,
    ticktick_modified: Optional[str] = None,
    notion_modified: Optional[str] = None,
) -> None:
    entry = cache.get(ticktick_id) or {}
    if ticktick_modified is not None:
        entry["ticktick_modified"] = ticktick_modified
    if notion_modified is not None:
        entry["notion_modified"] = notion_modified
    entry["last_synced_by"] = last_synced_by
    entry["last_synced_at"] = _now_iso()
    cache[ticktick_id] = entry


# ============================================================
# RUNNERS
# ============================================================


def run_tt_to_notion(
    tt: TickTickClient,
    notion: NotionClient,
    cache: dict,
    last_sync: Optional[str],
    full: bool,
) -> dict:
    """Itera projetos não-ignorados, sincroniza tasks ativas pro Notion."""
    cutoff_ts = 0.0
    if last_sync and not full:
        try:
            cutoff_ts = datetime.fromisoformat(last_sync.replace("Z", "+00:00")).timestamp()
        except ValueError:
            log.warning(f"last_sync_time inválido ({last_sync}) — fazendo full sync")

    scanned = 0
    created = 0
    updated = 0
    completed = 0
    skipped = 0
    errors: list[str] = []
    seen_ttids: set[str] = set()

    projects = tt.list_projects()
    log.info(f"TickTick: {len(projects)} projetos retornados")

    project_ids = [p["id"] for p in projects if p.get("id")]
    project_ids.append("inbox")

    for project_id in project_ids:
        if project_id in IGNORED_PROJECTS:
            continue
        if project_id not in PROJECT_TAG_MAP:
            log.warning(f"Projeto sem mapping: {project_id} — ignorando")
            continue

        data = tt.get_project_data(project_id)
        if not data:
            continue
        tasks = data.get("tasks") or []
        log.debug(f"  project {project_id}: {len(tasks)} tasks")

        for task in tasks:
            ttid = task.get("id")
            if not ttid:
                continue
            scanned += 1
            seen_ttids.add(ttid)
            modified = task.get("modifiedTime")

            # Cutoff por last_sync_time (rápido, antes de qualquer query a Notion).
            if cutoff_ts and modified:
                try:
                    mt = datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp()
                    if mt <= cutoff_ts:
                        skipped += 1
                        continue
                except ValueError:
                    pass

            entry = cache.get(ttid)
            skip, reason = should_skip_sync(entry, "ticktick", modified)
            if skip:
                log.debug(f"    skip {ttid}: {reason}")
                skipped += 1
                continue

            try:
                page = notion.find_by_ticktick_id(ttid)
                props = ticktick_to_notion_props(task, project_id)

                if page:
                    # Update: nunca seta Status quando TT está active (status=0),
                    # pra preservar Backlog/Doing/Homolog manuais.
                    page_id = page["id"]
                    result = notion.update_page(page_id, props)
                    if result:
                        updated += 1
                        log.info(f"  ✓ Notion update: {task.get('title')!r}")
                        update_cache_entry(
                            cache,
                            ttid,
                            last_synced_by="ticktick",
                            ticktick_modified=modified,
                            notion_modified=result.get("last_edited_time"),
                        )
                        items = task.get("items") or []
                        if not sync_checklist_tt_to_notion(notion, page_id, items, notion.dry_run):
                            errors.append(f"checklist_failed {ttid}")
                    else:
                        err = get_last_http_error() or "unknown"
                        errors.append(f"update_failed {ttid}: {err}")
                else:
                    _ensure_active_status(props)
                    result = notion.create_page(props)
                    if result and result.get("id"):
                        created += 1
                        log.info(f"  ✓ Notion create: {task.get('title')!r} (page={result['id']})")
                        update_cache_entry(
                            cache,
                            ttid,
                            last_synced_by="ticktick",
                            ticktick_modified=modified,
                            notion_modified=result.get("last_edited_time"),
                        )
                        items = task.get("items") or []
                        if items:
                            new_blocks = ticktick_items_to_notion_blocks(items)
                            if new_blocks and notion.append_blocks(result["id"], new_blocks) is None:
                                errors.append(f"checklist_create_failed {ttid}")
                    else:
                        err = get_last_http_error() or "unknown"
                        errors.append(f"create_failed {ttid}: {err}")
            except Exception as e:
                log.exception(f"Erro processando TT task {ttid}")
                errors.append(f"{ttid}: {e}")

    # Detecta tasks completadas: cache tem ttid mas o scan não retornou
    # (a API /project/{id}/data omite tasks completas). Confirmamos com GET individual.
    # Otimização: só verifica ttids no cache cujo last_synced_by != "ticktick" com modified
    # mais recente que cache (ou seja, foram vistas ativas antes e sumiram agora).
    missing_ttids = [tid for tid in cache.keys() if tid not in seen_ttids]
    if missing_ttids:
        log.info(f"Verificando {len(missing_ttids)} tasks ausentes do scan (possíveis completed)…")

        # Busca todas as páginas Notion não-Done com TickTick_ID em uma única query paginada.
        # Filtra por status != Done para evitar trabalho desnecessário em tasks já completadas.
        not_done_pages = notion.query_db({
            "filter": {
                "and": [
                    {"property": PROP_TICKTICK_ID, "rich_text": {"is_not_empty": True}},
                    {"property": PROP_STATUS, "status": {"does_not_equal": NOTION_STATUS_DONE}},
                ]
            },
            "page_size": 100,
        })
        ttid_to_page: dict[str, dict] = {}
        for pg in not_done_pages:
            tid = _extract_rich_text((pg.get("properties") or {}).get(PROP_TICKTICK_ID))
            if tid:
                ttid_to_page[tid] = pg

        for ttid in missing_ttids:
            page = ttid_to_page.get(ttid)
            if not page:
                continue  # ou já está Done no Notion, ou não existe
            try:
                tt_project = _extract_rich_text((page.get("properties") or {}).get(PROP_TICKTICK_PROJECT_ID))
                if not tt_project:
                    continue
                task = tt.get_task(tt_project, ttid)
                if not task:
                    continue  # task deletada de fato, não completed
                if int(task.get("status", 0)) != 2:
                    continue

                done_props = {
                    PROP_STATUS: {"status": {"name": NOTION_STATUS_DONE}},
                    PROP_COMPLETED: {"date": {"start": _today_brt_date()}},
                }
                result = notion.update_page(page["id"], done_props)
                if result:
                    completed += 1
                    log.info(f"  ✓ Notion complete: {_extract_title_text((page.get('properties') or {}).get(PROP_NAME)) or ttid!r}")
                    update_cache_entry(
                        cache,
                        ttid,
                        last_synced_by="ticktick",
                        ticktick_modified=task.get("modifiedTime"),
                        notion_modified=result.get("last_edited_time"),
                    )
                else:
                    err = get_last_http_error() or "unknown"
                    errors.append(f"complete_failed {ttid}: {err}")
            except Exception as e:
                log.exception(f"Erro verificando completed para {ttid}")
                errors.append(f"complete_check {ttid}: {e}")

    return {
        "scanned": scanned,
        "created": created,
        "updated": updated,
        "completed": completed,
        "skipped": skipped,
        "errors": errors,
    }


def run_notion_to_tt(
    tt: TickTickClient,
    notion: NotionClient,
    cache: dict,
    last_sync: Optional[str],
    full: bool,
) -> dict:
    """Itera tasks editadas no Notion, propaga pro TickTick."""
    since = None if full else last_sync
    pages = notion.list_edited_after(since)
    log.info(f"Notion: {len(pages)} tasks editadas desde {since or 'sempre'}")

    scanned = 0
    created = 0
    updated = 0
    completed = 0
    skipped = 0
    errors: list[str] = []

    for page in pages:
        scanned += 1
        page_id = page["id"]
        props = page.get("properties") or {}
        ttid = _extract_rich_text(props.get(PROP_TICKTICK_ID))
        tt_project = _extract_rich_text(props.get(PROP_TICKTICK_PROJECT_ID))
        notion_modified = page.get("last_edited_time")
        title = _extract_title_text(props.get(PROP_NAME)) or "(sem título)"
        status = _extract_status_name(props.get(PROP_STATUS))

        try:
            if ttid:
                entry = cache.get(ttid)
                skip, reason = should_skip_sync(entry, "notion", notion_modified)
                if skip:
                    log.debug(f"    skip {page_id}: {reason}")
                    skipped += 1
                    continue

                payload = notion_to_ticktick_payload(page)
                payload["id"] = ttid
                if tt_project:
                    payload["projectId"] = tt_project

                blocks = notion.get_page_blocks(page_id)
                todo_blocks = extract_todo_blocks(blocks)
                if todo_blocks:
                    payload["items"] = notion_blocks_to_ticktick_items(todo_blocks)

                result = tt.update_task(ttid, payload)
                if result:
                    updated += 1
                    log.info(f"  ✓ TT update: {title!r}")
                    # GET confirma o modifiedTime real que o /project/{id}/data vai retornar.
                    # update_task pode retornar modifiedTime ligeiramente diferente (ou None).
                    confirmed = tt.get_task(tt_project or result.get("projectId", ""), ttid) if tt_project or result.get("projectId") else None
                    tt_modified = (confirmed or result).get("modifiedTime")
                    update_cache_entry(
                        cache,
                        ttid,
                        last_synced_by="notion",
                        ticktick_modified=tt_modified,
                        notion_modified=notion_modified,
                    )
                else:
                    err = get_last_http_error() or "unknown"
                    errors.append(f"update_failed {ttid}: {err}")
                    continue

                # Completion: só dispara em Done. Outras transições ficam só no Notion.
                if status == NOTION_STATUS_DONE and tt_project:
                    if tt.complete_task(tt_project, ttid):
                        completed += 1
                        log.info(f"  ✓ TT complete: {title!r}")
            else:
                # Task nova no Notion → cria no TickTick e popula TickTick_ID.
                payload = notion_to_ticktick_payload(page)
                project = resolve_ticktick_project_for_notion_page(page)
                if project != "inbox":
                    payload["projectId"] = project

                blocks = notion.get_page_blocks(page_id)
                todo_blocks = extract_todo_blocks(blocks)
                if todo_blocks:
                    payload["items"] = notion_blocks_to_ticktick_items(todo_blocks)

                result = tt.create_task(payload)
                if not result or not result.get("id"):
                    err = get_last_http_error() or "unknown"
                    errors.append(f"create_failed page={page_id}: {err}")
                    continue
                new_ttid = result["id"]
                new_project = result.get("projectId") or project
                created += 1
                log.info(f"  ✓ TT create: {title!r} (id={new_ttid})")

                # Escreve TickTick_ID de volta no Notion.
                back_props = {
                    PROP_TICKTICK_ID: {"rich_text": [{"text": {"content": new_ttid}}]},
                    PROP_TICKTICK_PROJECT_ID: {"rich_text": [{"text": {"content": new_project}}]},
                }
                upd = notion.update_page(page_id, back_props)
                confirmed_create = tt.get_task(new_project, new_ttid)
                update_cache_entry(
                    cache,
                    new_ttid,
                    last_synced_by="notion",
                    ticktick_modified=(confirmed_create or result).get("modifiedTime"),
                    notion_modified=(upd or {}).get("last_edited_time", notion_modified),
                )

                if status == NOTION_STATUS_DONE:
                    if tt.complete_task(new_project, new_ttid):
                        completed += 1
        except Exception as e:
            log.exception(f"Erro processando Notion page {page_id}")
            errors.append(f"{page_id}: {e}")

    return {
        "scanned": scanned,
        "created": created,
        "updated": updated,
        "completed": completed,
        "skipped": skipped,
        "errors": errors,
    }


def run_bootstrap(
    tt: TickTickClient,
    notion: NotionClient,
    cache: dict,
) -> dict:
    """
    Match por título entre TickTick e Notion, popula TickTick_ID nas tasks que
    bateram. Tasks só no TickTick → cria no Notion. Tasks só no Notion → cria
    no TickTick (e popula TickTick_ID de volta).
    """
    matched = 0
    created_in_notion = 0
    created_in_tt = 0
    errors: list[str] = []

    # Phase 1: TickTick → Notion. Itera projetos, tenta achar por TickTick_ID
    # primeiro; se não achar, faz match por título; se não achar, cria.
    projects = tt.list_projects()
    project_ids = [p["id"] for p in projects if p.get("id")]
    project_ids.append("inbox")

    for project_id in project_ids:
        if project_id in IGNORED_PROJECTS or project_id not in PROJECT_TAG_MAP:
            continue
        data = tt.get_project_data(project_id)
        if not data:
            continue
        for task in data.get("tasks") or []:
            ttid = task.get("id")
            title = task.get("title") or ""
            if not ttid:
                continue

            try:
                page = notion.find_by_ticktick_id(ttid)
                if page:
                    update_cache_entry(
                        cache,
                        ttid,
                        last_synced_by="bootstrap",
                        ticktick_modified=task.get("modifiedTime"),
                        notion_modified=page.get("last_edited_time"),
                    )
                    matched += 1
                    continue

                page = notion.find_by_title(title)
                if page:
                    page_id = page["id"]
                    upd = notion.update_page(page_id, {
                        PROP_TICKTICK_ID: {"rich_text": [{"text": {"content": ttid}}]},
                        PROP_TICKTICK_PROJECT_ID: {"rich_text": [{"text": {"content": project_id}}]},
                    })
                    matched += 1
                    log.info(f"  ↔ match by title: {title!r}")
                    update_cache_entry(
                        cache,
                        ttid,
                        last_synced_by="bootstrap",
                        ticktick_modified=task.get("modifiedTime"),
                        notion_modified=(upd or {}).get("last_edited_time"),
                    )
                else:
                    props = ticktick_to_notion_props(task, project_id)
                    _ensure_active_status(props)
                    result = notion.create_page(props)
                    if result and result.get("id"):
                        created_in_notion += 1
                        log.info(f"  ✓ Notion create (bootstrap): {title!r}")
                        update_cache_entry(
                            cache,
                            ttid,
                            last_synced_by="bootstrap",
                            ticktick_modified=task.get("modifiedTime"),
                            notion_modified=result.get("last_edited_time"),
                        )
                    else:
                        errors.append(f"bootstrap_create_notion {ttid}")
            except Exception as e:
                log.exception(f"Erro no bootstrap TT→Notion {ttid}")
                errors.append(f"{ttid}: {e}")

    # Phase 2: Notion → TickTick. Tasks sem TickTick_ID viram tasks novas no TT.
    unmapped = notion.list_unmapped()
    log.info(f"Notion: {len(unmapped)} tasks sem TickTick_ID")
    for page in unmapped:
        page_id = page["id"]
        title = _extract_title_text((page.get("properties") or {}).get(PROP_NAME)) or "(sem título)"
        try:
            payload = notion_to_ticktick_payload(page)
            project = resolve_ticktick_project_for_notion_page(page)
            if project != "inbox":
                payload["projectId"] = project
            result = tt.create_task(payload)
            if not result or not result.get("id"):
                errors.append(f"bootstrap_create_tt page={page_id}")
                continue
            new_ttid = result["id"]
            new_project = result.get("projectId") or project
            upd = notion.update_page(page_id, {
                PROP_TICKTICK_ID: {"rich_text": [{"text": {"content": new_ttid}}]},
                PROP_TICKTICK_PROJECT_ID: {"rich_text": [{"text": {"content": new_project}}]},
            })
            created_in_tt += 1
            log.info(f"  ✓ TT create (bootstrap): {title!r} (id={new_ttid})")
            update_cache_entry(
                cache,
                new_ttid,
                last_synced_by="bootstrap",
                ticktick_modified=result.get("modifiedTime"),
                notion_modified=(upd or {}).get("last_edited_time"),
            )
        except Exception as e:
            log.exception(f"Erro no bootstrap Notion→TT {page_id}")
            errors.append(f"{page_id}: {e}")

    return {
        "matched": matched,
        "created_in_notion": created_in_notion,
        "created_in_tt": created_in_tt,
        "errors": errors,
    }


# ============================================================
# MAIN
# ============================================================


def resolve_notion_token() -> Optional[str]:
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
    parser.add_argument(
        "--direction",
        choices=["tt-to-notion", "notion-to-tt"],
        help="Direção do sync. Mutuamente exclusivo com --bootstrap.",
    )
    parser.add_argument("--bootstrap", action="store_true", help="Match por título e popula TickTick_ID")
    parser.add_argument("--full", action="store_true", help="Ignora last_sync_time")
    parser.add_argument("--dry-run", action="store_true", help="Não escreve em Notion/TickTick")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.bootstrap and args.direction:
        msg = "use --bootstrap OU --direction, não os dois"
        log.error(msg)
        print(json.dumps({"ok": False, "error": msg}))
        return 1
    if not args.bootstrap and not args.direction:
        msg = "passe --direction tt-to-notion | notion-to-tt | --bootstrap"
        log.error(msg)
        print(json.dumps({"ok": False, "error": msg}))
        return 1

    notion_token = resolve_notion_token()
    if not notion_token:
        log.error("NOTION_TOKEN não configurado")
        print(json.dumps({"ok": False, "error": "missing_notion_token"}))
        return 1

    state = load_state()
    cache = load_cache()

    run_start = datetime.now(timezone.utc).isoformat()

    try:
        auth = TickTickAuth(state)
    except RuntimeError as e:
        log.error(str(e))
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    tt = TickTickClient(auth, dry_run=args.dry_run)
    notion = NotionClient(notion_token, dry_run=args.dry_run)

    direction_label = "bootstrap" if args.bootstrap else args.direction
    last_sync = state.get(f"last_sync_{direction_label.replace('-', '_')}")

    try:
        if args.bootstrap:
            stats = run_bootstrap(tt, notion, cache)
        elif args.direction == "tt-to-notion":
            stats = run_tt_to_notion(tt, notion, cache, last_sync, args.full)
        else:
            stats = run_notion_to_tt(tt, notion, cache, last_sync, args.full)
    except Exception as e:
        log.exception("Erro fatal")
        print(json.dumps({"ok": False, "error": str(e)}))
        return 0

    if not args.dry_run:
        state[f"last_sync_{direction_label.replace('-', '_')}"] = run_start
        save_state(state)
        save_cache(cache)

    result = {
        "ok": not bool(stats.get("errors")),
        "mode": direction_label,
        "dry_run": args.dry_run,
        "timestamp": run_start,
        **stats,
    }
    print(json.dumps(result, ensure_ascii=False))
    log.info(f"✅ Fim: {direction_label} {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
