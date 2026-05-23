#!/usr/bin/env python3
"""
spotify_sync/get_token.py
=========================
Helper local de OAuth Authorization Code para obter um refresh_token de longa duração
do Spotify. Rodar UMA vez no PC do usuário e copiar o refresh_token para o Dokploy.

Pré-requisitos:
  1. App criado em developer.spotify.com/dashboard
  2. Redirect URI cadastrado: http://127.0.0.1:8888/callback
  3. SPOTIFY_CLIENT_ID e SPOTIFY_CLIENT_SECRET em .env (raiz do repo) ou env vars

Uso:
    python spotify_sync/get_token.py
"""

import base64
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests

SCOPE = "user-read-recently-played"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"


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


_received: dict = {}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        _received["code"] = (qs.get("code") or [None])[0]
        _received["state"] = (qs.get("state") or [None])[0]
        _received["error"] = (qs.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "OK — pode fechar esta aba." if _received["code"] else f"Erro: {_received['error']}"
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode("utf-8"))

    def log_message(self, fmt, *args):
        pass  # silencia logs


def main() -> int:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("ERRO: defina SPOTIFY_CLIENT_ID e SPOTIFY_CLIENT_SECRET em .env ou env vars.",
              file=sys.stderr)
        return 1

    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer(("127.0.0.1", 8888), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"Abrindo browser para autorização do Spotify...")
    print(f"Se não abrir automaticamente, acesse: {url}")
    webbrowser.open(url)

    # Aguarda callback
    while "code" not in _received and "error" not in _received:
        pass
    server.shutdown()

    if _received.get("error"):
        print(f"ERRO no callback: {_received['error']}", file=sys.stderr)
        return 1
    if _received.get("state") != state:
        print("ERRO: state mismatch (CSRF)", file=sys.stderr)
        return 1

    code = _received["code"]
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"ERRO ao trocar code por token: {resp.status_code} {resp.text}", file=sys.stderr)
        return 1

    payload = resp.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        print(f"ERRO: refresh_token não retornado. Resposta: {payload}", file=sys.stderr)
        return 1

    print()
    print("=" * 70)
    print("SUCESSO. Copie o valor abaixo para a env var SPOTIFY_REFRESH_TOKEN")
    print("no Dokploy (ou no seu .env local):")
    print("=" * 70)
    print(refresh_token)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
