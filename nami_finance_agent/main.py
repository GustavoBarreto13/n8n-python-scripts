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
import calendar
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
    "Você é a Nami de One Piece, a navegadora e tesoureira obcecada por dinheiro! 🍊💰\n"
    "Você extrai dados de transacoes financeiras do Gustavo (Brasil, UTC-3) e retorna "
    "APENAS JSON valido (sem markdown). "
    "Campos obrigatorios: name, valor (numero decimal), tipo (\"Despesa\" ou \"Receita\"), "
    "categoria_id, conta_id, data (YYYY-MM-DD), resumo (mensagem com a sua personalidade).\n\n"
    "CATEGORIAS:\n"
    + "\n".join(f"{k}={v}" for k, v in CATEGORIES.items())
    + "\n\nCONTAS:\n"
    + "\n".join(f"{k}={v}" for k, v in ACCOUNTS.items())
    + "\n\nREGRAS:\n"
    "- Sem data informada: usar data de hoje.\n"
    "- Em duvida na categoria: usar Inbox.\n"
    "- Sem conta mencionada: usar Generico.\n"
    "- tipo deve ser exatamente \"Despesa\" ou \"Receita\".\n"
    "- PARCELAS: se o usuario mencionar parcelamento (\"parcelado em Nx\", \"N parcelas de X\", \"Nx de X\", \"N vezes de X\"), incluir o campo opcional \"parcelas\": {\"total\": N, \"atual\": 1}. O campo \"valor\" deve ser SEMPRE o valor de UMA parcela (nao o total). Exemplos:\n"
    "  - \"12 parcelas de 10 reais\" -> valor=10, parcelas={\"total\":12,\"atual\":1}\n"
    "  - \"120 reais parcelado em 12x\" -> valor=10, parcelas={\"total\":12,\"atual\":1}\n"
    "  - \"300 em 3x no Cartao Nu\" -> valor=100, parcelas={\"total\":3,\"atual\":1}\n"
    "- Sem parcelamento mencionado: NAO incluir o campo parcelas.\n"
    "- Quando houver parcelamento E o usuario nao disser uma data especifica: \"data\" = primeiro dia do PROXIMO mes (a cobranca comeca na proxima fatura).\n"
    "- resumo: Aja como a Nami! Se for Despesa, fique furiosa com o gasto e reclame (ex: 'O QUÊ?! R$ 50 em Alimentação no Cartão Nu?! Acha que dinheiro dá em árvore, Gustavo?! 😠'). Se for Receita, fique muito feliz e gananciosa (ex: 'Isso!! R$ 100 na conta! Mais dinheiro pro nosso tesouro! 😍💸'). Se for parcelado, comente o parcelamento (ex: '12x de R$10?! Bota no caderno, Gustavo! 😤').\n"
    "- RETORNAR APENAS O JSON. Sem markdown. Sem explicacoes."
)


def expand_installments(tx: dict, ks: Optional[list[int]] = None) -> list[dict]:
    """Expande uma transação parcelada em N transações (uma por mês).

    Se `tx["parcelas"]` for ausente/None, retorna [tx] sem alterações.
    Se `ks` for fornecida, gera apenas as parcelas listadas; senão gera de
    `atual` até `total` (inclusive).
    """
    parc = tx.get("parcelas")
    if not parc:
        return [tx]
    try:
        total = int(parc["total"])
        atual = int(parc["atual"])
    except (KeyError, TypeError, ValueError):
        log.warning("Campo parcelas inválido: %s", parc)
        return [{k: v for k, v in tx.items() if k != "parcelas"}]
    if total <= 1 or atual < 1 or atual > total:
        log.warning("Parcelas inconsistentes (total=%s atual=%s) — sem expansão", total, atual)
        return [{k: v for k, v in tx.items() if k != "parcelas"}]
    try:
        base_date = datetime.strptime(tx["data"], "%Y-%m-%d")
    except (KeyError, ValueError) as exc:
        log.warning("Data inválida em parcelamento: %s", exc)
        return [{k: v for k, v in tx.items() if k != "parcelas"}]
    base_name = tx.get("name", "")
    if ks is None:
        ks = list(range(atual, total + 1))
    out = []
    for k in sorted(ks):
        offset = k - atual
        m_total = base_date.month - 1 + offset
        y_off, m_idx = divmod(m_total, 12)
        new_year = base_date.year + y_off
        new_month = m_idx + 1
        last_day = calendar.monthrange(new_year, new_month)[1]
        new_day = min(base_date.day, last_day)
        new_date = f"{new_year:04d}-{new_month:02d}-{new_day:02d}"
        sub = {kk: vv for kk, vv in tx.items() if kk != "parcelas"}
        sub["name"] = f"{base_name} ({k}/{total})"
        sub["data"] = new_date
        out.append(sub)
    return out


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

    def _call(self, system_prompt: str, text: str) -> Optional[dict]:
        url = f"{self.BASE}/{GEMINI_MODEL}:generateContent"
        body = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": text}]}],
        }
        resp = http_request("POST", url, params={"key": self.api_key}, json_body=body)
        time.sleep(GEMINI_DELAY)
        if not resp:
            log.error("Gemini retornou None: %s", get_last_http_error())
            return None
        try:
            raw = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            return json.loads(raw)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            log.error("Falha ao parsear resposta do Gemini: %s", exc)
            return None

    def extract_transaction(self, text: str) -> Optional[dict]:
        return self._call(SYSTEM_PROMPT, text)

    def extract_update(self, text: str) -> Optional[dict]:
        update_prompt = (
            "Extraia os campos a serem corrigidos de uma transação financeira.\n"
            "Retorne APENAS JSON com os campos mencionados na correção:\n"
            "- name: string\n"
            "- valor: número decimal\n"
            "- tipo: \"Despesa\" ou \"Receita\"\n"
            "- data: YYYY-MM-DD\n"
            "- categoria_id: usar IDs abaixo\n"
            "- conta_id: usar IDs abaixo\n\n"
            "CATEGORIAS:\n"
            + "\n".join(f"{k}={v}" for k, v in CATEGORIES.items())
            + "\n\nCONTAS:\n"
            + "\n".join(f"{k}={v}" for k, v in ACCOUNTS.items())
            + "\n\nRetorne APENAS o JSON. Sem markdown. Sem explicações."
        )
        return self._call(update_prompt, text)


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

    def delete_transaction(self, page_id: str) -> bool:
        if self.dry_run:
            log.info("[DRY] delete_transaction %s", page_id)
            return True
        resp = http_request(
            "PATCH", f"{self.BASE}/pages/{page_id}",
            headers=self.headers, json_body={"archived": True},
        )
        time.sleep(NOTION_DELAY)
        if not resp:
            log.error("Falha ao arquivar página: %s", get_last_http_error())
            return False
        log.info("Página arquivada: %s", page_id)
        return True

    def update_transaction(self, page_id: str, changes: dict) -> bool:
        if self.dry_run:
            log.info("[DRY] update_transaction %s: %s", page_id, changes)
            return True
        properties = {}
        if "name" in changes:
            properties["Name"] = {"title": [{"text": {"content": changes["name"]}}]}
        if "valor" in changes:
            properties["Valor"] = {"number": changes["valor"]}
        if "tipo" in changes:
            properties["Tipo"] = {"select": {"name": changes["tipo"]}}
        if "data" in changes:
            properties["Data"] = {"date": {"start": changes["data"]}}
        if "categoria_id" in changes:
            properties["Categorias"] = {"relation": [{"id": changes["categoria_id"]}]}
        if "conta_id" in changes:
            properties["Contas e Cartões"] = {"relation": [{"id": changes["conta_id"]}]}
        if not properties:
            log.warning("Nenhuma propriedade reconhecida para atualizar")
            return False
        resp = http_request(
            "PATCH", f"{self.BASE}/pages/{page_id}",
            headers=self.headers, json_body={"properties": properties},
        )
        time.sleep(NOTION_DELAY)
        if not resp:
            log.error("Falha ao atualizar página: %s", get_last_http_error())
            return False
        log.info("Página atualizada: %s | campos: %s", page_id, list(properties.keys()))
        return True

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
    parser = argparse.ArgumentParser(description="Registra/edita transação financeira no Notion via Gemini.")
    parser.add_argument("--text", default=None, help="Texto descrevendo a transação ou a correção")
    parser.add_argument("--delete-page-id", default=None, help="Arquiva (soft-delete) uma transação existente")
    parser.add_argument("--update-page-id", default=None, help="Atualiza campos de uma transação existente (requer --text)")
    parser.add_argument("--dry-run", action="store_true", help="Loga sem escrever no Notion")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.update_page_id and not args.text:
        parser.error("--update-page-id requer --text com o texto de correção")
    if not args.text and not args.delete_page_id and not args.update_page_id:
        parser.error("Informe --text, --delete-page-id ou --update-page-id")

    notion_token = resolve_notion_token()
    missing_vars = [k for k, v in [("NOTION_TOKEN", notion_token)] if not v]

    if args.delete_page_id:
        if missing_vars:
            err = f"Variáveis de ambiente ausentes: {', '.join(missing_vars)}"
            log.error(err)
            print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
            return 1
        notion = NotionClient(notion_token, dry_run=args.dry_run)
        ok = notion.delete_transaction(args.delete_page_id)
        result = {"ok": ok, "page_id": args.delete_page_id, "action": "deleted"}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if ok else 1

    if args.update_page_id:
        gemini_key = os.getenv("GEMINI_API_KEY")
        missing_vars += [k for k, v in [("GEMINI_API_KEY", gemini_key)] if not v]
        if missing_vars:
            err = f"Variáveis de ambiente ausentes: {', '.join(missing_vars)}"
            log.error(err)
            print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
            return 1
        gemini = GeminiClient(gemini_key)
        notion = NotionClient(notion_token, dry_run=args.dry_run)
        changes = gemini.extract_update(args.text)
        if not changes:
            err = "Gemini não conseguiu extrair os campos de correção"
            print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
            return 1
        ok = notion.update_transaction(args.update_page_id, changes)
        result = {"ok": ok, "page_id": args.update_page_id, "action": "updated", "changes": changes}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if ok else 1

    # Modo padrão: criar nova transação
    gemini_key = os.getenv("GEMINI_API_KEY")
    missing_vars += [k for k, v in [("GEMINI_API_KEY", gemini_key)] if not v]
    if missing_vars:
        err = f"Variáveis de ambiente ausentes: {', '.join(missing_vars)}"
        log.error(err)
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    log.info("Processando: %s", args.text[:80])
    gemini = GeminiClient(gemini_key)
    notion = NotionClient(notion_token, dry_run=args.dry_run)

    tx = gemini.extract_transaction(args.text)
    if not tx:
        err = "Gemini não conseguiu extrair os dados da transação"
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    log.info("Extração: %s", tx)

    tx_list = expand_installments(tx)
    page_ids: list[str] = []
    for sub in tx_list:
        pid = notion.create_transaction(sub)
        if pid:
            page_ids.append(pid)
    ok = len(page_ids) == len(tx_list)
    first = tx_list[0]

    result = {
        "ok": ok,
        "dry_run": args.dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "resumo": tx.get("resumo", ""),
        "name": first.get("name"),
        "valor": first.get("valor"),
        "tipo": first.get("tipo"),
        "data": first.get("data"),
        "categoria_id": first.get("categoria_id"),
        "conta_id": first.get("conta_id"),
        "notion_page_id": page_ids[0] if page_ids else None,
        "notion_page_ids": page_ids,
        "parcelas_total": len(tx_list),
    }

    print(json.dumps(result))
    log.info("✅ %s", result.get("resumo") or ("ok" if ok else "erro"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
