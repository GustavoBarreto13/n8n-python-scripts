# CLAUDE.md — n8n Python Scripts

Guia de contexto para o Claude Code trabalhar neste repositório.

---

## O que é este repo

Scripts Python chamados pelo n8n via `execSync` em Code nodes. Cada script é um módulo independente que executa uma tarefa específica e retorna JSON via stdout.
Atualize este documento com qualquer informação relevante.
O n8n não executa os scripts diretamente como subprocesso — ele usa o node `child_process` do Node.js pra chamar o Python.

---

## Infraestrutura

### VPS
- **Host**: `n8n.gusstavo42-vps.cloud`
- **Provedor**: Hostinger VPS
- **Deploy**: Dokploy (Docker)

### Container n8n
- **Imagem base**: `node:22-bookworm`
- **Python**: `/opt/venv/bin/python3`
- **Scripts**: `/home/node/scripts/` (bind mount do host)
- **User dentro do container**: `node` (UID 1000)

### Volume (Bind Mount)
| Host | Container |
|---|---|
| `/opt/n8n-scripts` | `/home/node/scripts` |
| `/root/.ssh` | `/home/node/.ssh` |

Os scripts ficam no host em `/opt/n8n-scripts` e são montados no container em `/home/node/scripts`. Editar no host = reflete imediatamente no container, sem rebuild.

### Sync automático
Push no GitHub → webhook → n8n workflow `GitHub Scripts Sync` (ID: `GOl6dewkyrftw9yU`) → `git reset --hard HEAD && git pull` em `/home/node/scripts`.

---

## Como o n8n chama os scripts

Em Code nodes do n8n, o padrão usado é:

```javascript
const { execSync } = require('child_process');

const result = execSync(
  '/opt/venv/bin/python3 /home/node/scripts/nome_do_script/main.py',
  { encoding: 'utf8', timeout: 60000 }
);

const data = JSON.parse(result);
return [{ json: data }];
```

**Importante**: o script Python deve sempre retornar JSON válido via `print()` no stdout. Erros devem ser capturados e retornados como JSON também.

---

## Variáveis de ambiente disponíveis no container

As env vars do n8n estão disponíveis nos scripts Python via `os.environ`:

```python
import os

# Configuradas no Dockerfile / Dokploy
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")        # via OPENAPI_MCP_HEADERS
N8N_HOST     = os.environ.get("N8N_HOST")            # n8n.gusstavo42-vps.cloud
```

Credenciais de APIs externas (MAL, AniList, TMDB, etc.) devem ser passadas como argumentos pelo n8n ou lidas de env vars configuradas no Dokploy.

---

## Estrutura de pastas

```
n8n-python-scripts/
├── CLAUDE.md                  ← este arquivo
├── requirements.txt           ← libs Python compartilhadas
├── anime_sync/
│   ├── main.py                ← entry point chamado pelo n8n
│   ├── tmdb.py                ← módulo TMDB
│   ├── mal.py                 ← módulo MAL/Jikan
│   ├── anilist.py             ← módulo AniList
│   ├── notion.py              ← módulo Notion
│   └── README.md
└── lucy_digest/               ← futuro
    ├── main.py
    └── README.md
```

Cada script fica numa subpasta própria com seu `main.py` como entry point.

---

## Libs Python disponíveis

Instaladas no venv `/opt/venv` via Dockerfile:

```
requests
pandas
numpy
beautifulsoup4
openpyxl
yt-dlp
```

Para adicionar libs novas: editar o `Dockerfile` no repo do n8n (requer rebuild) **ou** instalar manualmente no container para testes:

```bash
/opt/venv/bin/pip install nome-da-lib
```

Libs instaladas manualmente não sobrevivem ao redeploy — colocar no `Dockerfile` para persistir.

---

## Padrão de retorno dos scripts

Todo script deve retornar JSON no stdout:

```python
import json
import sys

def main():
    try:
        resultado = {"status": "ok", "data": [...]}
        print(json.dumps(resultado, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
```

---

## anime_sync — Contexto

Sincroniza animes do MAL/AniList com uma database no Notion.

### APIs utilizadas
| API | Base URL | Auth |
|---|---|---|
| Jikan (MAL) | `https://api.jikan.moe/v4` | Nenhuma |
| AniList | `https://graphql.anilist.co` | Nenhuma |
| TMDB | `https://api.themoviedb.org/3` | Bearer token |
| Notion | `https://api.notion.com/v1` | Bearer token |

### Rate limits
- **Jikan**: 3 req/s, 60 req/min
- **AniList**: 90 req/min
- **TMDB**: 50 req/s (na prática ilimitado)
- **Notion**: 3 req/s

### Notion Database
- **ID**: `22ef090e-a3ca-8047-a25d-c116c21ef2d8`
- Contém: título, capa, sinopse, score, status, temporada, episódios, gêneros, etc.

### Lógica principal
1. Busca lista de animes do MAL via Jikan
2. Enriquece com dados do AniList (score, popularidade)
3. Busca poster/backdrop no TMDB com detecção dinâmica de temporada
4. Upsert no Notion (cria ou atualiza baseado no MAL ID)

### Blacklist
Alguns MAL IDs problemáticos ficam em `BLACKLIST_MAL_IDS` no `main.py`. Adicionar IDs que causam erro ou dados incorretos.

### TODOs abertos
- [ ] Multi-season edge cases (animes com 3+ temporadas no TMDB)
- [ ] OAuth token refresh automático
- [ ] Paginação para listas grandes (>300 animes)

---

## Workflow n8n relacionados

| Workflow | ID | Descrição |
|---|---|---|
| MyAnimeBoxd | `N_l9odG0de5B1LlSXQTdO` | Trigger principal do anime_sync |
| GitHub Scripts Sync | `GOl6dewkyrftw9yU` | Webhook do GitHub → git pull |
| N8N Agent AI | `sepWZYkldB2KcJQSbEQ-V` | Agente principal com MCP |

---

## Como testar localmente (dentro do container)

```bash
# Acessar o container
docker exec -it <container_id> bash

# Rodar um script manualmente
/opt/venv/bin/python3 /home/node/scripts/anime_sync/main.py

# Ver output formatado
/opt/venv/bin/python3 /home/node/scripts/anime_sync/main.py | python3 -m json.tool
```

---

## Deploy de mudanças

```bash
# 1. Edita localmente
code anime_sync/main.py

# 2. Commita e sobe
git add .
git commit -m "fix: descrição da mudança"
git push

# 3. Webhook dispara automaticamente
# n8n roda git pull em /home/node/scripts
# Próxima execução do workflow já usa a versão nova
```

Zero rebuild. Zero redeploy. Zero downtime.

---

## Troubleshooting comum

**Script não atualiza após push**
- Verificar execuções do workflow `GitHub Scripts Sync` no n8n
- Checar se o webhook está cadastrado no GitHub: Repo → Settings → Webhooks

**`ModuleNotFoundError`**
- A lib não está no venv. Instalar via Dockerfile ou manualmente no container para testes.

**`Permission denied` ao rodar o script**
- `chmod +x /home/node/scripts/nome/main.py`
- Ou checar `chown -R 1000:1000 /opt/n8n-scripts` no host

**JSON inválido retornado pro n8n**
- O script está imprimindo algo além do JSON (print de debug, warnings, etc.)
- Redirecionar logs para stderr: `print("debug", file=sys.stderr)`

**Timeout no execSync**
- O timeout padrão nos Code nodes é 30s. Aumentar para scripts lentos:
  `execSync('...', { encoding: 'utf8', timeout: 120000 })`