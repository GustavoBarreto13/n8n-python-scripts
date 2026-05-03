#!/usr/bin/env python3
"""
series_sync/main.py
===================
Busca metadados de uma série de TV no TMDB e sincroniza com o Notion:
  - Atualiza a página principal da série (Type=Show)
  - Cria/atualiza páginas de temporadas (Type=Season)
  - Cria/atualiza páginas de episódios (Type=Episode)

Uso:
    python3 main.py --page-id <notion-page-id>   # single show (webhook)
    python3 main.py                               # batch: todas as Returning Series
    python3 main.py --dry-run -v

Saída: JSON em stdout. Logs no stderr.

Variáveis de ambiente:
    NOTION_TOKEN     - obrigatório
    TMDB_TOKEN     - Bearer token TMDB (obrigatório)
    NOTION_DB_SERIES - ID do database Notion (default: hard-coded)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
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

NOTION_DB_SERIES = os.getenv("NOTION_DB_SERIES", "196f090e-a3ca-80d7-adae-ee1e3da03edf")
NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"

NOTION_DELAY = 0.4
TMDB_DELAY = 0.3
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0

# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("series_sync")
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
class ShowInput:
    page_id: str
    title: str
    year: str
    tmdb_id: Optional[int] = None


@dataclass
class SyncStats:
    show_title: str = ""
    tmdb_id: int = 0
    seasons_processed: int = 0
    episodes_created: int = 0
    episodes_updated: int = 0
    episodes_skipped: int = 0
    errors: list = field(default_factory=list)


# ============================================================
# TMDB
# ============================================================


def tmdb_search_show(title: str, year: str, headers: dict) -> Optional[int]:
    log.info(f"TMDB: buscando '{title}' ({year})")
    params: dict = {"query": title}
    if year:
        params["first_air_date_year"] = year
    resp = http_request("GET", f"{TMDB_BASE}/search/tv", headers=headers, params=params)
    time.sleep(TMDB_DELAY)
    if not resp or not resp.get("results"):
        log.warning(f"TMDB: nenhum resultado para '{title}'")
        return None
    result = resp["results"][0]
    log.info(f"TMDB: encontrado ID {result['id']} — {result.get('name', '')}")
    return result["id"]


def tmdb_get_show(tmdb_id: int, headers: dict) -> Optional[dict]:
    log.info(f"TMDB: detalhes do show {tmdb_id}")
    resp = http_request("GET", f"{TMDB_BASE}/tv/{tmdb_id}", headers=headers)
    time.sleep(TMDB_DELAY)
    return resp


def tmdb_get_season(tmdb_id: int, season_number: int, headers: dict) -> Optional[dict]:
    log.debug(f"TMDB: temporada S{season_number:02d} do show {tmdb_id}")
    resp = http_request(
        "GET",
        f"{TMDB_BASE}/tv/{tmdb_id}/season/{season_number}",
        headers=headers,
    )
    time.sleep(TMDB_DELAY)
    return resp


# ============================================================
# NOTION HELPERS
# ============================================================


def _notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _rich_text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value[:2000]}}]}


def _files(name: str, url: str) -> dict:
    return {"files": [{"name": name, "type": "external", "external": {"url": url}}]}


def notion_get_show_page(page_id: str, token: str) -> Optional[ShowInput]:
    resp = http_request("GET", f"{NOTION_BASE}/pages/{page_id}", headers=_notion_headers(token))
    time.sleep(NOTION_DELAY)
    if not resp:
        return None
    props = resp.get("properties", {})

    title_arr = props.get("Name", {}).get("title", [])
    title = title_arr[0]["plain_text"].strip() if title_arr else ""

    year_arr = props.get("Year", {}).get("rich_text", [])
    year = year_arr[0]["plain_text"].strip() if year_arr else ""

    tmdb_raw = props.get("ID", {}).get("number")
    tmdb_id = int(tmdb_raw) if tmdb_raw else None

    return ShowInput(page_id=page_id, title=title, year=year, tmdb_id=tmdb_id)


def notion_query_returning_shows(token: str) -> list[ShowInput]:
    log.info("Buscando todas as Returning Series no Notion")
    headers = _notion_headers(token)
    filter_body = {
        "filter": {
            "and": [
                {"property": "Type", "select": {"equals": "Show"}},
                {"property": "Series Status", "select": {"equals": "Returning Series"}},
                {"property": "ID", "number": {"is_not_empty": True}},
            ]
        }
    }
    shows: list[ShowInput] = []
    cursor = None
    while True:
        body = {**filter_body}
        if cursor:
            body["start_cursor"] = cursor
        resp = http_request(
            "POST",
            f"{NOTION_BASE}/databases/{NOTION_DB_SERIES}/query",
            headers=headers,
            json_body=body,
        )
        time.sleep(NOTION_DELAY)
        if not resp:
            break
        for page in resp.get("results", []):
            props = page.get("properties", {})
            title_arr = props.get("Name", {}).get("title", [])
            title = title_arr[0]["plain_text"].strip() if title_arr else ""
            year_arr = props.get("Year", {}).get("rich_text", [])
            year = year_arr[0]["plain_text"].strip() if year_arr else ""
            tmdb_raw = props.get("ID", {}).get("number")
            tmdb_id = int(tmdb_raw) if tmdb_raw else None
            if title and tmdb_id:
                shows.append(ShowInput(page_id=page["id"], title=title, year=year, tmdb_id=tmdb_id))
        if resp.get("has_more"):
            cursor = resp.get("next_cursor")
        else:
            break
    log.info(f"Encontradas {len(shows)} séries em andamento")
    return shows


def notion_find_existing_seasons(show_page_id: str, token: str) -> dict[int, str]:
    """Returns {season_number: notion_page_id}"""
    headers = _notion_headers(token)
    resp = http_request(
        "POST",
        f"{NOTION_BASE}/databases/{NOTION_DB_SERIES}/query",
        headers=headers,
        json_body={
            "filter": {
                "and": [
                    {"property": "Type", "select": {"equals": "Season"}},
                    {"property": "Parent item", "relation": {"contains": show_page_id}},
                ]
            }
        },
    )
    time.sleep(NOTION_DELAY)
    result: dict[int, str] = {}
    if not resp:
        return result
    for page in resp.get("results", []):
        props = page.get("properties", {})
        season_num = props.get("Season Number", {}).get("number")
        if season_num is not None:
            result[int(season_num)] = page["id"]
    return result


def notion_find_existing_episodes(season_page_id: str, token: str) -> dict[tuple, dict]:
    """Returns {(season_number, episode_number): {"page_id", "air_date", "has_still"}}"""
    headers = _notion_headers(token)
    result: dict[tuple, dict] = {}
    cursor = None
    while True:
        body: dict = {
            "filter": {
                "and": [
                    {"property": "Type", "select": {"equals": "Episode"}},
                    {"property": "Parent item", "relation": {"contains": season_page_id}},
                ]
            }
        }
        if cursor:
            body["start_cursor"] = cursor
        resp = http_request(
            "POST",
            f"{NOTION_BASE}/databases/{NOTION_DB_SERIES}/query",
            headers=headers,
            json_body=body,
        )
        time.sleep(NOTION_DELAY)
        if not resp:
            break
        for page in resp.get("results", []):
            props = page.get("properties", {})
            ep_num = props.get("Episode Number", {}).get("number")
            s_num = props.get("Season Number", {}).get("number")
            if ep_num is None or s_num is None:
                continue
            air_date_data = props.get("Release Date", {}).get("date")
            air_date = air_date_data.get("start") if air_date_data else None
            has_still = bool(props.get("Poster", {}).get("files"))
            result[(int(s_num), int(ep_num))] = {
                "page_id": page["id"],
                "air_date": air_date,
                "has_still": has_still,
            }
        if resp.get("has_more"):
            cursor = resp.get("next_cursor")
        else:
            break
    return result


def notion_update_show(
    page_id: str,
    tmdb_data: dict,
    tmdb_id: int,
    token: str,
    dry_run: bool,
) -> bool:
    props: dict = {}

    overview = (tmdb_data.get("overview") or "")[:2000]
    if overview:
        props["Overview"] = _rich_text(overview)

    ep_count = tmdb_data.get("number_of_episodes")
    if ep_count is not None:
        props["Episode Count"] = {"number": ep_count}

    s_count = tmdb_data.get("number_of_seasons")
    if s_count is not None:
        props["Seasons Count"] = {"number": s_count}

    first_air = tmdb_data.get("first_air_date") or ""
    if first_air:
        props["Release Date"] = {"date": {"start": first_air}}

    status = tmdb_data.get("status") or ""
    if status:
        props["Series Status"] = {"select": {"name": status}}

    props["ID"] = {"number": tmdb_id}
    props["Type"] = {"select": {"name": "Show"}}

    poster_path = tmdb_data.get("poster_path") or ""
    if poster_path:
        props["Poster"] = _files("Poster", f"{TMDB_IMAGE_BASE}{poster_path}")

    networks = tmdb_data.get("networks") or []
    if networks:
        props["Network"] = {"select": {"name": networks[0]["name"]}}

    backdrop_path = tmdb_data.get("backdrop_path") or ""

    if dry_run:
        log.info(f"[dry-run] would update show {page_id}: {list(props.keys())}")
        return True

    body: dict = {"properties": props}
    if backdrop_path:
        body["cover"] = {"type": "external", "external": {"url": f"{TMDB_IMAGE_BASE}{backdrop_path}"}}

    resp = http_request(
        "PATCH",
        f"{NOTION_BASE}/pages/{page_id}",
        headers=_notion_headers(token),
        json_body=body,
    )
    time.sleep(NOTION_DELAY)
    if resp is None:
        log.error(f"Falha ao atualizar show {page_id}")
        return False
    log.info(f"✓ Show atualizado: {tmdb_data.get('name', page_id)}")
    return True


def notion_upsert_season(
    show_page_id: str,
    season_number: int,
    show_title: str,
    tmdb_season: dict,
    tmdb_id: int,
    existing_page_id: Optional[str],
    token: str,
    dry_run: bool,
) -> Optional[str]:
    season_name = f"{show_title} - Season {season_number}"
    overview = (tmdb_season.get("overview") or "")[:2000]
    poster_path = tmdb_season.get("poster_path") or ""
    air_date = tmdb_season.get("air_date") or ""

    props: dict = {
        "Name": {"title": [{"text": {"content": season_name}}]},
        "Type": {"select": {"name": "Season"}},
        "Season Number": {"number": season_number},
        "ID": {"number": tmdb_id},
    }
    if overview:
        props["Overview"] = _rich_text(overview)
    if poster_path:
        props["Poster"] = _files("Poster", f"{TMDB_IMAGE_BASE}{poster_path}")
    if air_date:
        props["Air Date"] = {"date": {"start": air_date}}

    if dry_run:
        action = "update" if existing_page_id else "create"
        log.info(f"[dry-run] would {action} season {season_name}")
        return existing_page_id or "dry-run-season-id"

    if existing_page_id:
        resp = http_request(
            "PATCH",
            f"{NOTION_BASE}/pages/{existing_page_id}",
            headers=_notion_headers(token),
            json_body={"properties": props},
        )
        time.sleep(NOTION_DELAY)
        if resp is None:
            log.error(f"Falha ao atualizar temporada {season_name}")
            return None
        log.debug(f"✓ Temporada atualizada: {season_name}")
        return existing_page_id
    else:
        props["Parent item"] = {"relation": [{"id": show_page_id}]}
        body = {
            "parent": {"database_id": NOTION_DB_SERIES},
            "properties": props,
        }
        resp = http_request(
            "POST",
            f"{NOTION_BASE}/pages",
            headers=_notion_headers(token),
            json_body=body,
        )
        time.sleep(NOTION_DELAY)
        if resp is None:
            log.error(f"Falha ao criar temporada {season_name}")
            return None
        log.info(f"✓ Temporada criada: {season_name}")
        return resp["id"]


def _build_episode_props(
    ep: dict,
    show_title: str,
    season_page_id: str,
    show_page_id: str,
    is_create: bool,
) -> tuple[dict, str]:
    """Returns (props dict, still_url). is_create=True adds Parent item relation."""
    season_number = ep.get("season_number", 0)
    episode_number = ep.get("episode_number", 0)
    ep_name = (ep.get("name") or "").strip() or f"Episode {episode_number}"
    overview = (ep.get("overview") or "")[:2000]
    air_date = ep.get("air_date") or ""
    still_path = ep.get("still_path") or ""
    still_url = f"{TMDB_IMAGE_BASE}{still_path}" if still_path else ""

    name = f"{show_title} - S{season_number:02d}E{episode_number:02d} - {ep_name}"

    props: dict = {
        "Name": {"title": [{"text": {"content": name}}]},
        "Type": {"select": {"name": "Episode"}},
        "Episode Number": {"number": episode_number},
        "Season Number": {"number": season_number},
        "Related Series": {"relation": [{"id": show_page_id}]},
    }
    if is_create:
        props["Parent item"] = {"relation": [{"id": season_page_id}]}
    if overview:
        props["Overview"] = _rich_text(overview)
    if air_date:
        props["Release Date"] = {"date": {"start": air_date}}
    if still_url:
        props["Poster"] = _files("Still", still_url)

    return props, still_url


def notion_create_episode(
    season_page_id: str,
    show_page_id: str,
    ep: dict,
    show_title: str,
    token: str,
    dry_run: bool,
) -> bool:
    props, still_url = _build_episode_props(ep, show_title, season_page_id, show_page_id, is_create=True)

    if dry_run:
        name = props["Name"]["title"][0]["text"]["content"]
        log.debug(f"[dry-run] would create episode: {name}")
        return True

    body: dict = {
        "parent": {"database_id": NOTION_DB_SERIES},
        "properties": props,
    }
    if still_url:
        body["cover"] = {"type": "external", "external": {"url": still_url}}

    resp = http_request(
        "POST",
        f"{NOTION_BASE}/pages",
        headers=_notion_headers(token),
        json_body=body,
    )
    time.sleep(NOTION_DELAY)
    if resp is None:
        log.error(f"Falha ao criar episódio: {props['Name']['title'][0]['text']['content']}")
        return False
    return True


def notion_update_episode(
    page_id: str,
    ep: dict,
    show_title: str,
    season_page_id: str,
    show_page_id: str,
    token: str,
    dry_run: bool,
) -> bool:
    props, still_url = _build_episode_props(ep, show_title, season_page_id, show_page_id, is_create=False)

    if dry_run:
        log.debug(f"[dry-run] would update episode {page_id}")
        return True

    body: dict = {"properties": props}
    if still_url:
        body["cover"] = {"type": "external", "external": {"url": still_url}}

    resp = http_request(
        "PATCH",
        f"{NOTION_BASE}/pages/{page_id}",
        headers=_notion_headers(token),
        json_body=body,
    )
    time.sleep(NOTION_DELAY)
    if resp is None:
        log.error(f"Falha ao atualizar episódio {page_id}")
        return False
    return True


# ============================================================
# SHOW PROCESSOR
# ============================================================


def process_show(
    show: ShowInput,
    token: str,
    tmdb_hdrs: dict,
    dry_run: bool,
    stats: SyncStats,
) -> None:
    log.info(f"Processando: {show.title} ({show.year or 'sem ano'})")

    tmdb_id = show.tmdb_id
    if not tmdb_id:
        tmdb_id = tmdb_search_show(show.title, show.year, tmdb_hdrs)
        if not tmdb_id:
            msg = f"Série não encontrada no TMDB: '{show.title}'"
            log.warning(msg)
            stats.errors.append(msg)
            return

    tmdb_data = tmdb_get_show(tmdb_id, tmdb_hdrs)
    if not tmdb_data:
        msg = f"Falha ao buscar detalhes TMDB: show {tmdb_id}"
        log.error(msg)
        stats.errors.append(msg)
        return

    notion_update_show(show.page_id, tmdb_data, tmdb_id, token, dry_run)

    seasons = [
        s for s in tmdb_data.get("seasons", [])
        if s.get("season_number", 0) != 0 and s.get("name") != "Specials"
    ]
    log.info(f"{show.title}: {len(seasons)} temporada(s)")

    existing_seasons = notion_find_existing_seasons(show.page_id, token)
    today = date.today().isoformat()

    for season in seasons:
        season_number = season.get("season_number", 0)

        tmdb_season = tmdb_get_season(tmdb_id, season_number, tmdb_hdrs)
        if not tmdb_season:
            log.warning(f"{show.title} S{season_number:02d}: não encontrada no TMDB, pulando")
            continue

        existing_season_id = existing_seasons.get(season_number)
        season_page_id = notion_upsert_season(
            show_page_id=show.page_id,
            season_number=season_number,
            show_title=show.title,
            tmdb_season=tmdb_season,
            tmdb_id=tmdb_id,
            existing_page_id=existing_season_id,
            token=token,
            dry_run=dry_run,
        )
        stats.seasons_processed += 1

        if not season_page_id:
            log.error(f"{show.title} S{season_number:02d}: falha na temporada, pulando episódios")
            continue

        existing_episodes = notion_find_existing_episodes(season_page_id, token)

        for ep in tmdb_season.get("episodes", []):
            ep_num = ep.get("episode_number", 0)
            s_num = ep.get("season_number", season_number)
            ep_key = (s_num, ep_num)

            existing = existing_episodes.get(ep_key)
            if existing:
                air_date = existing.get("air_date") or ""
                has_still = existing.get("has_still", False)
                if air_date and has_still and air_date < today:
                    stats.episodes_skipped += 1
                    continue
                ok = notion_update_episode(
                    page_id=existing["page_id"],
                    ep=ep,
                    show_title=show.title,
                    season_page_id=season_page_id,
                    show_page_id=show.page_id,
                    token=token,
                    dry_run=dry_run,
                )
                if ok:
                    stats.episodes_updated += 1
            else:
                ok = notion_create_episode(
                    season_page_id=season_page_id,
                    show_page_id=show.page_id,
                    ep=ep,
                    show_title=show.title,
                    token=token,
                    dry_run=dry_run,
                )
                if ok:
                    stats.episodes_created += 1

    stats.show_title = show.title
    stats.tmdb_id = tmdb_id
    log.info(
        f"✓ {show.title}: "
        f"{stats.seasons_processed} temporada(s), "
        f"criados={stats.episodes_created}, "
        f"atualizados={stats.episodes_updated}, "
        f"pulados={stats.episodes_skipped}"
    )


# ============================================================
# ENTRY POINT
# ============================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="Sincroniza séries de TV com TMDB → Notion")
    parser.add_argument("--page-id", help="ID da página Notion da série (sem: batch mode)")
    parser.add_argument("--dry-run", action="store_true", help="Loga sem escrever no Notion")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    notion_token = resolve_notion_token()
    if not notion_token:
        log.error("NOTION_TOKEN não configurado")
        print(json.dumps({"ok": False, "error": "missing_notion_token"}))
        return 1

    tmdb_api_key = os.getenv("TMDB_TOKEN", "")
    if not tmdb_api_key:
        log.error("TMDB_TOKEN não configurado")
        print(json.dumps({"ok": False, "error": "missing_tmdb_api_key"}))
        return 1

    tmdb_hdrs = {
        "Authorization": f"Bearer {tmdb_api_key}",
        "Accept": "application/json",
    }

    single_mode = bool(args.page_id)
    if single_mode:
        show = notion_get_show_page(args.page_id, notion_token)
        if not show:
            err = get_last_http_error() or "page_not_found"
            print(json.dumps({"ok": False, "error": err, "page_id": args.page_id}))
            return 1
        if not show.title:
            print(json.dumps({"ok": False, "error": "missing_title", "page_id": args.page_id}))
            return 1
        shows = [show]
    else:
        shows = notion_query_returning_shows(notion_token)

    stats_list: list[SyncStats] = []
    for show in shows:
        stats = SyncStats()
        try:
            process_show(show, notion_token, tmdb_hdrs, args.dry_run, stats)
        except Exception as e:
            log.exception(f"Erro ao processar {show.title}: {e}")
            stats.errors.append(f"{show.title}: {e}")
        stats_list.append(stats)

    all_errors = [e for s in stats_list for e in s.errors]
    result: dict = {
        "ok": not bool(all_errors),
        "dry_run": args.dry_run,
        "seasons_processed": sum(s.seasons_processed for s in stats_list),
        "episodes_created": sum(s.episodes_created for s in stats_list),
        "episodes_updated": sum(s.episodes_updated for s in stats_list),
        "episodes_skipped": sum(s.episodes_skipped for s in stats_list),
        "errors": all_errors,
    }
    if single_mode and stats_list:
        result["title"] = stats_list[0].show_title
        result["tmdb_id"] = stats_list[0].tmdb_id

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
