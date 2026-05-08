#!/usr/bin/env python3
"""
process_invoice.py (nami_finance_agent/process_invoice.py)
==========================================================
Processa fatura de cartão de crédito em PDF via Gemini e cria as transações
faltantes no Notion database 💰 Transações.

Uso:
    python3 process_invoice.py --pdf-base64 <base64>
    python3 process_invoice.py --pdf-base64 <base64> --dry-run -v

Saída: JSON em uma linha no stdout. Logs no stderr.

Variáveis de ambiente:
    GEMINI_API_KEY          - obrigatório
    NOTION_TOKEN            - obrigatório (fallback: OPENAPI_MCP_HEADERS)
    NOTION_DB_TRANSACTIONS  - opcional (default hardcoded)
    GEMINI_MODEL            - opcional (default: gemini-2.0-flash)
"""

import argparse
import base64 as _b64
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

MAX_RETRIES = 5
RETRY_BACKOFF = 2.0
RETRY_BACKOFF_429 = 30.0

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

def _make_extraction_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"Você é um assistente financeiro. Hoje é {today}.\n"
        "Analise esta fatura/extrato de cartão de crédito.\n"
        "Retorne APENAS JSON válido (sem markdown) com este formato exato:\n"
        '{\n'
        '  "account": "nome da conta exatamente como nas opções abaixo",\n'
        '  "fatura_mes": "YYYY-MM",\n'
        '  "period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},\n'
        '  "transactions": [\n'
        '    {"name": "...", "valor": 0.00, "tipo": "Despesa", "data": "YYYY-MM-DD", "categoria": "...", "parcelas": {"total": 12, "atual": 3}}\n'
        '  ]\n'
        '}\n\n'
        "Contas disponíveis (use o nome EXATO):\n"
        + "\n".join(f"- {k}" for k in ACCOUNTS.keys())
        + "\n\nCategorias disponíveis (use o nome EXATO):\n"
        + "\n".join(f"- {k}" for k in CATEGORIES.keys())
        + "\n\nRegras:\n"
        "- tipo: 'Despesa' para compras/gastos, 'Receita' para créditos/estornos/pagamentos\n"
        "- Se a conta não for reconhecida, use 'Generico'\n"
        "- Se a categoria não for reconhecida, use 'Inbox'\n"
        "- fatura_mes: mês pelo qual a fatura é NOMEADA no cabeçalho/título do PDF (ex: 'Fatura de Abril/2026' → '2026-04', 'Fatura Maio 2026' → '2026-05'). ATENÇÃO: NÃO use a data de vencimento — faturas de cartão costumam vencer no mês SEGUINTE ao nome (ex: 'Fatura de Abril' vence em maio; fatura_mes = '2026-04', não '2026-05'). Se o nome não estiver explícito, use o mês de fechamento (data de corte) se disponível; caso contrário, derive do mês predominante das transações.\n"
        "- period.start = data da primeira transação, period.end = data da última\n"
        "- Datas das transações: para cada linha com apenas DD/MM (sem ano), use o ano e mês do fatura_mes para inferir o ano correto\n"
        "- Datas de parcelas: a 'data' de uma linha parcelada é a data DESTA fatura (mês do fatura_mes), não de parcelas passadas/futuras\n"
        "- NUNCA use anos antes de 2020 — se o ano parecer inválido, corrija pelo fatura_mes\n"
        "- PARCELAS: marcadores comuns em fatura de cartão — 'K/N', 'PARCELAMENTO EM FAT K/N', 'PARC K/N', 'Parcela K/N', 'K DE N'. Exemplo real: 'PARCELAMENTO EM FAT 10/12 SAO PAULO' significa que esta fatura cobra a 10ª de 12 parcelas → parcelas={'total':12,'atual':10}. Incluir 'parcelas': {'total': N, 'atual': K} sempre que houver esse padrão.\n"
        "- ATENÇÃO: K é a parcela DESTA fatura (a 'atual'). As parcelas 1..K-1 já foram cobradas em faturas ANTERIORES (datas passadas) e K+1..N serão cobradas em faturas FUTURAS. Python calcula essas datas — você só precisa retornar K, N e a data desta linha (que será atribuída à parcela K).\n"
        "- O 'valor' da linha já é por parcela — manter como está. O 'name' deve ser o texto COMPLETO da linha removendo APENAS o padrão numérico 'K/N' (ex: '10/12') e espaços extras — preserve o restante integralmente. Ex: 'PARCELAMENTO EM FAT 10/12 SAO PAUL' → name='PARCELAMENTO EM FAT SAO PAUL'; 'PARC 03/06 AMAZON' → name='PARC AMAZON'. Python adiciona o sufixo '(K/N)' ao criar cada parcela.\n"
        "- Sem marcador de parcela: NÃO incluir o campo parcelas.\n"
        "- RETORNAR APENAS O JSON. Sem markdown. Sem explicações."
    )

DEDUP_PROMPT_TEMPLATE = (
    "Compare as duas listas abaixo e retorne APENAS um JSON array com as transações "
    "da lista 'extraidas' que ainda NÃO ESTÃO na lista 'existentes' do Notion.\n"
    "Use seu julgamento: considere uma transação como já existente se tiver o mesmo valor, "
    "data aproximada e nome/estabelecimento semelhante.\n"
    "ATENÇÃO PARCELAS: se a transação extraída tiver campo 'parcelas', preserve-o no resultado — NÃO remova. "
    "Para comparar com existentes, use o nome base (sem sufixo '(K/N)').\n"
    "Retorne APENAS o JSON array (pode ser vazio []). Sem markdown. Sem explicações.\n\n"
    "extraidas: {extracted}\n\n"
    "existentes: {existing}"
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

log = logging.getLogger("nami_invoice")
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
                wait = RETRY_BACKOFF_429 * attempt
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
        self.total_usage: dict = {"promptTokenCount": 0, "candidatesTokenCount": 0}

    def _generate(self, parts: list, timeout: int = 60) -> Optional[str]:
        url = f"{self.BASE}/{GEMINI_MODEL}:generateContent"
        body = {"contents": [{"parts": parts}]}
        resp = http_request("POST", url, params={"key": self.api_key}, json_body=body, timeout=timeout)
        time.sleep(GEMINI_DELAY)
        if not resp:
            log.error("Gemini retornou None: %s", get_last_http_error())
            return None
        usage = resp.get("usageMetadata", {})
        self.total_usage["promptTokenCount"] += usage.get("promptTokenCount", 0)
        self.total_usage["candidatesTokenCount"] += usage.get("candidatesTokenCount", 0)
        try:
            raw = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            return raw
        except (KeyError, IndexError) as exc:
            log.error("Falha ao parsear resposta do Gemini: %s", exc)
            return None

    def _upload_pdf(self, pdf_bytes: bytes) -> Optional[str]:
        """Faz upload do PDF via Files API e retorna o fileUri."""
        upload_url = "https://generativelanguage.googleapis.com/upload/v1beta/files"
        headers = {
            "X-Goog-Upload-Protocol": "multipart",
            "X-Goog-Api-Key": self.api_key,
        }
        files = {
            "metadata": ("metadata", '{"file": {"mimeType": "application/pdf"}}', "application/json"),
            "file": ("fatura.pdf", pdf_bytes, "application/pdf"),
        }
        try:
            resp = requests.post(upload_url, headers=headers, files=files, timeout=120)
            resp.raise_for_status()
            file_obj = resp.json()["file"]
            name = file_obj["name"]
            uri = file_obj["uri"]
            log.debug("PDF enviado para Files API: %s (name=%s)", uri, name)
        except Exception as exc:
            log.error("Falha no upload do PDF: %s", exc)
            return None

        status_url = f"https://generativelanguage.googleapis.com/v1beta/{name}"
        for attempt in range(30):
            try:
                s = requests.get(status_url, headers={"X-Goog-Api-Key": self.api_key}, timeout=15)
                s.raise_for_status()
                state = s.json().get("state")
                log.debug("Files API state (attempt %d): %s", attempt + 1, state)
                if state == "ACTIVE":
                    return uri
                if state == "FAILED":
                    log.error("Files API marcou o arquivo como FAILED")
                    return None
            except Exception as exc:
                log.warning("Erro consultando status do arquivo: %s", exc)
            time.sleep(1)
        log.error("Timeout esperando arquivo ficar ACTIVE")
        return None

    def extract_from_pdf(self, pdf_base64: str) -> Optional[dict | str]:
        """Extrai todas as transações do PDF via Gemini (Files API)."""
        pdf_bytes = _b64.b64decode(pdf_base64)
        if b"/Encrypt" in pdf_bytes[:8192]:
            log.error("PDF protegido com senha — remova a senha antes de enviar")
            return "ENCRYPTED"
        file_uri = self._upload_pdf(pdf_bytes)
        if not file_uri:
            return None
        parts = [
            {"fileData": {"mimeType": "application/pdf", "fileUri": file_uri}},
            {"text": _make_extraction_prompt()},
        ]
        raw = self._generate(parts, timeout=300)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            log.error("JSON inválido na extração: %s | raw: %s", exc, raw[:300])
            return None

    def filter_missing(self, extracted: list, existing: list) -> Optional[list]:
        """Retorna apenas as transações extraídas que não estão no Notion."""
        prompt = DEDUP_PROMPT_TEMPLATE.format(
            extracted=json.dumps(extracted, ensure_ascii=False),
            existing=json.dumps(existing, ensure_ascii=False),
        )
        raw = self._generate([{"text": prompt}])
        if not raw:
            return None
        try:
            result = json.loads(raw)
            if not isinstance(result, list):
                log.error("Dedup retornou não-lista: %s", type(result))
                return None
            return result
        except json.JSONDecodeError as exc:
            log.error("JSON inválido no dedup: %s | raw: %s", exc, raw[:300])
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

    def get_transactions_in_period(self, start: str, end: str) -> list:
        """Retorna lista resumida de transações já cadastradas no período."""
        url = f"{self.BASE}/databases/{NOTION_DB_TRANSACTIONS}/query"
        body = {
            "filter": {
                "and": [
                    {"property": "Data", "date": {"on_or_after": start}},
                    {"property": "Data", "date": {"on_or_before": end}},
                ]
            },
            "page_size": 100,
        }
        results = []
        cursor = None
        while True:
            if cursor:
                body["start_cursor"] = cursor
            resp = http_request("POST", url, headers=self.headers, json_body=body)
            time.sleep(NOTION_DELAY)
            if not resp:
                log.error("Falha ao buscar transações existentes: %s", get_last_http_error())
                break
            for page in resp.get("results", []):
                props = page.get("properties", {})
                try:
                    name_parts = props.get("Name", {}).get("title", [])
                    name = name_parts[0]["text"]["content"] if name_parts else ""
                    valor = props.get("Valor", {}).get("number", 0)
                    data_obj = props.get("Data", {}).get("date") or {}
                    data = data_obj.get("start", "")
                    results.append({"name": name, "valor": valor, "data": data})
                except (KeyError, IndexError, TypeError) as exc:
                    log.debug("Erro ao parsear página Notion: %s", exc)
            if resp.get("has_more"):
                cursor = resp.get("next_cursor")
            else:
                break
        log.info("Transações existentes no período: %d", len(results))
        return results

    def find_existing_installment_indices(self, base_name: str, total: int) -> set[int]:
        """Retorna o conjunto de K's já existentes para um base_name+total.

        Procura no DB por títulos que contenham '{base_name} (' e '/{total})'
        e parseia o sufixo (K/N) com regex.
        """
        url = f"{self.BASE}/databases/{NOTION_DB_TRANSACTIONS}/query"
        body = {
            "filter": {
                "and": [
                    {"property": "Name", "title": {"contains": f"{base_name} ("}},
                    {"property": "Name", "title": {"contains": f"/{total})"}},
                ]
            },
            "page_size": 100,
        }
        found: set[int] = set()
        cursor = None
        pattern = re.compile(r"\((\d+)/(\d+)\)\s*$")
        while True:
            if cursor:
                body["start_cursor"] = cursor
            resp = http_request("POST", url, headers=self.headers, json_body=body)
            time.sleep(NOTION_DELAY)
            if not resp:
                log.error("Falha ao buscar parcelas existentes: %s", get_last_http_error())
                break
            for page in resp.get("results", []):
                try:
                    title_parts = page.get("properties", {}).get("Name", {}).get("title", [])
                    name = title_parts[0]["text"]["content"] if title_parts else ""
                except (KeyError, IndexError, TypeError):
                    continue
                m = pattern.search(name)
                if not m:
                    continue
                k, n = int(m.group(1)), int(m.group(2))
                if n != total:
                    continue
                # confere base name (tudo antes do sufixo)
                base_in_name = name[: m.start()].rstrip()
                if base_in_name == base_name:
                    found.add(k)
            if resp.get("has_more"):
                cursor = resp.get("next_cursor")
            else:
                break
        log.debug("Parcelas existentes para '%s' (total=%d): %s", base_name, total, sorted(found))
        return found

    def create_transaction(self, data: dict) -> Optional[str]:
        categoria_id = CATEGORIES.get(data.get("categoria", ""), CATEGORIES["Inbox"])
        conta_id = ACCOUNTS.get(data.get("conta", ""), ACCOUNTS["Generico"])

        if self.dry_run:
            log.info("[DRY] create '%s' R$%.2f %s", data.get("name"), data.get("valor", 0), data.get("data"))
            return "dry-run-page-id"

        body = {
            "parent": {"database_id": NOTION_DB_TRANSACTIONS},
            "properties": {
                "Name": {"title": [{"text": {"content": data["name"]}}]},
                "Valor": {"number": data["valor"]},
                "Tipo": {"select": {"name": data["tipo"]}},
                "Data": {"date": {"start": data["data"]}},
                "Manual/Auto": {"select": {"name": "Automatico"}},
                "Categorias": {"relation": [{"id": categoria_id}]},
                "Contas e Cartões": {"relation": [{"id": conta_id}]},
            },
        }
        resp = http_request("POST", f"{self.BASE}/pages", headers=self.headers, json_body=body)
        time.sleep(NOTION_DELAY)
        if not resp:
            log.error("Falha ao criar '%s': %s", data.get("name"), get_last_http_error())
            return None
        page_id = resp.get("id")
        log.info("Criada: %s | %s R$%.2f", page_id, data.get("name"), data.get("valor", 0))
        return page_id


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Processa fatura PDF e cria transações faltantes no Notion.")
    parser.add_argument("--pdf-base64", help="Conteúdo do PDF em base64")
    parser.add_argument("--file-id", help="file_id do Telegram — script baixa o PDF automaticamente")
    parser.add_argument("--dry-run", action="store_true", help="Loga sem escrever no Notion")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    gemini_key = os.getenv("GEMINI_API_KEY")
    notion_token = resolve_notion_token()

    missing = [k for k, v in [("GEMINI_API_KEY", gemini_key), ("NOTION_TOKEN", notion_token)] if not v]
    if missing:
        err = f"Variáveis de ambiente ausentes: {', '.join(missing)}"
        log.error(err)
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    if args.file_id:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            err = "TELEGRAM_BOT_TOKEN não configurado"
            log.error(err)
            print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
            return 1
        log.info("Baixando PDF do Telegram (file_id=%s)...", args.file_id)
        info = http_request("GET", f"https://api.telegram.org/bot{bot_token}/getFile",
                            params={"file_id": args.file_id})
        if not info or not info.get("ok"):
            err = f"getFile falhou: {info}"
            log.error(err)
            print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
            return 1
        file_path = info["result"]["file_path"]
        dl_resp = requests.get(f"https://api.telegram.org/file/bot{bot_token}/{file_path}", timeout=60)
        dl_resp.raise_for_status()
        pdf_base64 = _b64.b64encode(dl_resp.content).decode()
    elif args.pdf_base64:
        pdf_base64 = args.pdf_base64
    else:
        err = "Forneça --pdf-base64 ou --file-id"
        log.error(err)
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    gemini = GeminiClient(gemini_key)
    notion = NotionClient(notion_token, dry_run=args.dry_run)

    # Etapa 1: extrair transações do PDF
    log.info("Extraindo transações do PDF via Gemini...")
    extracted = gemini.extract_from_pdf(pdf_base64)
    if extracted == "ENCRYPTED":
        print(json.dumps({"ok": False, "error": "PDF protegido com senha. Envie a versão sem senha."}, ensure_ascii=False))
        return 1
    if not extracted:
        err = "Gemini não conseguiu extrair transações do PDF"
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    account = extracted.get("account", "Generico")
    fatura_mes = extracted.get("fatura_mes", "")
    period = extracted.get("period", {})
    transactions = extracted.get("transactions", [])
    log.info(
        "Extraídas: %d | conta: %s | fatura_mes: %s | período: %s → %s",
        len(transactions), account, fatura_mes, period.get("start"), period.get("end"),
    )

    if not transactions:
        result = {"ok": True, "added": 0, "skipped": 0, "fatura_mes": fatura_mes, "period": period, "account": account, "transactions": []}
        print(json.dumps(result, ensure_ascii=False))
        return 0

    # Etapa 2: buscar o que já existe no Notion
    existing = notion.get_transactions_in_period(
        period.get("start", "2000-01-01"),
        period.get("end", "2099-12-31"),
    )

    # Etapa 3: dedup via Gemini
    log.info("Filtrando duplicatas via Gemini (%d extraídas, %d existentes)...", len(transactions), len(existing))
    to_create = gemini.filter_missing(transactions, existing)
    if to_create is None:
        log.warning("Dedup falhou — criando todas as transações extraídas")
        to_create = transactions

    skipped = len(transactions) - len(to_create)
    log.info("Para criar: %d | Puladas: %d", len(to_create), skipped)

    # Etapa 3.5: expandir parcelas (atual + futuras + passadas faltantes)
    expanded: list[dict] = []
    parcelas_expanded = 0
    for tx in to_create:
        parc = tx.get("parcelas")
        if not parc:
            expanded.append(tx)
            continue
        try:
            total = int(parc["total"])
            atual = int(parc["atual"])
        except (KeyError, TypeError, ValueError):
            log.warning("parcelas inválido em '%s': %s", tx.get("name"), parc)
            expanded.append({k: v for k, v in tx.items() if k != "parcelas"})
            continue
        if total <= 1 or atual < 1 or atual > total:
            expanded.append({k: v for k, v in tx.items() if k != "parcelas"})
            continue
        base_name = tx.get("name", "")
        existing_ks = notion.find_existing_installment_indices(base_name, total)
        # criar: parcela atual (sempre) + futuras (sempre) + passadas faltantes
        ks_to_create = sorted(
            {atual}
            | {k for k in range(atual + 1, total + 1)}
            | {k for k in range(1, atual) if k not in existing_ks}
        )
        sub_list = expand_installments(tx, ks=ks_to_create)
        log.info(
            "Parcela '%s' %d/%d → criar Ks=%s (existentes=%s)",
            base_name, atual, total, ks_to_create, sorted(existing_ks),
        )
        expanded.extend(sub_list)
        parcelas_expanded += len(sub_list) - 1  # -1 para descontar a "atual" que já contava
    to_create = expanded

    # Etapa 4: criar no Notion
    created = []
    for i, tx in enumerate(to_create, start=1):
        tx["conta"] = account
        page_id = notion.create_transaction(tx)
        created.append({
            "index": i,
            "notion_page_id": page_id,
            "name": tx.get("name"),
            "valor": tx.get("valor"),
            "data": tx.get("data"),
            "categoria": tx.get("categoria"),
        })

    in_tok = gemini.total_usage.get("promptTokenCount", 0)
    out_tok = gemini.total_usage.get("candidatesTokenCount", 0)
    cost = (in_tok / 1_000_000) * 0.10 + (out_tok / 1_000_000) * 0.40

    result = {
        "ok": True,
        "dry_run": args.dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "added": len(created),
        "skipped": skipped,
        "parcelas_expanded": parcelas_expanded,
        "fatura_mes": fatura_mes,
        "period": period,
        "account": account,
        "tokens_in": in_tok,
        "tokens_out": out_tok,
        "custo_usd": round(cost, 7),
        "transactions": created,
    }
    print(json.dumps(result, ensure_ascii=False))
    log.info("✅ Fatura processada: %d adicionadas, %d puladas", len(created), skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
