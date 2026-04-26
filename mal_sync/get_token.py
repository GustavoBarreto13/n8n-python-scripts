#!/usr/bin/env python3
"""
get_token.py — bootstrap MAL OAuth tokens para o mal_sync.

Rode UMA VEZ localmente. Abre o browser no MAL, captura o callback em
localhost:8765, troca o código por access_token + refresh_token, e grava
tudo em mal_sync/.last_sync.json.

Pré-requisito (único):
  Adicionar "http://localhost:8765" como Allowed Redirect URI no seu
  app MAL em: https://myanimelist.net/apiconfig

Variáveis de ambiente necessárias:
  MAL_CLIENT_ID      - Client ID do app MAL
  MAL_CLIENT_SECRET  - Client Secret do app MAL

Exemplo:
  MAL_CLIENT_ID=xxx MAL_CLIENT_SECRET=yyy python3 get_token.py
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


REDIRECT_URI = "http://localhost:49373"
MAL_AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
MAL_TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
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


def make_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def build_auth_url(client_id: str, code_verifier: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": code_verifier,
        "code_challenge_method": "plain",
    }
    return f"{MAL_AUTH_URL}?{urlencode(params)}"


_captured_code: list[str] = []


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        code = qs.get("code", [None])[0]
        if code:
            _captured_code.append(code)
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
        pass  # silencia o log do servidor HTTP


def wait_for_callback() -> str:
    server = HTTPServer(("localhost", 49373), CallbackHandler)
    print("Aguardando callback do MAL em http://localhost:49373 ...")
    while not _captured_code:
        server.handle_request()
    server.server_close()
    return _captured_code[0]


def exchange_code(client_id: str, client_secret: str, code: str, code_verifier: str) -> dict:
    body = urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }).encode()

    req = Request(
        MAL_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def save_tokens(tokens: dict) -> None:
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 2592000)))
    ).isoformat()

    # Preserve existing state (e.g. last_sync_time) if file already exists.
    state: dict = {}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    state["mal_access_token"] = tokens["access_token"]
    state["mal_refresh_token"] = tokens["refresh_token"]
    state["mal_expires_at"] = expires_at

    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def main() -> int:
    load_env()

    client_id = os.getenv("MAL_CLIENT_ID")
    client_secret = os.getenv("MAL_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("Erro: MAL_CLIENT_ID e MAL_CLIENT_SECRET precisam estar no .env ou no ambiente.")
        return 1

    code_verifier = make_code_verifier()
    auth_url = build_auth_url(client_id, code_verifier)

    print()
    print("=== MAL OAuth Bootstrap ===")
    print()
    print("Pré-requisito: adicione http://localhost:8765 como Allowed Redirect URI em")
    print("  https://myanimelist.net/apiconfig")
    print()
    print("Abrindo o browser para autorização...")
    print(f"  {auth_url}")
    print()
    webbrowser.open(auth_url)

    code = wait_for_callback()
    print(f"Código recebido. Trocando por tokens...")

    try:
        tokens = exchange_code(client_id, client_secret, code, code_verifier)
    except Exception as e:
        print(f"Erro ao trocar o código: {e}")
        return 1

    if "access_token" not in tokens:
        print(f"Resposta inesperada do MAL: {tokens}")
        return 1

    save_tokens(tokens)
    print(f"Tokens salvos em: {STATE_FILE}")
    print()
    print("Próximo passo: faça push, aguarde o sync, então teste com:")
    print("  /opt/venv/bin/python3 /home/node/scripts/mal_sync/main.py --dry-run --full -v")
    return 0


if __name__ == "__main__":
    sys.exit(main())
