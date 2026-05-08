#!/usr/bin/env python3
"""
gustavoboxd/main.py
====================
Enriquece filmes no Notion via OMDB e sincroniza com Letterboxd (RSS).

Modos:
  --page-id <id>  : Webhook — lê página Notion, busca OMDB, atualiza metadados
  (sem args)      : Sync — busca RSS do Letterboxd, cria/atualiza páginas no Notion

Uso:
    python3 main.py --page-id <notion-page-id>
    python3 main.py --page-id <id> --dry-run -v
    python3 main.py -v
    python3 main.py --dry-run

Saída: JSON em uma linha no stdout. Logs no stderr.

Variáveis de ambiente:
    NOTION_TOKEN             - obrigatório
    OMDB_API_KEY             - necessário para enriquecimento via OMDB
    NOTION_DB_GUSTAVOBOXD    - ID do banco de dados Notion (modo sync)
    LETTERBOXD_USERNAME      - username do Letterboxd (modo sync)
"""

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
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
OMDB_BASE = "http://www.omdbapi.com"
LETTERBOXD_RSS = "https://letterboxd.com/{username}/rss/"

NOTION_DELAY = 0.4
OMDB_DELAY = 0.3

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0
RSS_LIMIT = 50

LETTERBOXD_NS = {
    "letterboxd": "https://letterboxd.com",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("gustavoboxd")
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
    global _last_http_error
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
# DATACLASSES
# ============================================================


@dataclass
class MovieData:
    title: str = ""
    year: str = ""
    director: list[str] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)
    genre: list[str] = field(default_factory=list)
    country: list[str] = field(default_factory=list)
    language: list[str] = field(default_factory=list)
    overview: str = ""
    released_date: str = ""  # YYYY-MM-DD
    poster: str = ""


@dataclass
class LetterboxdEntry:
    title: str
    year: str
    url: str
    rating: Optional[float] = None
    watched_date: str = ""  # YYYY-MM-DD
    review: str = ""


@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


# ============================================================
# OMDB CLIENT
# ============================================================


def omdb_fetch(title: str, year: str, api_key: str) -> Optional[MovieData]:
    params: dict = {"t": title, "apikey": api_key, "type": "movie"}
    if year:
        params["y"] = year

    log.info(f"OMDB: buscando '{title}'" + (f" ({year})" if year else ""))
    data = http_request("GET", OMDB_BASE, params=params)
    time.sleep(OMDB_DELAY)

    if not data or data.get("Response") == "False":
        log.warning(f"OMDB: não encontrado — {data.get('Error', '') if data else 'sem resposta'}")
        return None

    def split_csv(val: str, limit: int = 25) -> list[str]:
        if not val or val == "N/A":
            return []
        return [v.strip() for v in val.split(",") if v.strip()][:limit]

    released = data.get("Released") or ""
    released_date = ""
    if released and released != "N/A":
        try:
            released_date = datetime.strptime(released, "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    year_out = ""
    if released_date:
        year_out = released_date[:4]
    elif data.get("Year") and data["Year"] != "N/A":
        year_out = data["Year"][:4]

    poster = data.get("Poster") or ""
    if poster == "N/A":
        poster = ""

    return MovieData(
        title=data.get("Title") or title,
        year=year_out,
        director=split_csv(data.get("Director", "")),
        actors=split_csv(data.get("Actors", ""), limit=10),
        genre=split_csv(data.get("Genre", "")),
        country=split_csv(data.get("Country", "")),
        language=split_csv(data.get("Language", "")),
        overview=(data.get("Plot") or "").replace("N/A", "")[:2000],
        released_date=released_date,
        poster=poster,
    )


# ============================================================
# LETTERBOXD RSS PARSER
# ============================================================


def _strip_html(text: str) -> str:
    text = html.unescape(text)
    return re.sub(r"<[^>]+>", "", text).strip()


def fetch_letterboxd_entries(username: str, limit: int = RSS_LIMIT) -> list[LetterboxdEntry]:
    url = LETTERBOXD_RSS.format(username=username)
    log.info(f"Letterboxd: buscando RSS de '{username}'")

    raw_resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw_resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            raw_resp.raise_for_status()
            break
        except requests.RequestException as e:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            log.warning(f"Erro ao buscar RSS (tentativa {attempt}): {e} — retry em {wait:.1f}s")
            time.sleep(wait)
            raw_resp = None

    if raw_resp is None:
        log.error("Falha definitiva ao buscar RSS do Letterboxd")
        return []

    try:
        root = ET.fromstring(raw_resp.content)
    except ET.ParseError as e:
        log.error(f"Erro ao parsear RSS: {e}")
        return []

    entries: list[LetterboxdEntry] = []
    items = root.findall(".//item")[:limit]
    log.info(f"Letterboxd: {len(items)} itens no feed")

    for item in items:
        film_title = item.findtext("letterboxd:filmTitle", namespaces=LETTERBOXD_NS)
        if not film_title:
            continue  # skip non-film entries (lists, watchlist activity, etc.)

        link = item.findtext("link") or ""
        year = item.findtext("letterboxd:filmYear", namespaces=LETTERBOXD_NS) or ""

        rating_text = item.findtext("letterboxd:memberRating", namespaces=LETTERBOXD_NS)
        rating = float(rating_text) if rating_text else None

        watched_date = item.findtext("letterboxd:watchedDate", namespaces=LETTERBOXD_NS) or ""

        description = item.findtext("description") or ""
        review = _strip_html(description)[:2000]

        entries.append(LetterboxdEntry(
            title=film_title,
            year=year,
            url=link,
            rating=rating,
            watched_date=watched_date,
            review=review,
        ))

    log.info(f"Letterboxd: {len(entries)} entradas de filmes parseadas")
    return entries


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
        resp = http_request("GET", f"{NOTION_BASE}/pages/{page_id}", headers=self.headers)
        time.sleep(NOTION_DELAY)
        return resp

    def update_page(self, page_id: str, props: dict, cover_url: str = "") -> bool:
        if self.dry_run:
            log.info(f"[dry-run] update_page {page_id}: {list(props.keys())}")
            return True
        body: dict = {"properties": props}
        if cover_url:
            body["cover"] = {"type": "external", "external": {"url": cover_url}}
        resp = http_request("PATCH", f"{NOTION_BASE}/pages/{page_id}",
                            headers=self.headers, json_body=body)
        time.sleep(NOTION_DELAY)
        if resp is None:
            log.error(f"Falha ao atualizar {page_id}: {get_last_http_error()}")
            return False
        log.debug(f"✓ updated {page_id}")
        return True

    def create_page(self, db_id: str, props: dict, cover_url: str = "") -> Optional[str]:
        if self.dry_run:
            log.info(f"[dry-run] create_page em {db_id}: {list(props.keys())}")
            return "dry-run-id"
        body: dict = {"parent": {"database_id": db_id}, "properties": props}
        if cover_url:
            body["cover"] = {"type": "external", "external": {"url": cover_url}}
        resp = http_request("POST", f"{NOTION_BASE}/pages", headers=self.headers, json_body=body)
        time.sleep(NOTION_DELAY)
        if resp is None:
            log.error(f"Falha ao criar página em {db_id}: {get_last_http_error()}")
            return None
        return resp.get("id")

    def find_by_letterboxd_url(self, db_id: str, url: str) -> Optional[str]:
        body = {
            "filter": {
                "property": "Letterboxd URL",
                "url": {"equals": url},
            }
        }
        resp = http_request("POST", f"{NOTION_BASE}/databases/{db_id}/query",
                            headers=self.headers, json_body=body)
        time.sleep(NOTION_DELAY)
        if not resp:
            return None
        results = resp.get("results") or []
        return results[0]["id"] if results else None

    @staticmethod
    def _get_title(props: dict) -> str:
        arr = props.get("Name", {}).get("title", [])
        return arr[0]["plain_text"].strip() if arr else ""

    @staticmethod
    def _get_rich_text(props: dict, key: str) -> str:
        arr = props.get(key, {}).get("rich_text", [])
        return arr[0]["plain_text"].strip() if arr else ""


# ============================================================
# PROPERTY BUILDERS
# ============================================================


def _multi_select(values: list[str]) -> dict:
    return {"multi_select": [{"name": v} for v in values if v]}


def build_movie_props(movie: MovieData) -> tuple[dict, str]:
    """Returns (properties dict, cover_url)."""
    props: dict = {
        "Name": {"title": [{"text": {"content": movie.title}}]},
    }
    if movie.year:
        props["Year"] = {"rich_text": [{"text": {"content": movie.year}}]}
    if movie.director:
        props["Director"] = _multi_select(movie.director)
    if movie.actors:
        props["Actors"] = _multi_select(movie.actors)
    if movie.genre:
        props["Genre"] = _multi_select(movie.genre)
    if movie.country:
        props["Country"] = _multi_select(movie.country)
    if movie.language:
        props["Language"] = _multi_select(movie.language)
    if movie.overview:
        props["Overview"] = {"rich_text": [{"text": {"content": movie.overview}}]}
    if movie.released_date:
        props["Released Date"] = {"date": {"start": movie.released_date}}
    if movie.poster:
        props["Poster"] = {"files": [{"name": "Poster", "external": {"url": movie.poster}}]}
    return props, movie.poster


def build_letterboxd_props(entry: LetterboxdEntry) -> dict:
    props: dict = {
        "Name": {"title": [{"text": {"content": entry.title}}]},
        "Letterboxd URL": {"url": entry.url},
    }
    if entry.year:
        props["Year"] = {"rich_text": [{"text": {"content": entry.year}}]}
    if entry.rating is not None:
        props["Rating"] = {"number": entry.rating}
    if entry.watched_date:
        props["Watched Date"] = {"date": {"start": entry.watched_date}}
    if entry.review:
        props["Review"] = {"rich_text": [{"text": {"content": entry.review}}]}
    return props


# ============================================================
# MODES
# ============================================================


def run_webhook(page_id: str, notion: NotionClient, omdb_key: str) -> dict:
    log.info(f"Modo webhook: enriquecendo página {page_id}")
    page = notion.get_page(page_id)
    if not page:
        return {"ok": False, "error": "page_not_found", "page_id": page_id}

    props = page.get("properties", {})
    title = NotionClient._get_title(props)
    year = NotionClient._get_rich_text(props, "Year")

    if not title:
        return {"ok": False, "error": "missing_title", "page_id": page_id}

    movie = omdb_fetch(title, year, omdb_key)
    if not movie:
        return {"ok": False, "error": "omdb_not_found", "title": title}

    movie_props, cover_url = build_movie_props(movie)
    success = notion.update_page(page_id, movie_props, cover_url)
    log.info(f"✓ '{movie.title}' atualizado" if success else f"✗ falha ao atualizar '{movie.title}'")

    return {
        "ok": success,
        "title": movie.title,
        "year": movie.year,
        "director": movie.director,
        "genre": movie.genre,
        "released_date": movie.released_date,
    }


def run_letterboxd_sync(
    username: str,
    db_id: str,
    notion: NotionClient,
    omdb_key: str,
) -> dict:
    log.info(f"Modo sync: Letterboxd@{username} → Notion DB {db_id[:8]}…")
    entries = fetch_letterboxd_entries(username)
    if not entries:
        return {"ok": False, "error": "no_letterboxd_entries", "username": username}

    stats = SyncStats()

    for entry in entries:
        try:
            existing_id = notion.find_by_letterboxd_url(db_id, entry.url)
            lb_props = build_letterboxd_props(entry)

            if existing_id:
                log.info(f"↻ '{entry.title}' ({entry.watched_date}) — atualizando")
                ok = notion.update_page(existing_id, lb_props)
                if ok:
                    stats.updated += 1
                else:
                    stats.errors.append(f"update failed: {entry.title}")
            else:
                log.info(f"+ '{entry.title}' ({entry.watched_date}) — criando")
                page_id = notion.create_page(db_id, lb_props)
                if not page_id:
                    stats.errors.append(f"create failed: {entry.title}")
                    continue

                # Enrich with OMDB metadata when API key is available
                if omdb_key:
                    movie = omdb_fetch(entry.title, entry.year, omdb_key)
                    if movie:
                        movie_props, cover_url = build_movie_props(movie)
                        movie_props.pop("Name", None)  # preserve Letterboxd title
                        notion.update_page(page_id, movie_props, cover_url)
                    else:
                        log.warning(f"OMDB: sem dados para '{entry.title}' — criado sem metadados")

                stats.created += 1

        except Exception as e:
            log.exception(f"Erro ao processar '{entry.title}': {e}")
            stats.errors.append(f"{entry.title}: {e}")

    log.info(
        f"Sync completo — criados: {stats.created}, "
        f"atualizados: {stats.updated}, "
        f"erros: {len(stats.errors)}"
    )
    return {
        "ok": not bool(stats.errors),
        "created": stats.created,
        "updated": stats.updated,
        "skipped": stats.skipped,
        "errors": stats.errors,
    }


# ============================================================
# ENTRY POINT
# ============================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GustavoBoxd — enriquece filmes no Notion via OMDB e sincroniza com Letterboxd"
    )
    parser.add_argument("--page-id", help="ID da página Notion (modo webhook: enriquece um filme)")
    parser.add_argument("--dry-run", action="store_true", help="Loga sem escrever no Notion")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    notion_token = resolve_notion_token()
    if not notion_token:
        log.error("NOTION_TOKEN não configurado")
        print(json.dumps({"ok": False, "error": "missing_notion_token"}))
        return 1

    omdb_key = os.getenv("OMDB_API_KEY", "")
    db_id = os.getenv("NOTION_DB_GUSTAVOBOXD", "")
    username = os.getenv("LETTERBOXD_USERNAME", "")

    notion = NotionClient(notion_token, dry_run=args.dry_run)

    if args.page_id:
        if not omdb_key:
            log.error("OMDB_API_KEY não configurado")
            print(json.dumps({"ok": False, "error": "missing_omdb_key"}))
            return 1
        result = run_webhook(args.page_id, notion, omdb_key)
    else:
        if not db_id:
            log.error("NOTION_DB_GUSTAVOBOXD não configurado")
            print(json.dumps({"ok": False, "error": "missing_notion_db_gustavoboxd"}))
            return 1
        if not username:
            log.error("LETTERBOXD_USERNAME não configurado")
            print(json.dumps({"ok": False, "error": "missing_letterboxd_username"}))
            return 1
        result = run_letterboxd_sync(username, db_id, notion, omdb_key)

    result["dry_run"] = args.dry_run
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
