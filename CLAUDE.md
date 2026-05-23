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

Em Code nodes do n8n, o padrão obrigatório é usar `spawnSync` com `-v` para capturar stderr nos logs visíveis pelo output do n8n:

```javascript
const { spawnSync } = require('child_process');

const proc = spawnSync(
  '/opt/venv/bin/python3',
  ['/home/node/scripts/nome_do_script/main.py', '-v'],
  { encoding: 'utf8', timeout: 60000 }
);

if (proc.error) throw proc.error;

const result = JSON.parse(proc.stdout.trim());
result._logs = proc.stderr.slice(-3000);
return [{ json: result }];
```

**Por que `spawnSync` e não `execSync`:**
- `execSync` descarta o stderr — impossível debugar pelo n8n
- `spawnSync` captura stdout e stderr separadamente
- `result._logs` expõe os últimas 3000 chars dos logs no output do n8n, sem poluir o JSON principal

**Para scripts com argumentos (ex: webhook com page-id):**
```javascript
const { spawnSync } = require('child_process');
const pageId = items[0].json.body.data.id;

const proc = spawnSync(
  '/opt/venv/bin/python3',
  ['/home/node/scripts/nome_do_script/main.py', '--page-id', pageId, '-v'],
  { encoding: 'utf8', timeout: 120000 }
);

if (proc.error) throw proc.error;

const result = JSON.parse(proc.stdout.trim());
result._logs = proc.stderr.slice(-3000);
return [{ json: result }];
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
TMDB_TOKEN         = os.environ.get("TMDB_TOKEN")          # para series_sync e gustavoboxd — Bearer token TMDB v4
OMDB_API_KEY       = os.environ.get("OMDB_API_KEY")        # para gustavoboxd — API key do OMDB
LETTERBOXD_USERNAME = os.environ.get("LETTERBOXD_USERNAME") # para gustavoboxd — username do Letterboxd
NOTION_DB_GUSTAVOBOXD = os.environ.get("NOTION_DB_GUSTAVOBOXD") # para gustavoboxd — ID do banco de filmes
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID")     # para spotify_sync — App do Spotify Developer
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET") # para spotify_sync
SPOTIFY_REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN") # para spotify_sync — obtido via get_token.py
GCP_PROJECT_ID        = os.environ.get("GCP_PROJECT_ID")        # para spotify_sync — projeto BigQuery
BQ_DATASET            = os.environ.get("BQ_DATASET")            # para spotify_sync — dataset (ex: "spotify")
BQ_TABLE              = os.environ.get("BQ_TABLE")              # para spotify_sync — tabela (ex: "streaming_history")
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")  # path do service account JSON
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
├── gustavoboxd/
│   ├── main.py
│   └── CLAUDE.md                    ← contexto específico do gustavoboxd
├── spotify_sync/
│   ├── main.py
│   ├── get_token.py
│   ├── CLAUDE.md                    ← contexto específico do spotify_sync
│   ├── .gcp-sa.json                 ← service account GCP, gitignored
│   └── .last_sync.json              ← runtime, gitignored
├── lucy_email_agent/
│   ├── main.py
│   └── CLAUDE.md                    ← contexto específico do lucy_email_agent
└── youtube_history_sync/
    └── PLAN.md                      ← carga histórica YouTube via Takeout → BigQuery (a implementar)
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
| Series Boxd (Python) | `QNmGcoi1Oad9s1uW` | Webhook/Schedule → series_sync/main.py → TMDB → atualiza série/temporadas/episódios no Notion |
| GustavoBoxd — Webhook (OMDB) | `0CvDslbxVNVeg0uv` | Webhook Notion + Schedule diário (08h) → gustavoboxd/main.py → OMDB/TMDB/Letterboxd RSS → Notion |
| Lucy Email Agent | `FXeSRs23jMJvIUuj` | Schedule 8h → Gmail IMAP → Gemini classifica e gera digest no Telegram |
| Spotify History Sync | `KftB75QdQHXbIzgj` | Schedule a cada 1h → spotify_sync/main.py → Spotify API → BigQuery (MERGE) |

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
- Com `spawnSync`, stdout e stderr são separados automaticamente — não há risco de mistura.

**Timeout**
- Aumentar o timeout: `spawnSync(..., { encoding: 'utf8', timeout: 120000 })`

**Logs não aparecem no `_logs`**
- Verificar se o script usa `setup_logging()` que direciona para stderr.
- Verificar se o Code node usa `-v` nos args do `spawnSync`.

---

## Documentação no Obsidian

A documentação detalhada, arquitetura e registros de decisões deste projeto estão armazenados no vault do Obsidian.
Sempre que você criar, modificar ou refatorar algo significativo neste repositório (novos scripts, mudanças de infraestrutura, novos fluxos no n8n), **é obrigatório** refletir essas atualizações na documentação do Obsidian.
* **Como acessar**: Utilize a skill `obsidian-vault` para consultar os caminhos corretos e editar os arquivos no seu vault do Obsidian
* **Diretriz**: O código e a base de conhecimento no Obsidian devem evoluir juntos. Nunca faça uma alteração grande sem também atualizar a documentação lá.
