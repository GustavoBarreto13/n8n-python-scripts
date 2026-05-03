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
from dataclasses import dataclass
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


# ============================================================
# GOOGLE BOOKS CLIENT
# ============================================================


class GoogleBooksClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def search_by_isbn(self, isbn: str) -> Optional[BookData]:
        log.info(f"Google Books: buscando por ISBN {isbn}")
        resp = http_request(
            "GET",
            f"{GOOGLE_BOOKS_BASE}/volumes",
            params={"q": f"isbn:{isbn}", "key": self.api_key},
        )
        time.sleep(GOOGLE_BOOKS_DELAY)
        if not resp or resp.get("totalItems", 0) == 0 or not resp.get("items"):
            log.info("Google Books: nenhum resultado por ISBN")
            return None
        return self._parse(resp["items"][0]["volumeInfo"])

    def search_by_title(self, title: str) -> Optional[BookData]:
        log.info(f"Google Books: buscando por título '{title}'")
        resp = http_request(
            "GET",
            f"{GOOGLE_BOOKS_BASE}/volumes",
            params={"q": f'intitle:"{title}"', "key": self.api_key},
        )
        time.sleep(GOOGLE_BOOKS_DELAY)
        if not resp or resp.get("totalItems", 0) == 0 or not resp.get("items"):
            log.info("Google Books: nenhum resultado por título")
            return None
        return self._parse(resp["items"][0]["volumeInfo"])

    def _parse(self, vi: dict) -> BookData:
        identifiers = vi.get("industryIdentifiers") or []
        isbn_10 = next((i["identifier"] for i in identifiers if i["type"] == "ISBN_10"), "")
        isbn_13 = next((i["identifier"] for i in identifiers if i["type"] == "ISBN_13"), "")

        images = vi.get("imageLinks") or {}
        raw_url = (
            images.get("extraLarge")
            or images.get("large")
            or images.get("medium")
            or images.get("thumbnail")
            or images.get("smallThumbnail")
            or ""
        )
        cover_url = raw_url.replace("http://", "https://").replace("&edge=curl", "") if raw_url else ""

        pub_date = vi.get("publishedDate") or ""
        if len(pub_date) == 4:
            pub_date += "-01-01"
        elif len(pub_date) == 7:
            pub_date += "-01"

        authors = ", ".join(vi.get("authors") or []) or "Autor Desconhecido"

        return BookData(
            title=vi.get("title") or "",
            authors=authors,
            description=(vi.get("description") or "")[:2000],
            publish_date=pub_date,
            isbn_10=isbn_10 or isbn_13,
            page_count=vi.get("pageCount") or 0,
            cover_url=cover_url,
            source="google_books",
        )


# ============================================================
# OPEN LIBRARY CLIENT (fallback)
# ============================================================


class OpenLibraryClient:
    def search_by_isbn(self, isbn: str) -> Optional[BookData]:
        log.info(f"Open Library: buscando por ISBN {isbn}")
        resp = http_request(
            "GET",
            f"{OPEN_LIBRARY_BASE}/api/books",
            params={"bibkeys": f"ISBN:{isbn}", "jscmd": "data", "format": "json"},
        )
        time.sleep(OPEN_LIBRARY_DELAY)
        if not resp:
            return None
        data = resp.get(f"ISBN:{isbn}")
        if not data:
            log.info("Open Library: nenhum resultado por ISBN")
            return None
        return self._parse(data)

    def search_by_title(self, title: str) -> Optional[BookData]:
        log.info(f"Open Library: buscando por título '{title}'")
        resp = http_request(
            "GET",
            f"{OPEN_LIBRARY_BASE}/search.json",
            params={"title": title, "limit": 1},
        )
        time.sleep(OPEN_LIBRARY_DELAY)
        if not resp or not resp.get("docs"):
            log.info("Open Library: nenhum resultado por título")
            return None

        doc = resp["docs"][0]

        isbn_list = doc.get("isbn") or []
        isbn_10 = next((i for i in isbn_list if len(i) == 10), "")
        isbn_13 = next((i for i in isbn_list if len(i) == 13), "")

        pub_year = str(doc.get("first_publish_year") or "")
        pub_date = f"{pub_year}-01-01" if pub_year else ""

        authors = ", ".join(doc.get("author_name") or []) or "Autor Desconhecido"
        cover_id = doc.get("cover_i")
        cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else ""

        return BookData(
            title=doc.get("title") or "",
            authors=authors,
            description="",  # search.json doesn't return description
            publish_date=pub_date,
            isbn_10=isbn_10 or isbn_13,
            page_count=doc.get("number_of_pages_median") or 0,
            cover_url=cover_url,
            source="open_library",
        )

    def _parse(self, data: dict) -> BookData:
        identifiers = data.get("identifiers") or {}
        isbn_10_list = identifiers.get("isbn_10") or []
        isbn_13_list = identifiers.get("isbn_13") or []
        isbn = (isbn_10_list[0] if isbn_10_list else "") or (isbn_13_list[0] if isbn_13_list else "")

        pub_date = data.get("publish_date") or ""
        # Normalise "January 1, 2001" / "2001" / "2001-01" to YYYY-MM-DD
        m4 = re.search(r"\b(\d{4})\b", pub_date)
        pub_date_norm = f"{m4.group(1)}-01-01" if m4 else ""

        authors_list = data.get("authors") or []
        authors = ", ".join(a.get("name", "") for a in authors_list) or "Autor Desconhecido"

        covers = data.get("cover") or {}
        cover_url = covers.get("large") or covers.get("medium") or covers.get("small") or ""
        if cover_url:
            cover_url = cover_url.replace("http://", "https://")

        notes = data.get("notes") or ""
        description = notes.get("value", "") if isinstance(notes, dict) else str(notes)

        return BookData(
            title=data.get("title") or "",
            authors=authors,
            description=description[:2000],
            publish_date=pub_date_norm,
            isbn_10=isbn,
            page_count=data.get("number_of_pages") or 0,
            cover_url=cover_url,
            source="open_library",
        )
