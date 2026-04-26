#!/usr/bin/env python3
"""
get_token.py — bootstrap TickTick OAuth tokens para o ticktick_notion_sync.

Rode UMA VEZ localmente. Abre o browser na página de autorização do TickTick,
captura o callback em http://localhost:8765/callback, troca o código por
access_token + refresh_token, e grava em ticktick_notion_sync/.last_sync.json.

Pré-requisito (único):
  Registrar um app em https://developer.ticktick.com/manage com
  "OAuth Redirect URL" = http://localhost:8765/callback

Variáveis de ambiente necessárias:
  TICKTICK_CLIENT_ID      - Client ID do app
  TICKTICK_CLIENT_SECRET  - Client Secret do app
"""

import json
import os
import secrets
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


REDIRECT_URI = "http://localhost:8888/callback"
PORT = 8888
TICKTICK_AUTH_URL = "https://ticktick.com/oauth/authorize"
TICKTICK_TOKEN_URL = "https://ticktick.com/oauth/token"
SCOPE = "tasks:read tasks:write"
STATE_FILE = Path(__file__).parent / ".last_sync.json"


def load_env() -> None:
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


def build_auth_url(client_id: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "scope": SCOPE,
        "state": state,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
    }
    return f"{TICKTICK_AUTH_URL}?{urlencode(params)}"


_captured: dict[str, str] = {}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        code = qs.get("code", [None])[0]
        state = qs.get("state", [None])[0]
        if code:
            _captured["code"] = code
            _captured["state"] = state or ""
            body = (
                b"<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
                b"<h2>&#10003; Autorizado!</h2>"
                b"<p>Pode fechar esta aba e voltar ao terminal.</p>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code received")

    def log_message(self, *_):
        pass


def wait_for_callback() -> tuple[str, str]:
    server = HTTPServer(("localhost", PORT), CallbackHandler)
    print(f"Aguardando callback do TickTick em http://localhost:8888/callback ...")
    while "code" not in _captured:
        server.handle_request()
    server.server_close()
    return _captured["code"], _captured.get("state", "")


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    body = urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": SCOPE,
    }).encode()
    req = Request(
        TICKTICK_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def save_tokens(tokens: dict) -> None:
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 15552000)))
    ).isoformat()

    state: dict = {}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    state["ticktick_access_token"] = tokens["access_token"]
    if tokens.get("refresh_token"):
        state["ticktick_refresh_token"] = tokens["refresh_token"]
    state["ticktick_expires_at"] = expires_at

    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp.replace(STATE_FILE)


def main() -> int:
    load_env()

    client_id = os.getenv("TICKTICK_CLIENT_ID")
    client_secret = os.getenv("TICKTICK_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("Erro: TICKTICK_CLIENT_ID e TICKTICK_CLIENT_SECRET precisam estar no .env ou no ambiente.")
        return 1

    csrf_state = secrets.token_urlsafe(32)
    auth_url = build_auth_url(client_id, csrf_state)

    print()
    print("=== TickTick OAuth Bootstrap ===")
    print()
    print(f"Pré-requisito: registre {REDIRECT_URI} como OAuth Redirect URL em")
    print("  https://developer.ticktick.com/manage")
    print()
    print("Abrindo o browser para autorização...")
    print(f"  {auth_url}")
    print()
    webbrowser.open(auth_url)

    code, returned_state = wait_for_callback()
    if returned_state and returned_state != csrf_state:
        print(f"Erro: state CSRF não bateu (esperado={csrf_state} recebido={returned_state})")
        return 1
    print("Código recebido. Trocando por tokens...")

    try:
        tokens = exchange_code(client_id, client_secret, code)
    except Exception as e:
        print(f"Erro ao trocar o código: {e}")
        return 1

    if "access_token" not in tokens:
        print(f"Resposta inesperada do TickTick: {tokens}")
        return 1

    save_tokens(tokens)
    print(f"Tokens salvos em: {STATE_FILE}")
    print()
    print("Próximo passo: faça push, aguarde o sync, então teste com:")
    print("  /opt/venv/bin/python3 /home/node/scripts/ticktick_notion_sync/main.py --bootstrap --dry-run -v")
    return 0


if __name__ == "__main__":
    sys.exit(main())
