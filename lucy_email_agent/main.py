#!/usr/bin/env python3
"""
main.py (lucy_email_agent/main.py)
=====================================
Processa e-mails via Gemini REST API (gemini-2.0-flash) usando a persona da Lucy,
formata um digest em HTML para envio ao Telegram e retorna os dados estruturados.

Agora gerencia completamente as Labels e Arquivamento via IMAP!
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

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_DELAY = 0.5
MAX_RETRIES = 5
RETRY_BACKOFF = 15.0

CATEGORY_EMOJIS = {
    "Art / Hobbies": "🎭",
    "Finance": "💵",
    "Knowledge": "🎓",
    "Shopping": "🛒",
    "Personal": "👤",
    "Health": "⚕️",
    "Security": "🔒",
    "Work": "💼",
    "Junk": "🗑️",
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
    "1. 'overview': Frase irônica e seca sobre o estado da inbox.\n"
    "2. 'intel_briefing': Resumo consolidado de todas as Newsletters e Notícias encontradas. Junte os assuntos principais num ou dois parágrafos concisos. Se não houver notícias, deixe vazio.\n"
    "3. 'action_items': Liste as pendências críticas do dia (boletos vencendo hoje, reuniões urgentes, alertas de segurança).\n"
    "4. 'emails': Categorize, priorize e resuma cada e-mail (1 linha máxima).\n\n"
    "DIRETRIZES DE CATEGORIZAÇÃO:\n"
    "- Art / Hobbies: Arte, hobbies, esportes, interesses pessoais e lazer.\n"
    "- Finance: Faturas, boletos, bancos, Pix, comprovantes e investimentos.\n"
    "- Knowledge: Newsletters, artigos, cursos, aprendizado, blogs, newsletter no assunto ou no corpo.\n"
    "- Shopping: Rastreio de entregas, recibos de compras, ofertas, cupons e e-commerce.\n"
    "- Personal: E-mails diretos de pessoas (amigos, familiares), viagens, eventos sociais, voos e redes sociais.\n"
    "- Health: Exames, resultados, médicos, farmácia, bem-estar.\n"
    "- Security: Alertas de login, senhas, códigos de verificação, OTP e acessos novos.\n"
    "- Work: Trabalho, chefe, clientes, corporativo.\n"
    "- Junk: Lixo inútil, termos de uso irrelevantes, spam, promoções de lojas, cupons, marketing de vendas, LinkedIn, Instagram, notificações de redes sociais, eventos sociais. (Junk não será exibido no Telegram, então use sem dó para limpar o ruído).\n"
    "- Other: Tudo que não couber acima.\n\n"
    "Preserve sempre o 'uid' original."
)

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "overview": {
            "type": "STRING",
            "description": "Frase seca sobre o estado da inbox hoje."
        },
        "intel_briefing": {
            "type": "STRING",
            "description": "Resumo consolidado das principais notícias encontradas nas newsletters."
        },
        "action_items": {
            "type": "ARRAY",
            "items": {"type": "STRING", "description": "Ação necessária (ex: Pagar boleto, Responder chefe)"}
        },
        "emails": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "uid": {"type": "STRING", "description": "ID original da mensagem IMAP"},
                    "category": {
                        "type": "STRING",
                        "enum": ["Art / Hobbies", "Finance", "Knowledge", "Shopping", "Personal", "Health", "Security", "Work", "Junk", "Other"]
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
                "required": ["uid", "category", "priority", "summary", "action"]
            }
        }
    },
    "required": ["overview", "emails"]
}

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
                time.sleep(wait)
                continue
            if 500 <= resp.status_code < 600:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                return None
            if 400 <= resp.status_code < 500:
                body_preview = (resp.text or "")[:500]
                _last_http_error = f"{resp.status_code} {method}: {body_preview}"
                return None
            return resp.json() if resp.content else {}
        except requests.RequestException as exc:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            time.sleep(wait)
    return None

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
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            parsed = json.loads(raw)
            usage = resp.get("usageMetadata", {})
            return {"data": parsed, "usage": usage}
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            log.error("Falha ao parsear resposta do Gemini: %s", exc)
            return None

def build_telegram_digest(overview: str, intel_briefing: str, action_items: list, emails: list, usage: dict) -> str:
    grouped = {}
    for em in emails:
        cat = em.get("category", "Other")
        if cat == "Junk":
            continue
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(em)
    
    lines = []
    lines.append("🕸️ <b>LUCY — Net Scan Matinal</b>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>\"{overview}\"</i>")
    lines.append("")
    
    if intel_briefing:
        lines.append("🌐 <b>INTEL BRIEFING:</b>")
        lines.append(intel_briefing)
        lines.append("")
        
    if action_items:
        lines.append("🚨 <b>AÇÃO IMEDIATA:</b>")
        for act in action_items:
            lines.append(f"• {act}")
        lines.append("")
    
    for cat, items in grouped.items():
        emoji = CATEGORY_EMOJIS.get(cat, "🗂️")
        lines.append(f"{emoji} <b>{cat}</b> ({len(items)})")
        for item in items:
            prio = item.get("priority", "low")
            color = PRIORITY_COLORS.get(prio, "🟢")
            summary = item.get("summary", "")
            summary = summary.replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"  {color} {summary}")
        lines.append("")
    
    lines.append("━━━━━━━━━━━━━━━━━━")
    
    in_tokens = usage.get("promptTokenCount", 0)
    out_tokens = usage.get("candidatesTokenCount", 0)
    cost = (in_tokens / 1_000_000) * 0.10 + (out_tokens / 1_000_000) * 0.40
    
    lines.append(f"🧠 Tokens: {in_tokens:,} in | {out_tokens:,} out")
    lines.append(f"💸 Custo: ~${cost:.5f}")
    
    hora_atual = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")
    lines.append(f"🕗 {hora_atual}")
    
    return "\n".join(lines)

def fetch_emails_via_imap(username: str, password: str) -> tuple:
    log.info("Conectando ao IMAP (%s)...", username)
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com', timeout=30)
        mail.login(username, password)
        mail.select('inbox')
    except Exception as exc:
        log.error("Falha ao conectar no IMAP: %s", exc)
        return None, []

    # E-mails de exatamente ontem
    yesterday = datetime.now() - timedelta(days=1)
    today = datetime.now()
    since_str = yesterday.strftime("%d-%b-%Y")
    before_str = today.strftime("%d-%b-%Y")

    search_criteria = f'(SINCE "{since_str}" BEFORE "{before_str}")'
    status, messages = mail.uid('SEARCH', None, search_criteria)
    
    if status != 'OK' or not messages[0]:
        return mail, []

    email_uids = messages[0].split()
    
    if len(email_uids) > 50:
        log.warning("Muitos e-mails ontem (%d). Limitando aos 50 mais recentes.", len(email_uids))
        email_uids = email_uids[-50:]
        
    emails_data = []
    log.info("Encontrados %d e-mails de ontem. Fazendo o parsing...", len(email_uids))

    for uid in email_uids:
        status, data = mail.uid('FETCH', uid, '(RFC822)')
        if status != 'OK':
            continue

        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)

        # Assunto
        subject_header = msg.get("Subject", "")
        decoded_subject = decode_header(subject_header)
        subject_parts = []
        for part, encoding in decoded_subject:
            if isinstance(part, bytes):
                enc = encoding or "utf-8"
                try:
                    subject_parts.append(part.decode(enc, errors="ignore"))
                except LookupError:
                    subject_parts.append(part.decode("utf-8", errors="ignore"))
            else:
                subject_parts.append(str(part))
        subject = "".join(subject_parts)

        # Remetente
        from_header = msg.get("From", "")
        decoded_from = decode_header(from_header)
        from_parts = []
        for part, encoding in decoded_from:
            if isinstance(part, bytes):
                enc = encoding or "utf-8"
                try:
                    from_parts.append(part.decode(enc, errors="ignore"))
                except LookupError:
                    from_parts.append(part.decode("utf-8", errors="ignore"))
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
        
        full_text = " ".join(body.split())
        snippet = full_text[:500]

        emails_data.append({
            "uid": uid.decode('utf-8'),
            "subject": subject,
            "from": from_,
            "snippet": snippet,
        })

    return mail, emails_data

def archive_and_label_emails(mail, processed_emails):
    imap_label_map = {
        "Art / Hobbies": '"Art / Hobbies"',
        "Finance": '"Finance"',
        "Knowledge": '"Knowledge"',
        "Shopping": '"Shopping"',
        "Personal": '"Personal"',
        "Health": '"Health"',
        "Security": '"Security"',
        "Work": '"Work"',
        "Junk": '"Junk"',
        "Other": '"Other"'
    }

    log.info("Iniciando aplicação de labels e arquivamento via IMAP...")
    for em in processed_emails:
        uid = em.get("uid")
        if not uid:
            continue
            
        uid_bytes = uid.encode('utf-8')
        cat = em.get("category", "Other")
        
        label = imap_label_map.get(cat)
        if label:
            mail.uid('STORE', uid_bytes, '+X-GM-LABELS', label)
            
        # Archive (Remove \Inbox label)
        mail.uid('STORE', uid_bytes, '-X-GM-LABELS', '\\Inbox')
        
    log.info("Processo de arquivamento concluído.")

def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description="Processa inbox do Gmail via Gemini (Lucy Agent).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Logs em nível DEBUG")
    args = parser.parse_args()

    setup_logging(args.verbose)
    
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        log.error("Variável GEMINI_API_KEY ausente no .env")
        print(json.dumps({"ok": False, "error": "GEMINI_API_KEY ausente"}, ensure_ascii=False))
        return 1

    gmail_user = os.getenv("GMAIL_USERNAME")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_pass:
        log.error("Variáveis GMAIL_USERNAME e GMAIL_APP_PASSWORD ausentes")
        print(json.dumps({"ok": False, "error": "Credenciais Gmail ausentes"}, ensure_ascii=False))
        return 1

    mail, emails_data = fetch_emails_via_imap(gmail_user, gmail_pass)

    if not emails_data:
        log.info("Nenhum e-mail de ontem encontrado.")
        overview = "🌙 A rede estava quieta ontem. Nada de novo na inbox."
        digest_html = build_telegram_digest(overview, "", [], [], {})
        result = {
            "ok": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "digest_html": digest_html,
            "emails": []
        }
        print(json.dumps(result, ensure_ascii=False))
        if mail:
            mail.logout()
        return 0

    gemini = GeminiClient(gemini_key)
    ai_result = gemini.process_emails(emails_data)
    
    if not ai_result or "data" not in ai_result:
        print(json.dumps({"ok": False, "error": "Falha no Gemini"}, ensure_ascii=False))
        if mail:
            mail.logout()
        return 1

    overview = ai_result["data"].get("overview", "")
    intel_briefing = ai_result["data"].get("intel_briefing", "")
    action_items = ai_result["data"].get("action_items", [])
    processed_emails = ai_result["data"].get("emails", [])
    usage = ai_result.get("usage", {})
    
    log.info("Recebido do Gemini: %d e-mails categorizados.", len(processed_emails))

    if mail:
        archive_and_label_emails(mail, processed_emails)
        mail.logout()

    digest_html = build_telegram_digest(overview, intel_briefing, action_items, processed_emails, usage)

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
