#!/usr/bin/env python3
"""
main.py (nami_finance_agent/main.py)
=====================================
Extrai transação financeira de texto via Gemini REST API e cria entrada
no Notion database 💰 Transações.

Uso:
    python3 main.py --text "Gastei 89 no Rappi, Cartão Nu"
    python3 main.py --text "..." --dry-run
    python3 main.py --text "..." -v

Saída: JSON em uma linha no stdout. Logs no stderr.

Variáveis de ambiente:
    GEMINI_API_KEY          - obrigatório
    NOTION_TOKEN            - obrigatório (fallback: OPENAPI_MCP_HEADERS)
    NOTION_DB_TRANSACTIONS  - opcional (default hardcoded)
    GEMINI_MODEL            - opcional (default: gemini-2.0-flash)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
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

NOTION_DB_TRANSACTIONS = os.getenv(
    "NOTION_DB_TRANSACTIONS", "2b4f090e-a3ca-8093-821c-fd0e28a1cdec"
)
NOTION_VERSION = "2022-06-28"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

NOTION_DELAY = 0.4
GEMINI_DELAY = 0.5

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0

CATEGORIES: dict[str, str] = {
    "Alimentacao": "2b4f090ea3ca80e7b1b5d7366da8de6c",
    "Comer Fora": "2b4f090ea3ca803ab491e92c916b8e84",
    "Transporte": "2b4f090ea3ca80919aaaf743b9f45561",
    "Moradia": "2b4f090ea3ca80568c53d382321087fd",
    "Saude": "2b4f090ea3ca8091b215e6e0fea0c332",
    "Lazer": "2b4f090ea3ca8083a9dcc81885c174ca",
    "Educacao": "2b4f090ea3ca80e883e6d4388b1c1aa2",
    "Assinaturas": "2b4f090ea3ca8006a703e1489e4dbfb9",
    "Compras": "2b4f090ea3ca8017924cca22154aa804",
    "Cuidados": "2b4f090ea3ca802f9eb3f51b2b8cd0ec",
    "Contas Consumo": "2b4f090ea3ca8099b3f4c03b0195ac65",
    "Viagens": "2b4f090ea3ca80d78d99c6f43c6348ff",
    "Investimento": "2b4f090ea3ca807496ffe72c91342095",
    "Reserva": "2b4f090ea3ca8021acefceec373cf9c7",
    "Metas": "2b4f090ea3ca80bfa99aeb54dde136a5",
    "Emprestimos": "2b4f090ea3ca8086a889da6c5950e84a",
    "Pagamento Divida": "2b4f090ea3ca80c58b54c4a2f197159c",
    "Salario": "2b4f090ea3ca80f5b465c0e542c04788",
    "Outras Receitas": "2b4f090ea3ca80b6aa17c9cf16477152",
    "Transferencias": "2b4f090ea3ca80059bd7c35f8423a0c1",
    "Inbox": "2b4f090ea3ca8079b356f256881f9316",
}

ACCOUNTS: dict[str, str] = {
    "Cartao Nu": "2c9f090ea3ca80b08ed6d781ef001397",
    "Cartao Itau": "2b4f090ea3ca80eea99dd95597e3c55a",
    "Cartao Porto": "2c9f090ea3ca80da90cef02f2e68df7b",
    "Itau": "2b4f090ea3ca802aa5f4ea7ea81bfb32",
    "Mercado Pago": "2dcf090ea3ca8070bac3f6f276adcd9d",
    "Generico": "2def090ea3ca80909abadd12e5e70a61",
}

SYSTEM_PROMPT = (
    "Voce extrai dados de transacoes financeiras do Gustavo (Brasil, UTC-3) e retorna "
    "APENAS JSON valido (sem markdown). "
    "Campos obrigatorios: name, valor (numero decimal), tipo (\"Despesa\" ou \"Receita\"), "
    "categoria_id, conta_id, data (YYYY-MM-DD), resumo (mensagem amigavel em portugues).\n\n"
    "CATEGORIAS:\n"
    + "\n".join(f"{k}={v}" for k, v in CATEGORIES.items())
    + "\n\nCONTAS:\n"
    + "\n".join(f"{k}={v}" for k, v in ACCOUNTS.items())
    + "\n\nREGRAS:\n"
    "- Sem data informada: usar data de hoje.\n"
    "- Em duvida na categoria: usar Inbox.\n"
    "- Sem conta mencionada: usar Generico.\n"
    "- tipo deve ser exatamente \"Despesa\" ou \"Receita\".\n"
    '- resumo: ex "Despesa de R$ 50,00 registrada em Alimentacao no Cartao Nu"\n'
    "- RETORNAR APENAS O JSON. Sem markdown. Sem explicacoes."
)


# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("nami_finance")
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
# HTTP
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
                method, url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=timeout,
            )
            if resp.status_code == 429:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning("429 — retry em %.1fs", wait)
                time.sleep(wait)
                continue
            if 500 <= resp.status_code < 600:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning("%d — retry em %.1fs", resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                log.debug("404 em %s", url)
                return None
            if 400 <= resp.status_code < 500:
                body_preview = (resp.text or "")[:500]
                _last_http_error = f"{resp.status_code} {method}: {body_preview}"
                log.error("%d %s %s: %s", resp.status_code, method, url, body_preview)
                return None
            return resp.json() if resp.content else {}
        except requests.RequestException as exc:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            log.warning("Erro %s — retry em %.1fs", exc, wait)
            time.sleep(wait)
    log.error("Falha definitiva após %d tentativas", MAX_RETRIES)
    return None


# ============================================================
# TOKEN
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
# GEMINI CLIENT
# ============================================================

class GeminiClient:
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def extract_transaction(self, text: str) -> Optional[dict]:
        url = f"{self.BASE}/{GEMINI_MODEL}:generateContent"
        body = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": text}]}],
        }
        resp = http_request("POST", url, params={"key": self.api_key}, json_body=body)
        time.sleep(GEMINI_DELAY)
        if not resp:
            log.error("Gemini retornou None: %s", get_last_http_error())
            return None
        try:
            raw = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Remove markdown code fences caso o modelo ignore a instrução
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            return json.loads(raw)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            log.error("Falha ao parsear resposta do Gemini: %s", exc)
            return None


# ============================================================
# NOTION CLIENT
# ============================================================

class NotionClient:
    BASE = "https://api.notion.com/v1"

    def __init__(self, token: str, dry_run: bool = False) -> None:
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self.dry_run = dry_run

    def create_transaction(self, data: dict) -> Optional[str]:
        if self.dry_run:
            log.info("[DRY] create_transaction '%s' R$%.2f", data.get("name"), data.get("valor", 0))
            return "dry-run-page-id"
        body = {
            "parent": {"database_id": NOTION_DB_TRANSACTIONS},
            "properties": {
                "Name": {"title": [{"text": {"content": data["name"]}}]},
                "Valor": {"number": data["valor"]},
                "Tipo": {"select": {"name": data["tipo"]}},
                "Data": {"date": {"start": data["data"]}},
                "Manual/Auto": {"select": {"name": "Automatico"}},
                "Categorias": {"relation": [{"id": data["categoria_id"]}]},
                "Contas e Cartões": {"relation": [{"id": data["conta_id"]}]},
            },
        }
        resp = http_request(
            "POST", f"{self.BASE}/pages",
            headers=self.headers, json_body=body,
        )
        time.sleep(NOTION_DELAY)
        if not resp:
            log.error("Falha ao criar transação: %s", get_last_http_error())
            return None
        page_id = resp.get("id")
        log.info("Transação criada: %s", page_id)
        return page_id


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Registra transação financeira no Notion via Gemini.")
    parser.add_argument("--text", required=True, help="Texto descrevendo a transação")
    parser.add_argument("--dry-run", action="store_true", help="Loga sem escrever no Notion")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    log.info("Processando: %s", args.text[:80])

    gemini_key = os.getenv("GEMINI_API_KEY")
    notion_token = resolve_notion_token()

    missing = [k for k, v in [("GEMINI_API_KEY", gemini_key), ("NOTION_TOKEN", notion_token)] if not v]
    if missing:
        err = f"Variáveis de ambiente ausentes: {', '.join(missing)}"
        log.error(err)
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    gemini = GeminiClient(gemini_key)
    notion = NotionClient(notion_token, dry_run=args.dry_run)

    tx = gemini.extract_transaction(args.text)
    if not tx:
        err = "Gemini não conseguiu extrair os dados da transação"
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    log.info("Extração: %s", tx)

    page_id = notion.create_transaction(tx)
    ok = bool(page_id)

    result = {
        "ok": ok,
        "dry_run": args.dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "resumo": tx.get("resumo", ""),
        "name": tx.get("name"),
        "valor": tx.get("valor"),
        "tipo": tx.get("tipo"),
        "data": tx.get("data"),
        "categoria_id": tx.get("categoria_id"),
        "conta_id": tx.get("conta_id"),
        "notion_page_id": page_id,
    }

    print(json.dumps(result, ensure_ascii=False))
    log.info("✅ %s", result.get("resumo") or ("ok" if ok else "erro"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
