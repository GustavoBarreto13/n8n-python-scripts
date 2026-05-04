#!/usr/bin/env python3
"""
main.py (lucy_email_agent/main.py)
=====================================
Processa e-mails via Gemini REST API (gemini-2.0-flash) usando a persona da Lucy,
formata um digest em HTML para envio ao Telegram e retorna os dados estruturados.

Uso:
    python3 main.py --emails-json '[{"emailId":"123","subject":"..."}]'
    python3 main.py --emails-json "..." -v

Saída: JSON em uma linha no stdout. Logs no stderr.

Variáveis de ambiente:
    GEMINI_API_KEY          - obrigatório
    GEMINI_MODEL            - opcional (default: gemini-2.0-flash)
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
import imaplib
import email
from email.header import decode_header

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

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_DELAY = 0.5
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0

CATEGORY_EMOJIS = {
    "Promotions": "🏷️",
    "Work": "💼",
    "Finance": "💰",
    "Shopping": "🛍️",
    "Travel": "✈️",
    "Newsletter": "📰",
    "Social": "👥",
    "Personal": "👤",
    "Other": "🗂️"
}

PRIORITY_COLORS = {
    "high": "🔴",
    "medium": "🟡",
    "low": "🟢"
}

SYSTEM_PROMPT = (
    "Você é Lucy — uma netrunner fria e eficiente de Night City. "
    "Você vasculha a rede toda manhã, filtra o ruído e entrega só o que importa — sem drama, sem enrolação.\n"
    "Suas respostas de overview devem ser secas, diretas, irônicas e lacônicas.\n"
    "Seus resumos de e-mails (summary) devem ser super curtos (1 linha máxima, sem floreio).\n"
    "Você deve ler a lista de e-mails fornecida no prompt (JSON) e categorizar, priorizar e resumir.\n"
    "Preserve sempre o 'emailId' original.\n"
    "Se houver 'gmailCategory', use como sinal forte, sobrescrevendo a categoria apenas se sender/subject contradizerem claramente.\n"
)

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "overview": {
            "type": "STRING",
            "description": "Frase seca sobre o estado da inbox hoje."
        },
        "emails": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "emailId": {"type": "STRING", "description": "ID original do Gmail"},
                    "category": {
                        "type": "STRING",
                        "enum": ["Work", "Finance", "Shopping", "Travel", "Newsletter", "Social", "Promotions", "Personal", "Other"]
                    },
                    "priority": {
                        "type": "STRING",
                        "enum": ["high", "medium", "low"]
                    },
                    "summary": {"type": "STRING", "description": "Resumo muito curto de 1 frase"},
                    "action": {
                        "type": "STRING",
                        "enum": ["arquivar", "responder", "ler", "agir", "ignorar"]
                    }
                },
                "required": ["emailId", "category", "priority", "summary", "action"]
            }
        }
    },
    "required": ["overview", "emails"]
}

# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("lucy_agent")
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
    timeout: int = 45,
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
# GEMINI CLIENT
# ============================================================

class GeminiClient:
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def process_emails(self, emails_data: list) -> Optional[dict]:
        url = f"{self.BASE}/{GEMINI_MODEL}:generateContent"
        
        prompt = f"Categorize e resuma a seguinte lista de e-mails:\n\n{json.dumps(emails_data, ensure_ascii=False, indent=2)}"
        
        body = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "response_mime_type": "application/json",
                "response_schema": RESPONSE_SCHEMA
            }
        }
        
        log.info("Chamando Gemini API (%s) para %d emails...", GEMINI_MODEL, len(emails_data))
        resp = http_request("POST", url, params={"key": self.api_key}, json_body=body)
        time.sleep(GEMINI_DELAY)
        
        if not resp:
            log.error("Gemini retornou None: %s", get_last_http_error())
            return None
            
        try:
            raw = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Remove markdown code fences se existirem
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            return json.loads(raw)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            log.error("Falha ao parsear resposta do Gemini: %s", exc)
            return None

# ============================================================
# FORMATADOR HTML
# ============================================================

def build_telegram_digest(overview: str, emails: list) -> str:
    """Constrói a string HTML para o Telegram baseada no padrão estabelecido."""
    
    # Agrupa e-mails por categoria
    grouped = {}
    for em in emails:
        cat = em.get("category", "Other")
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(em)
    
    lines = []
    lines.append("🕸️ <b>LUCY — Net Scan Matinal</b>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(overview)
    lines.append("")
    
    # Ordenar categorias para exibir: Work primeiro, Promotions/Other no fim, etc.
    # Mas iteramos pelo que foi detectado:
    for cat, items in grouped.items():
        emoji = CATEGORY_EMOJIS.get(cat, "🗂️")
        lines.append(f"{emoji} <b>{cat}</b> ({len(items)})")
        
        for item in items:
            prio = item.get("priority", "low")
            color = PRIORITY_COLORS.get(prio, "🟢")
            summary = item.get("summary", "")
            
            # Escape HTML básico no resumo
            summary = summary.replace("<", "&lt;").replace(">", "&gt;")
            
            lines.append(f"  {color} {summary}")
        lines.append("")
    
    lines.append("━━━━━━━━━━━━━━━━━━")
    
    # Fuso horário BRT (UTC-3)
    hora_atual = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")
    lines.append(f"🕗 {hora_atual}")
    
    return "\n".join(lines)


# ============================================================
# IMAP FETCH
# ============================================================

def fetch_emails_via_imap(username: str, password: str) -> list:
    log.info("Conectando ao IMAP (%s)...", username)
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(username, password)
        mail.select('inbox')
    except Exception as exc:
        log.error("Falha ao conectar no IMAP: %s", exc)
        return []

    status, messages = mail.search(None, 'UNSEEN')
    if status != 'OK' or not messages[0]:
        mail.logout()
        return []

    email_ids = messages[0].split()
    emails_data = []
    
    log.info("Encontrados %d e-mails não lidos. Fazendo o parsing...", len(email_ids))

    for num in email_ids:
        # Fetch com suporte às extensões do Gmail (MSGID e LABELS)
        status, data = mail.fetch(num, '(X-GM-MSGID X-GM-LABELS RFC822)')
        if status != 'OK':
            continue

        response_header = data[0][0].decode('utf-8', errors='ignore')
        
        # Converter X-GM-MSGID (decimal) para Hex (API do Gmail)
        msg_id_match = re.search(r'X-GM-MSGID\s+(\d+)', response_header)
        email_id = ""
        if msg_id_match:
            email_id = hex(int(msg_id_match.group(1)))[2:]
        else:
            email_id = num.decode('utf-8')

        # Extrair categoria do Gmail
        labels_match = re.search(r'X-GM-LABELS\s+\((.*?)\)', response_header)
        gmail_category = "Other"
        if labels_match:
            labels = labels_match.group(1)
            if "CATEGORY_PROMOTIONS" in labels:
                gmail_category = "Promotions"
            elif "CATEGORY_SOCIAL" in labels:
                gmail_category = "Social"
            elif "CATEGORY_UPDATES" in labels:
                gmail_category = "Newsletter"
            elif "CATEGORY_PERSONAL" in labels:
                gmail_category = "Personal"

        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)

        # Assunto
        subject_header = msg.get("Subject", "")
        decoded_subject = decode_header(subject_header)
        subject_parts = []
        for part, encoding in decoded_subject:
            if isinstance(part, bytes):
                subject_parts.append(part.decode(encoding or "utf-8", errors="ignore"))
            else:
                subject_parts.append(str(part))
        subject = "".join(subject_parts)

        # Remetente
        from_header = msg.get("From", "")
        decoded_from = decode_header(from_header)
        from_parts = []
        for part, encoding in decoded_from:
            if isinstance(part, bytes):
                from_parts.append(part.decode(encoding or "utf-8", errors="ignore"))
            else:
                from_parts.append(str(part))
        from_ = "".join(from_parts)

        # Corpo / Snippet
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))

                if "attachment" not in content_disposition:
                    if content_type == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                        break
                    elif content_type == "text/html" and not body:
                        payload = part.get_payload(decode=True)
                        if payload:
                            html_content = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                            body = re.sub(r'<[^>]+>', ' ', html_content)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
        # Removemos excesso de whitespace para economizar tokens
        full_text = " ".join(body.split())
        
        # Guardamos um snippet curto (para display se precisar)
        snippet = full_text[:200]

        emails_data.append({
            "emailId": email_id,
            "subject": subject,
            "from": from_,
            "snippet": snippet,
            "full_body": full_text,
            "gmailCategory": gmail_category
        })

    mail.logout()
    return emails_data

# ============================================================
# MAIN
# ============================================================

def main() -> int:
    # Força stdout para UTF-8 (útil para Windows/Powershell)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description="Processa inbox do Gmail via Gemini (Lucy Agent).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Logs em nível DEBUG")
    args = parser.parse_args()

    setup_logging(args.verbose)
    
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        err = "Variável GEMINI_API_KEY ausente no .env"
        log.error(err)
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    gmail_user = os.getenv("GMAIL_USERNAME")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_pass:
        err = "Variáveis GMAIL_USERNAME e GMAIL_APP_PASSWORD ausentes no .env"
        log.error(err)
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    emails_data = fetch_emails_via_imap(gmail_user, gmail_pass)

    # Trata caso de inbox vazia
    if not emails_data:
        log.info("Nenhum e-mail encontrado. Pulando chamada de IA.")
        overview = "🌙 Inbox limpa. Até parece Night City numa segunda de manhã."
        digest_html = build_telegram_digest(overview, [])
        result = {
            "ok": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "digest_html": digest_html,
            "emails": []
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0

    gemini = GeminiClient(gemini_key)
    ai_result = gemini.process_emails(emails_data)
    
    if not ai_result:
        err = "Falha ao processar e-mails com Gemini"
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    overview = ai_result.get("overview", "")
    processed_emails = ai_result.get("emails", [])
    
    log.info("Recebido do Gemini: %d e-mails categorizados.", len(processed_emails))

    digest_html = build_telegram_digest(overview, processed_emails)

    result = {
        "ok": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "digest_html": digest_html,
        "emails": processed_emails
    }

    print(json.dumps(result, ensure_ascii=False))
    log.info("✅ Processamento finalizado com sucesso.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
