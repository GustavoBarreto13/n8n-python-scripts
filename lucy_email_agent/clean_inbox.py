#!/usr/bin/env python3
"""
clean_inbox.py
=====================================
Script one-off para limpar o backlog de e-mails da Inbox (ex: milhares de e-mails).
Ele varre a caixa inteira em lotes, pede para a IA categorizar apenas as labels
(sem gerar resumos ou intel para economizar tokens e tempo) e já aplica o arquivamento.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
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
GEMINI_DELAY = 6.0 # 6 segundos para evitar Rate Limit com folga (15 RPM no tier gratuito)
MAX_RETRIES = 5
RETRY_BACKOFF = 15.0 # Esperar mais tempo em caso de 429 (começa em 15s, depois 30s, etc)

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lucy_cleaner")

_last_http_error: Optional[str] = None

SYSTEM_PROMPT = (
    "Você é uma IA de categorização estrita. "
    "Sua única função é ler os e-mails e retornar a categoria correta para cada 'uid'.\n"
    "DIRETRIZES DE CATEGORIZAÇÃO:\n"
    "- Art / Hobbies: Arte, hobbies, esportes, interesses pessoais e lazer.\n"
    "- Finance: Faturas, boletos, bancos, Pix, comprovantes e investimentos.\n"
    "- Knowledge: Newsletters, artigos, cursos, aprendizado, blogs, newsletter no assunto ou no corpo.\n"
    "- Shopping: Rastreio de entregas, recibos de compras, ofertas, cupons e e-commerce.\n"
    "- Personal: E-mails diretos de pessoas (amigos, familiares), viagens, eventos sociais, voos e redes sociais.\n"
    "- Health: Exames, resultados, médicos, farmácia, bem-estar.\n"
    "- Security: Alertas de login, senhas, códigos de verificação, OTP e acessos novos.\n"
    "- Work: Trabalho, chefe, clientes, corporativo.\n"
    "- Junk: Lixo inútil, termos de uso irrelevantes, spam, promoções de lojas, cupons, marketing de vendas, LinkedIn, Instagram, notificações de redes sociais, eventos sociais.\n"
    "- Other: Tudo que não couber acima."
)

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "uid": {"type": "STRING"},
            "category": {
                "type": "STRING",
                "enum": ["Art / Hobbies", "Finance", "Knowledge", "Shopping", "Personal", "Health", "Security", "Work", "Junk", "Other"]
            }
        },
        "required": ["uid", "category"]
    }
}

IMAP_LABEL_MAP = {
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
                log.warning("Rate limit 429. Aguardando %ds...", wait)
                time.sleep(wait)
                continue
            if 500 <= resp.status_code < 600:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning("Erro 5xx. Aguardando %ds...", wait)
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

def process_batch_gemini(api_key: str, emails_data: list) -> list:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    prompt = f"Categorize os seguintes e-mails:\n\n{json.dumps(emails_data, ensure_ascii=False, indent=2)}"
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": RESPONSE_SCHEMA
        }
    }
    
    resp = http_request("POST", url, params={"key": api_key}, json_body=body)
    time.sleep(GEMINI_DELAY)
    
    if not resp:
        log.error("Gemini falhou. Erro: %s", _last_http_error)
        return []
        
    try:
        raw = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except Exception as exc:
        log.error("Falha ao parsear reposta: %s", exc)
        return []

def extract_email_data(mail, uid_bytes) -> Optional[dict]:
    status, data = mail.uid('FETCH', uid_bytes, '(RFC822)')
    if status != 'OK' or not data[0]:
        return None

    raw_email = data[0][1]
    msg = email.message_from_bytes(raw_email)

    # Subject
    subject_header = msg.get("Subject", "")
    decoded_subject = decode_header(subject_header)
    subject_parts = []
    for part, encoding in decoded_subject:
        if isinstance(part, bytes):
            try:
                subject_parts.append(part.decode(encoding or "utf-8", errors="ignore"))
            except LookupError:
                subject_parts.append(part.decode("utf-8", errors="ignore"))
        else:
            subject_parts.append(str(part))
    subject = "".join(subject_parts)

    # From
    from_header = msg.get("From", "")
    decoded_from = decode_header(from_header)
    from_parts = []
    for part, encoding in decoded_from:
        if isinstance(part, bytes):
            try:
                from_parts.append(part.decode(encoding or "utf-8", errors="ignore"))
            except LookupError:
                from_parts.append(part.decode("utf-8", errors="ignore"))
        else:
            from_parts.append(str(part))
    from_ = "".join(from_parts)

    # Body
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
    snippet = full_text[:200]

    return {
        "uid": uid_bytes.decode('utf-8'),
        "subject": subject,
        "from": from_,
        "snippet": snippet
    }

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description="Limpa a Inbox em massa.")
    parser.add_argument("--batch-size", type=int, default=50, help="E-mails por lote")
    parser.add_argument("--limit", type=int, default=0, help="Limite total de e-mails a processar (0 = Todos)")
    args = parser.parse_args()

    gemini_key = os.getenv("GEMINI_API_KEY")
    gmail_user = os.getenv("GMAIL_USERNAME")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD")

    if not all([gemini_key, gmail_user, gmail_pass]):
        log.error("Credenciais ausentes no .env!")
        return

    log.info("Conectando ao IMAP...")
    mail = imaplib.IMAP4_SSL('imap.gmail.com')
    mail.login(gmail_user, gmail_pass)
    mail.select('inbox')

    # Busca TODOS os e-mails na Inbox
    status, messages = mail.uid('SEARCH', None, 'ALL')
    if status != 'OK' or not messages[0]:
        log.info("Nenhum e-mail na Inbox. Trabalho concluído!")
        mail.logout()
        return

    email_uids = messages[0].split()
    email_uids.reverse() # Mais novos primeiro
    
    if args.limit > 0:
        email_uids = email_uids[:args.limit]

    total_emails = len(email_uids)
    log.info("Encontrados %d e-mails na Inbox para processar.", total_emails)

    batch_size = args.batch_size
    total_batches = (total_emails + batch_size - 1) // batch_size

    for i in range(0, total_emails, batch_size):
        batch_uids = email_uids[i:i+batch_size]
        batch_num = i // batch_size + 1
        
        log.info("--- Processando Lote %d de %d (%d e-mails) ---", batch_num, total_batches, len(batch_uids))
        
        emails_data = []
        for uid in batch_uids:
            data = extract_email_data(mail, uid)
            if data:
                emails_data.append(data)
                
        if not emails_data:
            continue
            
        log.info("Enviando %d e-mails para o Gemini...", len(emails_data))
        categories = process_batch_gemini(gemini_key, emails_data)
        
        if not categories:
            log.warning("Falha na IA. Pulando lote.")
            continue
            
        log.info("Aplicando labels e arquivando lote...")
        for item in categories:
            uid_str = item.get("uid")
            cat = item.get("category", "Other")
            if not uid_str:
                continue
                
            uid_bytes = uid_str.encode('utf-8')
            label = IMAP_LABEL_MAP.get(cat)
            
            try:
                if label:
                    mail.uid('STORE', uid_bytes, '+X-GM-LABELS', label)
                mail.uid('STORE', uid_bytes, '-X-GM-LABELS', '\\Inbox')
            except Exception as e:
                log.error("Erro ao arquivar uid %s: %s", uid_str, e)
                
        log.info("Lote %d concluído.", batch_num)

    log.info("Todos os lotes foram processados! Inbox limpís sima.")
    mail.logout()

if __name__ == "__main__":
    main()
