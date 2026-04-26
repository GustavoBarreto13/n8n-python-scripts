# n8n Python Scripts

Scripts Python usados nos workflows do n8n. Cada script é um módulo independente chamado via `execSync` em Code nodes.

## Como funciona

O n8n não executa Python nativamente — ele usa `child_process` do Node.js pra chamar o interpretador Python do container. O script roda, imprime JSON no stdout, e o n8n captura esse output.

```
n8n Code node
    └── execSync('/opt/venv/bin/python3 /home/node/scripts/<script>/main.py')
            └── stdout (JSON) → n8n item
```

## Estrutura

```
n8n-python-scripts/
├── CLAUDE.md               ← contexto para o Claude Code
├── README.md               ← este arquivo
├── requirements.txt        ← libs compartilhadas
├── anime_sync/
│   ├── main.py             ← entry point
│   ├── mal.py              ← cliente Jikan/MAL
│   ├── anilist.py          ← cliente AniList
│   ├── tmdb.py             ← cliente TMDB
│   ├── notion.py           ← cliente Notion
│   └── README.md
└── lucy_digest/            ← em desenvolvimento
    ├── main.py
    └── README.md
```

## Scripts

### `anime_sync`

Sincroniza a lista de animes do MAL com uma database no Notion.

- Busca metadados via Jikan (MAL) e AniList
- Detecta temporada dinamicamente no TMDB
- Faz upsert no Notion por MAL ID

**Trigger**: workflow `MyAnimeBoxd` (`N_l9odG0de5B1LlSXQTdO`)

### `lucy_digest` *(em desenvolvimento)*

Processa emails do Gmail com a personalidade da Lucy (Cyberpunk Edgerunners) e envia digest diário pro Telegram.

## Padrão de retorno

Todo script retorna JSON via stdout:

```python
import json, sys

def main():
    try:
        print(json.dumps({"status": "ok", "data": [...]}))
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
```

Logs de debug devem ir para stderr para não poluir o JSON:

```python
import sys
print("debug info", file=sys.stderr)
```

## Deploy

Push no GitHub → webhook → n8n roda `git pull` automaticamente em `/home/node/scripts`. Sem rebuild, sem redeploy.

```bash
git add .
git commit -m "fix: descrição"
git push
```

## Desenvolvimento local

Os scripts rodam dentro do container n8n. Para testar:

```bash
# Acessar o container
docker exec -it <container_id> bash

# Rodar um script
/opt/venv/bin/python3 /home/node/scripts/anime_sync/main.py

# Ver output formatado
/opt/venv/bin/python3 /home/node/scripts/anime_sync/main.py | python3 -m json.tool
```

## Libs disponíveis

Instaladas no venv `/opt/venv` do container:

| Lib | Uso |
|---|---|
| `requests` | HTTP calls para APIs |
| `pandas` | Manipulação de dados |
| `numpy` | Operações numéricas |
| `beautifulsoup4` | Web scraping |
| `openpyxl` | Arquivos Excel |
| `yt-dlp` | Download de mídia |

Para adicionar libs: editar o `Dockerfile` do n8n (requer rebuild).

## Infraestrutura

- **Container**: `node:22-bookworm` com Python em `/opt/venv/bin/python3`
- **Volume**: `/opt/n8n-python-scripts` (host) → `/home/node/scripts` (container)
- **Sync**: GitHub webhook → workflow `GitHub Scripts Sync` (`GOl6dewkyrftw9yU`)
- **n8n**: `https://n8n.gusstavo42-vps.cloud`