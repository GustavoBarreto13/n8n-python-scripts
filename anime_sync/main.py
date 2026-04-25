#!/usr/bin/env python3
"""
main.py (anime_sync/main.py)
============================
Sincroniza animes entre Jikan/AniList/TMDB → Notion.

Substitui ~10 Code nodes + HTTP Requests do workflow MyAnimeBoxd.

Uso:
    # Sync todos os animes "Currently Airing" do Notion
    python3 main.py

    # Sync um anime específico (via webhook do Notion)
    python3 main.py --page-id <notion_page_id>

    # Dry-run (não escreve no Notion, só loga)
    python3 main.py --dry-run

    # Verbose (logs detalhados)
    python3 main.py -v

Saída: JSON estruturado com resumo da execução via stdout (última linha).
Logs intermediários vão pro stderr.

Variáveis de ambiente necessárias:
    NOTION_TOKEN        - Bearer token do Notion (integration)
    NOTION_DB_ANIMES    - ID do database "Animes" (default já hardcoded)
    TMDB_TOKEN          - Bearer token da TMDB v4 API
"""

# Standard library imports
import argparse       # parse CLI arguments (--page-id, --dry-run, -v)
import json           # serialize the final JSON output to stdout
import logging        # structured logs to stderr
import os             # read environment variables
import re             # regex used to extract token from OPENAPI_MCP_HEADERS fallback
import sys            # sys.exit() and sys.stderr for logging stream
import time           # time.sleep() for rate limiting between API calls
from dataclasses import dataclass, field   # lightweight data containers (AnimeInput, Episode, SyncStats)
from datetime import datetime, timezone    # parse/format ISO 8601 dates and UTC timestamps
from pathlib import Path                   # resolve .env file path relative to this script
from typing import Any, Optional           # type hints

import requests  # HTTP client for all external API calls


def _load_dotenv() -> None:
    """Load key=value pairs from /home/node/scripts/.env into os.environ.

    Uses setdefault so real env vars (Dokploy/container) always win.
    No-op if the file doesn't exist.
    """
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

# Notion database ID — read from env so it can be overridden without changing code.
# The default is the "Animes" database in the user's Notion workspace.
NOTION_DB_ANIMES = os.getenv(
    "NOTION_DB_ANIMES", "22ef090e-a3ca-8047-a25d-c116c21ef2d8"
)

# Notion API version header required on every request.
NOTION_VERSION = "2022-06-28"

# MAL IDs that are skipped for episode sync (metadata-only update).
# One Piece (21) has 1100+ episodes and would spam the rate limit.
BLACKLIST_MAL_IDS: set[int] = {21}

# Seconds to sleep after each API call to stay within rate limits.
# We sleep conservatively — slightly under the actual limit — to avoid 429s.
JIKAN_DELAY = 1.2    # Jikan allows ~3 req/s; we do ~0.83 req/s
ANILIST_DELAY = 0.8  # AniList allows 90 req/min (~1.5 req/s)
TMDB_DELAY = 0.3     # TMDB allows 40 req/10s (~4 req/s)
NOTION_DELAY = 0.4   # Notion allows 3 req/s

# Retry settings for http_request().
MAX_RETRIES = 3       # total attempts before giving up
RETRY_BACKOFF = 2.0   # base seconds; doubles each retry (2s → 4s → 8s)


# ============================================================
# LOGGING
# ============================================================

# Module-level logger. All intermediate logs go to stderr so stdout stays
# clean for the single JSON line printed at the end (parsed by the n8n Code node).
log = logging.getLogger("anime_sync")


def setup_logging(verbose: bool = False) -> None:
    """Configure logging level and format. Called once at startup."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        stream=sys.stderr,  # keep stdout reserved for the final JSON output
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


# ============================================================
# HTTP HELPER (com retry + backoff)
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
    """
    Thin wrapper around requests that handles retries and rate limiting.

    Retries on:
      - 429 (rate limited) — waits and retries
      - 5xx (server error) — waits and retries
      - Network exceptions — waits and retries

    Returns the parsed JSON dict on success, or None after all retries fail.
    404 is treated as "not found" and returns None immediately without retrying.
    """
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
                # We hit the API rate limit — back off and try again.
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(f"429 em {url} — retry em {wait:.1f}s (tentativa {attempt})")
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                # Server-side error — likely transient, worth retrying.
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(f"{resp.status_code} em {url} — retry em {wait:.1f}s")
                time.sleep(wait)
                continue

            if resp.status_code == 404:
                # Resource doesn't exist — no point retrying.
                log.debug(f"404 em {url}")
                return None

            resp.raise_for_status()  # raise on any other 4xx
            return resp.json() if resp.content else {}

        except requests.RequestException as e:
            # Network-level error (timeout, connection refused, etc.)
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            log.warning(f"Erro {e} em {url} — retry em {wait:.1f}s")
            time.sleep(wait)

    log.error(f"Falha definitiva em {url} após {MAX_RETRIES} tentativas")
    return None


# ============================================================
# DATACLASSES
# ============================================================


@dataclass
class AnimeInput:
    """Represents a single anime entry fetched from the Notion database."""
    page_id: str           # Notion page UUID — used for PATCH/relation calls
    title: str             # display name (used in episode names)
    mal_id: Optional[str] = None  # MyAnimeList ID — key for all external API lookups

    @property
    def has_mal_id(self) -> bool:
        """Returns False if MAL_ID is missing; the anime is skipped without it."""
        return bool(self.mal_id)


@dataclass
class Episode:
    """Represents a single episode with all data needed to create/update it in Notion."""
    number: int                    # episode number (used as the primary key for matching)
    title: str                     # episode title (from Jikan; AniList only provides "Episódio N")
    aired: Optional[str] = None    # air date in ISO 8601 or YYYY-MM-DD format
    synopsis: Optional[str] = None # episode synopsis (from Jikan only; truncated to 2000 chars)
    status: str = "Lançado"        # "Lançado" = already aired | "Agendado" = scheduled
    thumbnail: Optional[str] = None  # still image URL from TMDB (optional)


@dataclass
class SyncStats:
    """Accumulates counters and errors across the full sync run."""
    animes_processed: int = 0   # animes that completed the full pipeline
    animes_skipped: int = 0     # animes skipped (no MAL_ID, blacklisted, or Jikan error)
    episodes_created: int = 0   # new episode pages created in Notion
    episodes_updated: int = 0   # existing episode pages updated (status changed)
    episodes_skipped: int = 0   # episodes already "Lançado" — no update needed
    errors: list[str] = field(default_factory=list)  # per-anime error messages

    def to_dict(self) -> dict:
        """Serialize to a flat dict for inclusion in the final JSON output."""
        return {
            "animes_processed": self.animes_processed,
            "animes_skipped": self.animes_skipped,
            "episodes_created": self.episodes_created,
            "episodes_updated": self.episodes_updated,
            "episodes_skipped": self.episodes_skipped,
            "errors": self.errors,
        }


# ============================================================
# NOTION API
# ============================================================


class NotionClient:
    """Wraps all Notion REST API calls needed by this script."""

    BASE = "https://api.notion.com/v1"

    def __init__(self, token: str, dry_run: bool = False):
        # Auth headers sent on every request.
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        # When True, write operations are logged but not actually sent.
        self.dry_run = dry_run

    def query_airing_animes(self) -> list[AnimeInput]:
        """
        Query the Notion database for all animes currently airing.

        Filters: Type=Season AND Anime Status=Currently Airing AND MAL_ID is not empty.
        Handles Notion's cursor-based pagination automatically (100 per page).
        """
        url = f"{self.BASE}/databases/{NOTION_DB_ANIMES}/query"
        body = {
            "filter": {
                "and": [
                    {"property": "MAL_ID", "rich_text": {"is_not_empty": True}},
                    {"property": "Type", "select": {"equals": "Season"}},
                    {"property": "Anime Status", "select": {"equals": "Currently Airing"}},
                ]
            },
            "page_size": 100,
        }
        results: list[AnimeInput] = []
        has_more = True
        start_cursor = None

        # Loop through all pages of results until Notion says there are no more.
        while has_more:
            if start_cursor:
                body["start_cursor"] = start_cursor
            resp = http_request("POST", url, headers=self.headers, json_body=body)
            if not resp:
                break
            for page in resp.get("results", []):
                results.append(self._parse_anime_page(page))
            has_more = resp.get("has_more", False)
            start_cursor = resp.get("next_cursor")
            time.sleep(NOTION_DELAY)

        log.info(f"Notion: {len(results)} animes airing encontrados")
        return results

    def get_anime_page(self, page_id: str) -> Optional[AnimeInput]:
        """Fetch a single Notion page by ID. Used in webhook mode (--page-id)."""
        url = f"{self.BASE}/pages/{page_id}"
        resp = http_request("GET", url, headers=self.headers)
        if not resp:
            return None
        return self._parse_anime_page(resp)

    def _parse_anime_page(self, page: dict) -> AnimeInput:
        """Extract title and MAL_ID from a raw Notion page object."""
        props = page.get("properties", {})

        # Notion stores title fields as an array of rich text objects.
        title_arr = props.get("Name", {}).get("title", [])
        title = title_arr[0]["plain_text"] if title_arr else "Desconhecido"

        # MAL_ID is stored as a rich_text field (not a number) to allow leading zeros.
        mal_arr = props.get("MAL_ID", {}).get("rich_text", [])
        mal_id = mal_arr[0]["plain_text"].strip() if mal_arr else None

        return AnimeInput(page_id=page["id"], title=title, mal_id=mal_id)

    def update_parent_page(self, page_id: str, props: dict, cover_url: Optional[str] = None) -> bool:
        """
        PATCH a Season page with updated metadata and/or a banner image.

        props: Notion property payload (title, synopsis, dates, etc.)
        cover_url: if provided, sets the page's cover image (used for AniList banner).
        """
        if self.dry_run:
            log.info(f"[DRY] update_parent_page {page_id}")
            return True
        url = f"{self.BASE}/pages/{page_id}"
        body: dict = {"properties": props}
        if cover_url:
            body["cover"] = {"type": "external", "external": {"url": cover_url}}
        resp = http_request("PATCH", url, headers=self.headers, json_body=body)
        time.sleep(NOTION_DELAY)
        return resp is not None

    def find_existing_episodes(self, parent_page_id: str) -> dict[str, dict]:
        """
        Query all Episode pages that belong to a given Season page.

        Returns a dict keyed by the episode's Name (e.g. "#1 - Naruto - Episode 1")
        so we can quickly check if an episode already exists before creating it.

        Value: {"page_id": str, "status": str | None}
        """
        url = f"{self.BASE}/databases/{NOTION_DB_ANIMES}/query"
        body = {
            "filter": {
                "and": [
                    {"property": "Type", "select": {"equals": "Episode"}},
                    # "Parent Item" is a Notion relation field linking episodes to their season.
                    {"property": "Parent Item", "relation": {"contains": parent_page_id}},
                ]
            },
            "page_size": 100,
        }
        existing: dict[str, dict] = {}
        has_more = True
        start_cursor = None

        while has_more:
            if start_cursor:
                body["start_cursor"] = start_cursor
            resp = http_request("POST", url, headers=self.headers, json_body=body)
            if not resp:
                break
            for page in resp.get("results", []):
                props = page.get("properties", {})
                name_arr = props.get("Name", {}).get("title", [])
                if not name_arr:
                    continue
                name = name_arr[0]["plain_text"].strip()
                status_obj = props.get("Episode Status", {}).get("select")
                status = status_obj["name"] if status_obj else None
                existing[name] = {"page_id": page["id"], "status": status}
            has_more = resp.get("has_more", False)
            start_cursor = resp.get("next_cursor")
            time.sleep(NOTION_DELAY)

        return existing

    def create_episode(self, parent_page_id: str, ep: Episode, notion_name: str) -> bool:
        """
        Create a new Episode page inside the Notion database.

        Sets all episode properties and links it to the parent Season page
        via the "Parent Item" relation field.
        """
        if self.dry_run:
            log.info(f"[DRY] create_episode {notion_name}")
            return True
        url = f"{self.BASE}/pages"
        props = {
            "Name": {"title": [{"text": {"content": notion_name}}]},
            "Episode Number": {"number": ep.number},
            "Episode Status": {"select": {"name": ep.status}},
            "Overview": {"rich_text": [{"text": {"content": (ep.synopsis or "")[:2000]}}]},
            "Type": {"select": {"name": "Episode"}},
            "Parent Item": {"relation": [{"id": parent_page_id}]},
        }
        if ep.aired:
            props["Air Date"] = {"date": {"start": ep.aired, "time_zone": "America/Sao_Paulo"}}
        body: dict = {
            "parent": {"database_id": NOTION_DB_ANIMES},
            "properties": props,
        }
        if ep.thumbnail:
            # TMDB still image set as the page cover (decorative, not a file property).
            body["cover"] = {"type": "external", "external": {"url": ep.thumbnail}}
        resp = http_request("POST", url, headers=self.headers, json_body=body)
        time.sleep(NOTION_DELAY)
        return resp is not None

    def update_episode(self, page_id: str, ep: Episode, notion_name: str) -> bool:
        """
        Update an existing Episode page.

        Only called when the episode exists but is NOT yet "Lançado" —
        i.e., the air date or status changed since the last sync.
        Synopsis is intentionally not updated here to avoid overwriting manual edits.
        """
        if self.dry_run:
            log.info(f"[DRY] update_episode {notion_name}")
            return True
        url = f"{self.BASE}/pages/{page_id}"
        props = {
            "Name": {"title": [{"text": {"content": notion_name}}]},
            "Episode Number": {"number": ep.number},
            "Episode Status": {"select": {"name": ep.status}},
        }
        if ep.aired:
            props["Air Date"] = {"date": {"start": ep.aired, "time_zone": "America/Sao_Paulo"}}
        body: dict = {"properties": props}
        if ep.thumbnail:
            body["cover"] = {"type": "external", "external": {"url": ep.thumbnail}}
        resp = http_request("PATCH", url, headers=self.headers, json_body=body)
        time.sleep(NOTION_DELAY)
        return resp is not None


# ============================================================
# JIKAN API
# ============================================================


def jikan_get_full(mal_id: str) -> Optional[dict]:
    """
    Fetch complete anime metadata from Jikan (unofficial MAL wrapper).

    Returns the "data" object from /anime/{id}/full, which includes
    title, synopsis, studios, aired dates, episode count, and poster image.
    """
    url = f"https://api.jikan.moe/v4/anime/{mal_id}/full"
    resp = http_request("GET", url)
    time.sleep(JIKAN_DELAY)
    return (resp or {}).get("data") if resp else None


def jikan_get_episodes(mal_id: str) -> list[Episode]:
    """
    Fetch all episodes for an anime from Jikan, handling pagination.

    Jikan returns episodes in pages of 100. We loop until has_next_page is False.
    Each episode gets: number, title, air date (YYYY-MM-DD), and synopsis.
    All Jikan episodes are treated as "Lançado" (already aired).
    """
    eps: list[Episode] = []
    page = 1
    while True:
        url = f"https://api.jikan.moe/v4/anime/{mal_id}/episodes?page={page}"
        resp = http_request("GET", url)
        time.sleep(JIKAN_DELAY)
        if not resp:
            break
        data = resp.get("data", [])
        for raw in data:
            aired = raw.get("aired")
            aired = aired[:10] if aired else None  # truncate to YYYY-MM-DD
            eps.append(Episode(
                number=int(raw.get("mal_id", 0) or 0),  # Jikan uses "mal_id" for episode number
                title=raw.get("title") or f"Episódio {raw.get('mal_id')}",
                aired=aired,
                synopsis=(raw.get("synopsis") or "")[:2000] or None,
                status="Lançado",
            ))
        if not resp.get("pagination", {}).get("has_next_page"):
            break
        page += 1
    log.debug(f"Jikan {mal_id}: {len(eps)} eps")
    return eps


# ============================================================
# ANILIST API
# ============================================================


def anilist_query(query: str, variables: dict) -> Optional[dict]:
    """Send a GraphQL request to AniList and return the "data" field."""
    url = "https://graphql.anilist.co"
    resp = http_request("POST", url, json_body={"query": query, "variables": variables})
    time.sleep(ANILIST_DELAY)
    return (resp or {}).get("data") if resp else None


def anilist_get_schedule_and_banner(mal_id: int) -> tuple[list[Episode], Optional[str]]:
    """
    Fetch the airing schedule and banner image for an anime from AniList.

    AniList is used instead of Jikan for the airing schedule because it provides
    future episode timestamps ("Agendado") that Jikan doesn't have yet.

    Returns:
        - list of Episode objects (status = "Agendado" or "Lançado" based on airingAt vs now)
        - banner image URL (used as the Notion page cover for the Season page)
    """
    q = """
    query ($malId: Int) {
      Media(idMal: $malId, type: ANIME) {
        bannerImage
        airingSchedule(perPage: 100) { nodes { episode airingAt } }
      }
    }
    """
    data = anilist_query(q, {"malId": mal_id})
    if not data or not data.get("Media"):
        return [], None

    media = data["Media"]
    banner = media.get("bannerImage")
    eps: list[Episode] = []
    now = time.time()  # current Unix timestamp for comparing with airingAt

    for node in (media.get("airingSchedule", {}) or {}).get("nodes", []) or []:
        airing_at = node.get("airingAt")
        if not airing_at:
            continue
        dt = datetime.fromtimestamp(airing_at, tz=timezone.utc)
        # If the episode hasn't aired yet, mark it as "Agendado" so Notion shows a future date.
        status = "Agendado" if airing_at > now else "Lançado"
        eps.append(Episode(
            number=int(node.get("episode") or 0),
            title=f"Episódio {node.get('episode')}",  # AniList has no episode titles
            aired=dt.isoformat(),
            status=status,
        ))
    log.debug(f"AniList {mal_id}: {len(eps)} eps no schedule, banner={'sim' if banner else 'não'}")
    return eps, banner


# ============================================================
# ARM (MAL → TMDB)
# ============================================================


def arm_get_tmdb(mal_id: str) -> Optional[dict]:
    """
    Translate a MAL ID to a TMDB ID using the ARM bridge API.

    ARM (Anime Relations Map) maps MAL → TMDB without auth.
    Returns {"tmdb_id": int, "type": "tv"|"movie"} or None if not found.
    We only use TMDB for type="tv" (series); movies are skipped.
    """
    url = f"https://arm.haglund.dev/api/v2/ids?source=myanimelist&id={mal_id}"
    resp = http_request("GET", url)
    time.sleep(0.5)
    if not resp or not resp.get("themoviedb"):
        return None
    return {
        "tmdb_id": resp["themoviedb"],
        "type": resp.get("type", "tv"),
    }


# ============================================================
# TMDB API (com SEASON DETECTION CORRETO)
# ============================================================


class TMDBClient:
    """Wraps TMDB v3 API calls for fetching season info and episode thumbnails."""

    BASE = "https://api.themoviedb.org/3"

    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}"}
        # In-memory cache so we don't fetch the same show's seasons multiple times
        # within a single run. Key: tmdb_id, value: list of season dicts.
        self._seasons_cache: dict[int, list[dict]] = {}

    def get_tv_seasons(self, tmdb_id: int) -> list[dict]:
        """
        Fetch all seasons for a TV show from TMDB.

        Filters out season 0 (specials/OVAs) since those don't match anime episode numbers.
        Results are cached per run to avoid redundant API calls.
        """
        if tmdb_id in self._seasons_cache:
            return self._seasons_cache[tmdb_id]
        url = f"{self.BASE}/tv/{tmdb_id}"
        resp = http_request("GET", url, headers=self.headers)
        time.sleep(TMDB_DELAY)
        if not resp:
            return []
        seasons = [
            s for s in resp.get("seasons", [])
            if s.get("season_number", 0) > 0  # season 0 = specials, irrelevant here
        ]
        self._seasons_cache[tmdb_id] = seasons
        return seasons

    def detect_season(self, tmdb_id: int, anime_aired_from: Optional[str]) -> int:
        """
        Find which TMDB season number matches this anime's premiere date.

        Without this, we'd always use season 1 — which is wrong for sequels
        (e.g., Attack on Titan S4 would get S1 thumbnails).

        Strategy: compare the anime's aired_from date against each season's air_date
        in TMDB and pick the closest one.
        """
        if not anime_aired_from:
            return 1
        seasons = self.get_tv_seasons(tmdb_id)
        if not seasons:
            return 1
        try:
            target = datetime.fromisoformat(anime_aired_from.replace("Z", "+00:00"))
        except ValueError:
            return 1

        # Find the season whose start date is closest to the anime's premiere.
        best = None
        best_delta = float("inf")
        for s in seasons:
            s_date = s.get("air_date")
            if not s_date:
                continue
            try:
                sd = datetime.fromisoformat(s_date).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            delta = abs((target - sd).total_seconds())
            if delta < best_delta:
                best_delta = delta
                best = s

        season_num = best["season_number"] if best else 1
        log.debug(f"TMDB {tmdb_id}: aired_from={anime_aired_from} → season {season_num}")
        return season_num

    def get_episode_images(
        self, tmdb_id: int, season_num: int, episode_num: int
    ) -> Optional[str]:
        """
        Fetch the best-quality still image for a specific episode from TMDB.

        Returns a full TMDB image URL at w780 size, or None if no stills exist.
        "Best quality" = highest pixel width among available stills.
        """
        url = f"{self.BASE}/tv/{tmdb_id}/season/{season_num}/episode/{episode_num}/images"
        resp = http_request("GET", url, headers=self.headers)
        time.sleep(TMDB_DELAY)
        if not resp:
            return None
        stills = resp.get("stills", [])
        if not stills:
            return None
        best = max(stills, key=lambda x: x.get("width", 0))
        return f"https://image.tmdb.org/t/p/w780{best['file_path']}"


# ============================================================
# MERGE & NORMALIZE
# ============================================================


def merge_episodes(jikan_eps: list[Episode], anilist_eps: list[Episode]) -> list[Episode]:
    """
    Combine episode lists from Jikan and AniList into a single deduplicated list.

    Strategy:
      1. Start with AniList episodes (they have future "Agendado" entries).
      2. Overwrite with Jikan episodes where available (better titles + synopsis).

    Result: sorted by episode number, with the best data from each source.
    """
    by_num: dict[int, Episode] = {}
    for ep in anilist_eps:
        by_num[ep.number] = ep   # AniList first (provides scheduled episodes)
    for ep in jikan_eps:
        by_num[ep.number] = ep   # Jikan overwrites (better title + synopsis)
    return sorted(by_num.values(), key=lambda e: e.number)


def normalize_jikan_data(data: dict) -> dict:
    """
    Flatten Jikan's nested response into a simple dict ready for Notion.

    Extracts: title variants, synopsis, studio, aired dates, poster URL,
    episode count, season label, and MAL status string.
    """
    aired = data.get("aired", {}) or {}
    aired_from = aired.get("from")
    aired_to = aired.get("to") or aired_from  # use start date as end if series hasn't finished
    studios = data.get("studios") or []
    titles = data.get("titles") or []
    images = (data.get("images") or {}).get("jpg") or {}
    return {
        "synopsis": (data.get("synopsis") or "")[:2000],  # Notion rich_text cap
        "aired_from": aired_from,
        "aired_to": aired_to,
        "episodes_count": data.get("episodes") or 0,
        "season": f"{data.get('season') or 'unknown'} {data.get('year') or ''}".strip(),
        "status": data.get("status") or "Not yet aired",
        "mal_id": str(data.get("mal_id") or ""),
        "studio": studios[0]["name"] if studios else "Unknown",
        "poster_url": images.get("large_image_url") or "",
        "title": titles[0]["title"] if titles else (data.get("title") or "Sem título"),
        "title_english": data.get("title_english") or "",
        "title_japanese": data.get("title_japanese") or "",
    }


def build_notion_props_for_parent(norm: dict) -> dict:
    """
    Convert normalized Jikan data into the Notion property payload format
    for a Season page PATCH request.
    """
    return {
        "Name": {"title": [{"text": {"content": norm["title"]}}]},
        "Overview": {"rich_text": [{"text": {"content": norm["synopsis"]}}]},
        "Episodes Count": {"number": norm["episodes_count"]},
        "Season": {"select": {"name": norm["season"]}},
        "Anime Status": {"select": {"name": norm["status"]}},
        "MAL_ID": {"rich_text": [{"text": {"content": norm["mal_id"]}}]},
        "Studio": {"select": {"name": norm["studio"]}},
        "Title English": {"rich_text": [{"text": {"content": norm["title_english"]}}]},
        "Title Japanese": {"rich_text": [{"text": {"content": norm["title_japanese"]}}]},
        "Type": {"select": {"name": "Season"}},
        "Aired to": {
            "date": {
                "start": norm["aired_from"] or "1970-01-01",
                # Notion date ranges require end=None when start==end (single-day).
                "end": norm["aired_to"] if norm["aired_to"] != norm["aired_from"] else None,
                "time_zone": "America/Sao_Paulo",
            }
        },
        "Poster": {
            "files": [
                {
                    "name": "Poster",
                    "type": "external",
                    "external": {"url": norm["poster_url"]},
                }
            ]
        } if norm["poster_url"] else {"files": []},
    }


# ============================================================
# CORE: processa 1 anime
# ============================================================


def process_anime(
    anime: AnimeInput,
    notion: NotionClient,
    tmdb: Optional[TMDBClient],
    stats: SyncStats,
) -> None:
    """
    Full sync pipeline for a single anime. Steps:

    1. Fetch complete metadata from Jikan (/full endpoint).
    2. Skip if in BLACKLIST_MAL_IDS (only update metadata, no episodes).
    3. Update the Season page in Notion with latest metadata.
    4. Fetch airing schedule + banner from AniList.
    5. Fetch paginated episode list from Jikan.
    6. Merge AniList + Jikan episodes (Jikan wins on conflicts).
    7. Fetch TMDB episode thumbnails (if TMDB token is available).
    8. Load existing episodes from Notion (to diff create vs update).
    9. For each episode: create if new, update if status changed, skip if "Lançado".
    """
    log.info(f"▶ Processando: {anime.title} (MAL={anime.mal_id})")

    # Step 1: Jikan full metadata
    full = jikan_get_full(anime.mal_id)
    if not full:
        stats.errors.append(f"{anime.title}: Jikan full falhou")
        stats.animes_skipped += 1
        return

    mal_id_int = int(full.get("mal_id") or 0)

    # Step 2: Blacklist check — skip episode sync for shows with huge episode counts.
    if mal_id_int in BLACKLIST_MAL_IDS:
        log.warning(f"⚠ {anime.title} (MAL={mal_id_int}) está na blacklist — pulando episódios")
        # Still update parent metadata so titles/status stay current.
        norm = normalize_jikan_data(full)
        notion.update_parent_page(anime.page_id, build_notion_props_for_parent(norm))
        stats.animes_skipped += 1
        return

    # Step 3: Update Season page metadata in Notion.
    norm = normalize_jikan_data(full)
    notion.update_parent_page(anime.page_id, build_notion_props_for_parent(norm))

    # Step 4: AniList — get future episode schedule and banner image.
    anilist_eps, banner = anilist_get_schedule_and_banner(mal_id_int)
    if banner:
        # Set banner as the page cover (separate PATCH call since cover isn't a property).
        notion.update_parent_page(anime.page_id, {}, cover_url=banner)

    # Step 5: Jikan — get all released episodes with titles and synopses.
    jikan_eps = jikan_get_episodes(anime.mal_id)

    # Step 6: Merge both sources into a single deduplicated episode list.
    merged = merge_episodes(jikan_eps, anilist_eps)
    log.info(f"  {len(merged)} episódios (Jikan={len(jikan_eps)} + AniList={len(anilist_eps)})")

    # Step 7: TMDB thumbnails — requires ARM to translate MAL ID → TMDB ID first.
    if tmdb:
        arm = arm_get_tmdb(anime.mal_id)
        if arm and arm["type"] == "tv":
            season_num = tmdb.detect_season(arm["tmdb_id"], norm["aired_from"])
            log.info(f"  TMDB: show={arm['tmdb_id']} season={season_num}")
            for ep in merged:
                thumb = tmdb.get_episode_images(arm["tmdb_id"], season_num, ep.number)
                if thumb:
                    ep.thumbnail = thumb

    # Step 8: Load existing Notion episodes to avoid duplicates.
    existing = notion.find_existing_episodes(anime.page_id)
    log.debug(f"  {len(existing)} episódios já existem no Notion")

    # Step 9: Create new episodes or update changed ones.
    for ep in merged:
        # Name format: "#1 - Attack on Titan - Dual Blades" — used as the unique key.
        notion_name = f"#{ep.number} - {anime.title} - {ep.title}"
        key = notion_name.strip()
        ex = existing.get(key)

        if not ex:
            # Episode doesn't exist yet — create it.
            if notion.create_episode(anime.page_id, ep, notion_name):
                stats.episodes_created += 1
        elif ex["status"] == "Lançado":
            # Episode is already marked as aired — no need to update anything.
            stats.episodes_skipped += 1
        else:
            # Episode exists but status may have changed (e.g., "Agendado" → "Lançado").
            if notion.update_episode(ex["page_id"], ep, notion_name):
                stats.episodes_updated += 1

    stats.animes_processed += 1


# ============================================================
# MAIN
# ============================================================


def main() -> int:
    """
    Entry point. Parses CLI args, initializes API clients, runs the sync loop,
    and prints the final JSON summary to stdout.

    Exit codes:
      0 — success with no errors
      1 — fatal startup error (missing token)
      2 — completed but with per-anime errors
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--page-id", help="Notion page_id específico (webhook mode)")
    parser.add_argument("--dry-run", action="store_true", help="Não escreve no Notion")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    # Resolve Notion token: prefer NOTION_TOKEN, fall back to extracting it
    # from OPENAPI_MCP_HEADERS (a JSON header object set in some Docker environments).
    notion_token = os.getenv("NOTION_TOKEN")
    if not notion_token:
        mcp_headers = os.getenv("OPENAPI_MCP_HEADERS", "")
        m = re.search(r'Bearer\s+(\S+?)["\s]', mcp_headers)
        if m:
            notion_token = m.group(1).rstrip('"')
    if not notion_token:
        log.error("NOTION_TOKEN ou OPENAPI_MCP_HEADERS não configurado")
        print(json.dumps({"ok": False, "error": "missing_notion_token"}))
        return 1

    # TMDB is optional — without it we just skip thumbnails.
    tmdb_token = os.getenv("TMDB_TOKEN")
    tmdb = TMDBClient(tmdb_token) if tmdb_token else None
    if not tmdb:
        log.warning("TMDB_TOKEN não configurado — sem thumbnails")

    notion = NotionClient(notion_token, dry_run=args.dry_run)
    stats = SyncStats()

    # Webhook mode: sync one specific anime. Batch mode: sync all currently airing.
    if args.page_id:
        anime = notion.get_anime_page(args.page_id)
        if anime is None:
            log.error(f"Notion page not found: {args.page_id}")
            stats.errors.append(f"page_not_found: {args.page_id}")
            animes = []
        else:
            animes = [anime]
    else:
        animes = notion.query_airing_animes()

    log.info(f"Vai processar {len(animes)} anime(s)")

    for anime in animes:
        if not anime.has_mal_id:
            log.warning(f"  {anime.title}: sem MAL_ID, pulando")
            stats.animes_skipped += 1
            continue
        try:
            process_anime(anime, notion, tmdb, stats)
        except Exception as e:
            # Catch-all so one broken anime doesn't abort the entire run.
            log.exception(f"Erro em {anime.title}: {e}")
            stats.errors.append(f"{anime.title}: {e}")

    # Print the final JSON to stdout — this is what the n8n Code node reads and parses.
    result = {
        "ok": not bool(stats.errors),
        "dry_run": args.dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **stats.to_dict(),
    }
    print(json.dumps(result, ensure_ascii=False))
    log.info(f"✅ Fim: {stats.to_dict()}")
    return 0  # always 0 — execSync throws on non-zero, n8n reads "ok" field instead


if __name__ == "__main__":
    sys.exit(main())
