#!/usr/bin/env python3
"""
books_sync/main.py
==================
Busca metadados de um livro (Google Books + Open Library) e atualiza uma
página do Notion com os campos: Author, Pages, Description, Publish Date,
ISBN-10, Cover.

Uso:
    python3 main.py --page-id <notion-page-id>
    python3 main.py --page-id <id> --dry-run
    python3 main.py --page-id <id> -v

Saída: JSON em uma linha no stdout. Logs no stderr.

Variáveis de ambiente:
    NOTION_TOKEN         - obrigatório
    GOOGLE_BOOKS_API_KEY - obrigatório para Google Books (fallback: Open Library)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests


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

NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"
GOOGLE_BOOKS_BASE = "https://www.googleapis.com/books/v1"
OPEN_LIBRARY_BASE = "https://openlibrary.org"

NOTION_DELAY = 0.4
GOOGLE_BOOKS_DELAY = 0.3
OPEN_LIBRARY_DELAY = 0.5

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0

# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("books_sync")
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
# AUTH
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
    timeout: int = 30,
) -> Optional[dict]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
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
# BOOK DATA
# ============================================================


@dataclass
class BookData:
    title: str = ""
    authors: str = ""
    description: str = ""
    publish_date: str = ""      # YYYY-MM-DD
    isbn_10: str = ""
    page_count: int = 0
    cover_url: str = ""
    source: str = ""            # "google_books" | "open_library"


# ============================================================
# NOTION CLIENT
# ============================================================


class NotionClient:
    def __init__(self, token: str, dry_run: bool = False):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self.dry_run = dry_run

    def get_page(self, page_id: str) -> Optional[dict]:
        url = f"{NOTION_BASE}/pages/{page_id}"
        resp = http_request("GET", url, headers=self.headers)
        time.sleep(NOTION_DELAY)
        return resp

    def _get_rich_text(self, props: dict, key: str) -> str:
        arr = props.get(key, {}).get("rich_text", [])
        return arr[0]["plain_text"].strip() if arr else ""

    def _get_title(self, props: dict) -> str:
        arr = props.get("Name", {}).get("title", [])
        return arr[0]["plain_text"].strip() if arr else ""

    def extract_search_info(self, page: dict) -> tuple[str, str]:
        """Returns (title, isbn). isbn is ISBN-10 first, then ISBN-13, or ''."""
        props = page.get("properties", {})
        title = self._get_title(props)
        isbn = self._get_rich_text(props, "ISBN-10") or self._get_rich_text(props, "ISBN-13")
        return title, isbn

    def update_page(self, page_id: str, book: BookData) -> tuple[bool, list[str]]:
        """Updates Notion page with book metadata. Returns (success, updated_fields)."""
        props: dict = {}
        updated_fields: list[str] = []

        if book.authors:
            props["Author"] = {"rich_text": [{"text": {"content": book.authors}}]}
            updated_fields.append("Author")

        if book.page_count:
            props["Pages"] = {"number": book.page_count}
            updated_fields.append("Pages")

        if book.description:
            props["Description"] = {"rich_text": [{"text": {"content": book.description[:2000]}}]}
            updated_fields.append("Description")

        if book.publish_date:
            props["Publish Date"] = {"date": {"start": book.publish_date}}
            updated_fields.append("Publish Date")

        if book.isbn_10:
            props["ISBN-10"] = {"rich_text": [{"text": {"content": book.isbn_10}}]}
            updated_fields.append("ISBN-10")

        if book.cover_url:
            props["Cover"] = {
                "files": [{"name": "cover", "type": "external", "external": {"url": book.cover_url}}]
            }
            updated_fields.append("Cover")

        if self.dry_run:
            log.info(f"[dry-run] would update {page_id}: {updated_fields}")
            return True, updated_fields

        if not props:
            log.info("Nenhum campo para atualizar")
            return True, []

        url = f"{NOTION_BASE}/pages/{page_id}"
        resp = http_request("PATCH", url, headers=self.headers, json_body={"properties": props})
        time.sleep(NOTION_DELAY)

        if resp is None:
            err = get_last_http_error() or "unknown"
            log.error(f"Falha ao atualizar página {page_id}: {err}")
            return False, []

        log.info(f"✓ Notion atualizado: {updated_fields}")
        return True, updated_fields
