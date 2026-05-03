# CLAUDE.md — n8n Python Scripts

Guia de contexto para o Claude Code trabalhar neste repositório.
Atualize este documento com qualquer informação relevante.

---

## O que é este repo

Scripts Python chamados pelo n8n via `execSync` em Code nodes. Cada script é um módulo independente que executa uma tarefa específica e retorna JSON via stdout.

O n8n não executa os scripts diretamente como subprocesso — ele usa o `child_process` do Node.js pra chamar o Python.

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
| `/opt/n8n-python-scripts` | `/home/node/scripts` |
| `/root/.ssh` | `/home/node/.ssh` |

Os scripts ficam no host em `/opt/n8n-scripts` e são montados no container em `/home/node/scripts`. Editar no host = reflete imediatamente no container, sem rebuild.

### Sync automático
Push no GitHub → webhook → n8n workflow `GitHub Scripts Sync` (ID: `GOl6dewkyrftw9yU`) → `git reset --hard HEAD && git pull` em `/home/node/scripts`.

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

O script Python deve sempre retornar JSON válido via `print()` no stdout. Erros devem ser capturados e retornados como JSON também.

### Scripts de longa duração (> 30s)

O n8n task runner aborta tasks que ficam mais de 30s sem heartbeat com:
> `Task execution aborted because runner became unresponsive`

**Solução**: adicionar no Dokploy (env vars do container):
```
N8N_RUNNERS_HEARTBEAT_INTERVAL=60
```

Para scripts muito longos (bootstrap com centenas de tasks), use 120.

**Regra geral**:
- Syncs delta regulares (~5min): terminam em < 10s, sem problema
- Bootstrap / full sync: configurar `N8N_RUNNERS_HEARTBEAT_INTERVAL=120` no Dokploy

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

Logs de debug devem ir para stderr: `print("debug", file=sys.stderr)`

---

## Variáveis de ambiente disponíveis no container

```python
import os

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")        # via OPENAPI_MCP_HEADERS
N8N_HOST     = os.environ.get("N8N_HOST")            # n8n.gusstavo42-vps.cloud
GOOGLE_BOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY")  # para books_sync — configurar no Dokploy
TMDB_TOKEN         = os.environ.get("TMDB_TOKEN")          # para series_sync — Bearer token TMDB v4
```

Credenciais de APIs externas devem ser passadas como argumentos pelo n8n ou lidas de env vars configuradas no Dokploy.

---

## Estrutura de pastas

```
n8n-python-scripts/
├── CLAUDE.md                        ← este arquivo
├── requirements.txt                 ← libs Python compartilhadas
├── anime_sync/
│   ├── main.py
│   └── CLAUDE.md                    ← contexto específico do anime_sync
├── mal_sync/
│   ├── main.py
│   ├── CLAUDE.md                    ← contexto específico do mal_sync
│   └── .last_sync.json              ← runtime, gitignored
├── ticktick_notion_sync/
│   ├── main.py
│   ├── get_token.py
│   ├── CLAUDE.md                    ← contexto específico do ticktick_notion_sync
│   ├── .last_sync.json              ← runtime, gitignored
│   └── .sync_cache.json             ← runtime, gitignored
├── nami_finance_agent/
│   ├── main.py
│   └── CLAUDE.md                    ← contexto específico do nami_finance_agent
├── books_sync/
│   ├── main.py
│   └── CLAUDE.md                    ← contexto específico do books_sync
├── series_sync/
│   ├── main.py
│   └── CLAUDE.md                    ← contexto específico do series_sync
└── lucy_digest/                     ← futuro
    └── main.py
```

---

## Workflows n8n relacionados

| Workflow | ID | Descrição |
|---|---|---|
| MyAnimeBoxd | `N_l9odG0de5B1LlSXQTdO` | Trigger principal do anime_sync |
| MAL Sync | `RA5ojUMIbU7Kma6M` | Trigger do mal_sync (Schedule → Code node) |
| GitHub Scripts Sync | `GOl6dewkyrftw9yU` | Webhook do GitHub → git pull |
| N8N Agent AI | `sepWZYkldB2KcJQSbEQ-V` | Agente principal com MCP |
| TickTick↔Notion Sync | `FoPcj3MGlsL6FVB5` | Projeto com os 3 workflows de sync |
| Nami Finance Agent | `UyRklhswVdxJwy22` | Telegram → Gemini → Notion 💰 Transações |
| GustavoBooks | `YW1MqVhd6zR-ufOvrxJIR` | Webhook do Notion → busca Google Books/Open Library → atualiza página com metadados do livro |
| Series Boxd | `Wqa6x53ZRkpI7bw6` | Webhook/Schedule → series_sync/main.py → TMDB → atualiza série/temporadas/episódios no Notion |

---

## Deploy de mudanças

```bash
git add .
git commit -m "fix: descrição da mudança"
git push
# webhook dispara automaticamente → git pull no VPS
# próxima execução do workflow já usa a versão nova
```

Zero rebuild. Zero redeploy. Zero downtime.

---

## Como testar localmente (dentro do container)

```bash
docker exec -it <container_id> bash
/opt/venv/bin/python3 /home/node/scripts/anime_sync/main.py | python3 -m json.tool
```

---

## Troubleshooting comum

**Script não atualiza após push**
- Verificar execuções do workflow `GitHub Scripts Sync` no n8n
- Checar se o webhook está cadastrado no GitHub: Repo → Settings → Webhooks

**`ModuleNotFoundError`**
- A lib não está no venv. Instalar via Dockerfile ou manualmente no container para testes.

**`Permission denied` ao rodar o script**
- `chmod +x /home/node/scripts/nome/main.py`
- Ou `chown -R 1000:1000 /opt/n8n-scripts` no host

**JSON inválido retornado pro n8n**
- O script está imprimindo algo além do JSON — redirecionar logs para stderr.

**Timeout no execSync**
- Aumentar: `execSync('...', { encoding: 'utf8', timeout: 120000 })`
