#!/usr/bin/env python3
import argparse
import base64
import json
import os
import sys
from pathlib import Path
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

def main():
    parser = argparse.ArgumentParser(description="Transcreve áudio do Telegram via Gemini.")
    parser.add_argument("--file_id", required=True, help="ID do arquivo de áudio no Telegram")
    parser.add_argument("--chat_id", required=True, help="ID do chat do Telegram")
    args = parser.parse_args()

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    gemini_key = os.getenv("GEMINI_API_KEY")

    if not telegram_token or not gemini_key:
        print(json.dumps({"error": "TELEGRAM_BOT_TOKEN ou GEMINI_API_KEY ausente"}))
        sys.exit(1)

    try:
        # 1. Get file path
        file_info_url = f"https://api.telegram.org/bot{telegram_token}/getFile"
        file_info_resp = requests.get(file_info_url, params={"file_id": args.file_id})
        file_info_resp.raise_for_status()
        file_path = file_info_resp.json()["result"]["file_path"]

        # 2. Get audio file
        audio_url = f"https://api.telegram.org/file/bot{telegram_token}/{file_path}"
        audio_resp = requests.get(audio_url)
        audio_resp.raise_for_status()
        audio_base64 = base64.b64encode(audio_resp.content).decode('utf-8')

        # 3. Call Gemini
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
        payload = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": "audio/ogg", "data": audio_base64}},
                    {"text": "Transcreva o áudio em português."}
                ]
            }]
        }
        gemini_resp = requests.post(
            gemini_url, 
            params={"key": gemini_key}, 
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        gemini_resp.raise_for_status()
        gemini_data = gemini_resp.json()
        
        text = gemini_data["candidates"][0]["content"]["parts"][0]["text"]
        print(json.dumps({"text": text.strip(), "chatId": args.chat_id}))
        
    except requests.RequestException as e:
        print(json.dumps({"error": f"Erro na requisição HTTP: {str(e)}"}))
        sys.exit(1)
    except (KeyError, IndexError) as e:
        print(json.dumps({"error": "Falha ao extrair dados do Gemini", "details": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
