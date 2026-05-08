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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests


def _load_dotenv() -> None:
    # Carrega variáveis de ambiente do arquivo .env na raiz do projeto (útil para desenvolvimento local)
    env_path = Path(__file__).parent.parent / ".env"
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                # Ignora linhas vazias e comentários
                if not line or line.startswith("#") or "=" not in line:
                    continue
                # Separa chave e valor na primeira ocorrência de "="
                key, _, val = line.partition("=")
                # setdefault garante que variáveis já definidas no ambiente não sejam sobrescritas
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass  # .env é opcional; em produção as vars vêm do Dokploy


_load_dotenv()

# ============================================================
# CONFIG
# ============================================================

NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"
OMDB_BASE = "http://www.omdbapi.com"
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w1280"
LETTERBOXD_RSS = "https://letterboxd.com/{username}/rss/"

# Delays entre chamadas para respeitar os rate limits das APIs
NOTION_DELAY = 0.4
OMDB_DELAY = 0.3
TMDB_DELAY = 0.3

MAX_RETRIES = 3       # Número máximo de tentativas em caso de falha transitória
RETRY_BACKOFF = 2.0   # Base do backoff exponencial (em segundos)
RSS_LIMIT = 50        # Quantidade máxima de itens lidos do RSS do Letterboxd por execução

# Namespaces XML do feed RSS do Letterboxd — necessários para acessar campos proprietários
LETTERBOXD_NS = {
    "letterboxd": "https://letterboxd.com",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("gustavoboxd")
# Guarda o último erro HTTP para incluir no resultado JSON em caso de falha
_last_http_error: Optional[str] = None


def get_last_http_error() -> Optional[str]:
    # Retorna e limpa o erro HTTP armazenado (consumo único)
    global _last_http_error
    err = _last_http_error
    _last_http_error = None
    return err


def setup_logging(verbose: bool = False) -> None:
    # Logs vão para stderr para não poluir o stdout, que é reservado para o JSON de saída
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
    # Tenta obter o token do Notion diretamente da variável de ambiente
    tok = os.getenv("NOTION_TOKEN")
    if tok:
        return tok
    # Fallback: extrai o Bearer token do header MCP quando rodando via agente n8n
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

            # 429 = Too Many Requests — aguarda com backoff exponencial antes de tentar novamente
            if resp.status_code == 429:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(f"429 em {url} — retry em {wait:.1f}s (tentativa {attempt})")
                time.sleep(wait)
                continue

            # Erros 5xx indicam falha temporária no servidor — vale tentar novamente
            if 500 <= resp.status_code < 600:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(f"{resp.status_code} em {url} — retry em {wait:.1f}s")
                time.sleep(wait)
                continue

            # 404 = recurso não encontrado — retorna None sem logar erro (comportamento esperado em algumas buscas)
            if resp.status_code == 404:
                log.debug(f"404 em {url}")
                return None

            # Outros erros 4xx são problemas do cliente (autenticação, payload inválido) — não adianta tentar novamente
            if 400 <= resp.status_code < 500:
                body_preview = (resp.text or "")[:500]
                _last_http_error = f"{resp.status_code} {method}: {body_preview}"
                log.error(f"{resp.status_code} {method} {url}: {body_preview}")
                return None

            # Sucesso: retorna o JSON da resposta (ou dict vazio se a resposta não tiver corpo)
            return resp.json() if resp.content else {}

        except requests.RequestException as e:
            # Erros de rede (timeout, conexão recusada) — tenta novamente com backoff
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
    # Estrutura intermediária com os dados de um filme vindos do OMDB
    title: str = ""
    year: str = ""
    director: list[str] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)
    genre: list[str] = field(default_factory=list)
    country: list[str] = field(default_factory=list)
    language: list[str] = field(default_factory=list)
    overview: str = ""
    released_date: str = ""  # formato YYYY-MM-DD para compatibilidade com o campo date do Notion
    poster: str = ""


@dataclass
class LetterboxdEntry:
    # Representa um item do feed RSS do Letterboxd (filme assistido ou avaliado)
    title: str
    year: str
    url: str                     # URL canônica do filme no Letterboxd — usada como chave de deduplicação
    rating: Optional[float] = None
    watched_date: str = ""       # formato YYYY-MM-DD
    review: str = ""


@dataclass
class SyncStats:
    # Contadores acumulados durante o sync para compor o JSON de retorno
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


# ============================================================
# OMDB CLIENT
# ============================================================


def omdb_fetch(title: str, year: str, api_key: str) -> Optional[MovieData]:
    # Monta os parâmetros da busca; o ano é opcional mas aumenta a precisão
    params: dict = {"t": title, "apikey": api_key, "type": "movie"}
    if year:
        params["y"] = year

    log.info(f"OMDB: buscando '{title}'" + (f" ({year})" if year else ""))
    data = http_request("GET", OMDB_BASE, params=params)
    time.sleep(OMDB_DELAY)  # Respeita o rate limit do OMDB

    # OMDB retorna Response=False quando o filme não é encontrado em vez de usar código HTTP de erro
    if not data or data.get("Response") == "False":
        log.warning(f"OMDB: não encontrado — {data.get('Error', '') if data else 'sem resposta'}")
        return None

    def split_csv(val: str, limit: int = 25) -> list[str]:
        # OMDB retorna listas de valores como strings separadas por vírgula (ex: "Action, Drama")
        if not val or val == "N/A":
            return []
        return [v.strip() for v in val.split(",") if v.strip()][:limit]

    # Converte a data de lançamento do formato "DD Mon YYYY" (ex: "16 Jul 2010") para YYYY-MM-DD
    released = data.get("Released") or ""
    released_date = ""
    if released and released != "N/A":
        try:
            released_date = datetime.strptime(released, "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Extrai o ano: prefere a data de lançamento completa; usa o campo Year como fallback
    year_out = ""
    if released_date:
        year_out = released_date[:4]
    elif data.get("Year") and data["Year"] != "N/A":
        year_out = data["Year"][:4]

    # OMDB retorna "N/A" quando o poster não está disponível — normaliza para string vazia
    poster = data.get("Poster") or ""
    if poster == "N/A":
        poster = ""

    return MovieData(
        title=data.get("Title") or title,
        year=year_out,
        director=split_csv(data.get("Director", "")),
        actors=split_csv(data.get("Actors", ""), limit=10),  # Limita atores para não estourar multi_select do Notion
        genre=split_csv(data.get("Genre", "")),
        country=split_csv(data.get("Country", "")),
        language=split_csv(data.get("Language", "")),
        overview=(data.get("Plot") or "").replace("N/A", "")[:2000],  # Notion suporta até 2000 chars em rich_text
        released_date=released_date,
        poster=poster,
    )


# ============================================================
# TMDB CLIENT
# ============================================================


def tmdb_fetch_backdrop(title: str, year: str, token: str) -> str:
    """Returns a horizontal backdrop URL from TMDB, or empty string if not found."""
    # TMDB usa autenticação via Bearer token (API v4)
    headers = {"Authorization": f"Bearer {token}"}
    params: dict = {"query": title, "language": "en-US", "page": 1}
    if year:
        params["year"] = year

    log.info(f"TMDB: buscando backdrop para '{title}'" + (f" ({year})" if year else ""))
    data = http_request("GET", f"{TMDB_BASE}/search/movie", headers=headers, params=params)
    time.sleep(TMDB_DELAY)

    if not data:
        return ""

    results = data.get("results") or []
    if not results:
        log.debug(f"TMDB: nenhum resultado para '{title}'")
        return ""

    # Usa o primeiro resultado (mais relevante) e extrai o caminho do backdrop horizontal
    backdrop = results[0].get("backdrop_path") or ""
    if not backdrop:
        log.debug(f"TMDB: sem backdrop para '{title}'")
        return ""

    # Constrói a URL completa com a resolução w1280 (boa qualidade para cover do Notion)
    url = f"{TMDB_IMAGE_BASE}{backdrop}"
    log.debug(f"TMDB: backdrop encontrado → {url}")
    return url


# ============================================================
# LETTERBOXD RSS PARSER
# ============================================================


def _strip_html(text: str) -> str:
    # Remove tags HTML e decodifica entidades HTML (ex: &amp; → &) do texto das reviews
    text = html.unescape(text)
    return re.sub(r"<[^>]+>", "", text).strip()


def fetch_letterboxd_entries(username: str, limit: int = RSS_LIMIT) -> list[LetterboxdEntry]:
    url = LETTERBOXD_RSS.format(username=username)
    log.info(f"Letterboxd: buscando RSS de '{username}'")

    # Faz o download do RSS com retry manual (sem usar http_request pois precisa do conteúdo raw para parsear XML)
    raw_resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # User-Agent necessário para evitar bloqueio de bot pelo Letterboxd
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

    # Parseia o XML do RSS — usa o conteúdo binário para preservar a codificação original
    try:
        root = ET.fromstring(raw_resp.content)
    except ET.ParseError as e:
        log.error(f"Erro ao parsear RSS: {e}")
        return []

    entries: list[LetterboxdEntry] = []
    # Limita o número de itens processados para evitar processamento excessivo em contas com muitos filmes
    items = root.findall(".//item")[:limit]
    log.info(f"Letterboxd: {len(items)} itens no feed")

    for item in items:
        # O campo filmTitle só existe em itens de filmes — listas e atividades de watchlist não o têm
        film_title = item.findtext("letterboxd:filmTitle", namespaces=LETTERBOXD_NS)
        if not film_title:
            continue  # Pula itens que não são filmes (listas, watchlist, etc.)

        link = item.findtext("link") or ""
        year = item.findtext("letterboxd:filmYear", namespaces=LETTERBOXD_NS) or ""

        # rating é None quando o usuário assistiu o filme sem dar nota
        rating_text = item.findtext("letterboxd:memberRating", namespaces=LETTERBOXD_NS)
        rating = float(rating_text) if rating_text else None

        watched_date = item.findtext("letterboxd:watchedDate", namespaces=LETTERBOXD_NS) or ""

        # A description do RSS contém HTML com o poster e a review — extrai apenas o texto
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
        # Headers padrão exigidos pela Notion API em todas as requisições
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self.dry_run = dry_run  # Quando True, nenhuma escrita é feita no Notion

    def get_page(self, page_id: str) -> Optional[dict]:
        # Recupera uma página Notion pelo ID para ler suas propriedades atuais
        resp = http_request("GET", f"{NOTION_BASE}/pages/{page_id}", headers=self.headers)
        time.sleep(NOTION_DELAY)
        return resp

    def update_page(self, page_id: str, props: dict, cover_url: str = "") -> bool:
        if self.dry_run:
            log.info(f"[dry-run] update_page {page_id}: {list(props.keys())}")
            return True
        body: dict = {"properties": props}
        # A cover é atualizada junto com as propriedades para economizar uma chamada de API
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
        # parent.database_id vincula a nova página ao banco de dados correto
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
        # Busca exata pelo campo URL para identificar se o filme já existe no banco (deduplicação)
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
        # Retorna o ID da primeira (e única) página encontrada, ou None se não existir
        return results[0]["id"] if results else None

    @staticmethod
    def _get_title(props: dict) -> str:
        # Extrai o texto do campo título (tipo "title") de um dict de propriedades Notion
        arr = props.get("Name", {}).get("title", [])
        return arr[0]["plain_text"].strip() if arr else ""

    @staticmethod
    def _get_rich_text(props: dict, key: str) -> str:
        # Extrai o texto de um campo rich_text de um dict de propriedades Notion
        arr = props.get(key, {}).get("rich_text", [])
        return arr[0]["plain_text"].strip() if arr else ""


# ============================================================
# PROPERTY BUILDERS
# ============================================================

# Mapeamento de nota numérica (0.5–5.0) para representação em estrelas usada no Notion
_RATING_STARS = {
    0.5: "½",
    1.0: "★",
    1.5: "★½",
    2.0: "★★",
    2.5: "★★½",
    3.0: "★★★",
    3.5: "★★★½",
    4.0: "★★★★",
    4.5: "★★★★½",
    5.0: "★★★★★",
}


def _rating_to_stars(rating: float) -> str:
    # Converte nota numérica para string de estrelas; usa fallback numérico para valores inesperados
    return _RATING_STARS.get(rating, f"{rating:g}")


def _multi_select(values: list[str]) -> dict:
    # Constrói o formato de propriedade multi_select esperado pela Notion API
    return {"multi_select": [{"name": v} for v in values if v]}


def build_movie_props(movie: MovieData) -> tuple[dict, str]:
    """Returns (properties dict, cover_url)."""
    # Título é obrigatório; os demais campos são opcionais e só incluídos quando têm valor
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
        # Poster é salvo como arquivo externo (link) no campo Files do Notion
        props["Poster"] = {"files": [{"name": "Poster", "external": {"url": movie.poster}}]}
    # Retorna o poster como cover_url para ser usado como capa da página
    return props, movie.poster


def build_letterboxd_props(entry: LetterboxdEntry) -> dict:
    # Monta as propriedades vindas do Letterboxd — usadas tanto na criação quanto na atualização da página
    props: dict = {
        "Name": {"title": [{"text": {"content": entry.title}}]},
        "Letterboxd URL": {"url": entry.url},  # URL é a chave de deduplicação — sempre inclusa
    }
    if entry.year:
        props["Year"] = {"rich_text": [{"text": {"content": entry.year}}]}
    if entry.rating is not None:
        # Rating é salvo como select (string de estrelas) e não como número para melhor visualização
        props["Rating"] = {"select": {"name": _rating_to_stars(entry.rating)}}
    if entry.watched_date:
        props["Date"] = {"date": {"start": entry.watched_date}}
    if entry.review:
        props["Review"] = {"rich_text": [{"text": {"content": entry.review}}]}
    return props


# ============================================================
# MODES
# ============================================================


def run_webhook(page_id: str, notion: NotionClient, omdb_key: str, tmdb_token: str) -> dict:
    """
    Modo webhook: dado um page_id do Notion, lê o título do filme,
    busca metadados no OMDB e atualiza a página com os dados encontrados.
    A cover da página é definida como o backdrop do TMDB (melhor qualidade)
    ou, se indisponível, o poster retornado pelo OMDB.
    """
    log.info(f"Modo webhook: enriquecendo página {page_id}")

    # Lê a página para obter o título e o ano já preenchidos pelo usuário
    page = notion.get_page(page_id)
    if not page:
        return {"ok": False, "error": "page_not_found", "page_id": page_id}

    props = page.get("properties", {})
    title = NotionClient._get_title(props)
    year = NotionClient._get_rich_text(props, "Year")

    if not title:
        return {"ok": False, "error": "missing_title", "page_id": page_id}

    # Busca os metadados do filme no OMDB usando o título lido da página
    movie = omdb_fetch(title, year, omdb_key)
    if not movie:
        return {"ok": False, "error": "omdb_not_found", "title": title}

    # Monta as propriedades para atualizar o Notion e decide a imagem de capa
    movie_props, poster_url = build_movie_props(movie)
    # Tenta obter um backdrop horizontal do TMDB (mais adequado como cover de página);
    # usa o poster do OMDB como fallback caso o TMDB não retorne nada
    cover_url = (tmdb_fetch_backdrop(movie.title, movie.year, tmdb_token) if tmdb_token else "") or poster_url
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
    tmdb_token: str,
    yesterday_only: bool = False,
) -> dict:
    """
    Modo sync: busca o feed RSS do Letterboxd do usuário e importa os filmes para o Notion.
    Para cada entrada:
      - Se já existe no Notion (mesmo Letterboxd URL): atualiza rating/review/data.
      - Se não existe: cria a página com dados do Letterboxd e enriquece com OMDB.
    """
    log.info(f"Modo sync: Letterboxd@{username} → Notion DB {db_id[:8]}…")
    entries = fetch_letterboxd_entries(username)
    if not entries:
        return {"ok": False, "error": "no_letterboxd_entries", "username": username}

    # Modo diário (--yesterday): filtra apenas filmes assistidos no dia anterior
    if yesterday_only:
        target = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        entries = [e for e in entries if e.watched_date == target]
        log.info(f"Filtro --yesterday ({target}): {len(entries)} entradas")

    stats = SyncStats()

    for entry in entries:
        try:
            # Verifica se o filme já existe no Notion pelo URL do Letterboxd (chave única)
            existing_id = notion.find_by_letterboxd_url(db_id, entry.url)
            lb_props = build_letterboxd_props(entry)

            if existing_id:
                # Filme já existe — atualiza apenas os dados do Letterboxd (rating pode ter mudado)
                log.info(f"↻ '{entry.title}' ({entry.watched_date}) — atualizando")
                ok = notion.update_page(existing_id, lb_props)
                if ok:
                    stats.updated += 1
                else:
                    stats.errors.append(f"update failed: {entry.title}")
            else:
                # Filme novo — cria a página com dados do Letterboxd primeiro
                log.info(f"+ '{entry.title}' ({entry.watched_date}) — criando")
                page_id = notion.create_page(db_id, lb_props)
                if not page_id:
                    stats.errors.append(f"create failed: {entry.title}")
                    continue

                # Após criar, enriquece com metadados do OMDB (diretor, gênero, poster, etc.)
                if omdb_key:
                    movie = omdb_fetch(entry.title, entry.year, omdb_key)
                    if movie:
                        movie_props, poster_url = build_movie_props(movie)
                        # Remove o Name do OMDB para preservar o título exato do Letterboxd
                        movie_props.pop("Name", None)
                        cover_url = (tmdb_fetch_backdrop(movie.title, movie.year, tmdb_token) if tmdb_token else "") or poster_url
                        notion.update_page(page_id, movie_props, cover_url)
                    else:
                        log.warning(f"OMDB: sem dados para '{entry.title}' — criado sem metadados")

                stats.created += 1

        except Exception as e:
            # Captura qualquer erro inesperado para não interromper o processamento dos demais filmes
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
    parser.add_argument("--yesterday", action="store_true", help="Sync: filtra apenas filmes assistidos ontem")
    parser.add_argument("--dry-run", action="store_true", help="Loga sem escrever no Notion")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    # Valida o token do Notion antes de qualquer operação
    notion_token = resolve_notion_token()
    if not notion_token:
        log.error("NOTION_TOKEN não configurado")
        print(json.dumps({"ok": False, "error": "missing_notion_token"}))
        return 1

    # Lê as variáveis de ambiente necessárias para cada modo
    omdb_key = os.getenv("OMDB_API_KEY", "")
    tmdb_token = os.getenv("TMDB_TOKEN", "")
    db_id = os.getenv("NOTION_DB_GUSTAVOBOXD", "")
    username = os.getenv("LETTERBOXD_USERNAME", "")

    notion = NotionClient(notion_token, dry_run=args.dry_run)

    if args.page_id:
        # Modo webhook: --page-id fornecido — exige OMDB_API_KEY
        if not omdb_key:
            log.error("OMDB_API_KEY não configurado")
            print(json.dumps({"ok": False, "error": "missing_omdb_key"}))
            return 1
        result = run_webhook(args.page_id, notion, omdb_key, tmdb_token)
    else:
        # Modo sync: sem --page-id — exige banco de dados Notion e username do Letterboxd
        if not db_id:
            log.error("NOTION_DB_GUSTAVOBOXD não configurado")
            print(json.dumps({"ok": False, "error": "missing_notion_db_gustavoboxd"}))
            return 1
        if not username:
            log.error("LETTERBOXD_USERNAME não configurado")
            print(json.dumps({"ok": False, "error": "missing_letterboxd_username"}))
            return 1
        result = run_letterboxd_sync(username, db_id, notion, omdb_key, tmdb_token, yesterday_only=args.yesterday)

    # dry_run é sempre incluído no resultado para facilitar o debug no n8n
    result["dry_run"] = args.dry_run
    # Imprime o JSON em uma única linha no stdout — o n8n lê via proc.stdout.trim()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
