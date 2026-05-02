# CLAUDE.md — ticktick_notion_sync

Contexto específico do script `ticktick_notion_sync`. Veja o `CLAUDE.md` na raiz para infra, padrões de retorno e deploy.

---

## O que faz

Sincronização bidirecional entre TickTick e a database "Tasks" do Notion. Um único `main.py` opera em três modos via flags:

- `--direction tt-to-notion` — espelha mudanças do TickTick → Notion (Schedule a cada 5min)
- `--direction notion-to-tt` — espelha mudanças do Notion → TickTick (Schedule a cada 5min, com offset)
- `--bootstrap` — match por título e popula `TickTick_ID` em tasks que já existem dos dois lados

TickTick não tem node nativo no n8n: o Python conversa direto com a Open API via OAuth 2.0.

---

## Entry point

```bash
/opt/venv/bin/python3 /home/node/scripts/ticktick_notion_sync/main.py --direction tt-to-notion
/opt/venv/bin/python3 /home/node/scripts/ticktick_notion_sync/main.py --direction notion-to-tt
/opt/venv/bin/python3 /home/node/scripts/ticktick_notion_sync/main.py --bootstrap
```

Chamado pelos workflows do projeto n8n `FoPcj3MGlsL6FVB5` via `execSync`.

---

## Modos de execução

```bash
# Sync regular: TT → Notion (delta desde last_sync_tt_to_notion)
python3 main.py --direction tt-to-notion

# Sync regular: Notion → TT (delta desde last_sync_notion_to_tt)
python3 main.py --direction notion-to-tt

# Bootstrap: match por título nas tasks existentes; cria as faltantes nos dois lados
python3 main.py --bootstrap

# Full: ignora last_sync_time
python3 main.py --direction tt-to-notion --full

# Dry-run: loga, não escreve
python3 main.py --direction tt-to-notion --dry-run

# Verbose
python3 main.py --direction tt-to-notion -v
```

---

## State files

Dois arquivos em `ticktick_notion_sync/`, ambos gitignored e com escrita atômica (tmp + rename).

### `.last_sync.json` — tokens OAuth + watermarks

```json
{
  "ticktick_access_token": "...",
  "ticktick_refresh_token": "...",
  "ticktick_expires_at": "2026-10-23T13:44:00+00:00",
  "last_sync_tt_to_notion": "2026-04-26T13:44:00+00:00",
  "last_sync_notion_to_tt": "2026-04-26T13:44:00+00:00"
}
```

**Crítico:** se o TickTick rotacionar `refresh_token`, o novo é persistido imediatamente. Perder esse arquivo = re-autenticar via `get_token.py`.

### `.sync_cache.json` — loop prevention

Cache de timestamps por entrada. Se sumir, a próxima rodada apenas re-sincroniza tudo (idempotente). Schema:

```json
{
  "<ticktick_id>": {
    "ticktick_modified": "2026-04-26T13:44:00.000+0000",
    "notion_modified":   "2026-04-26T13:44:30.000Z",
    "last_synced_by":    "ticktick" | "notion" | "bootstrap",
    "last_synced_at":    "2026-04-26T13:44:35+00:00"
  },
  ...
}
```

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `TICKTICK_CLIENT_ID` | Sim | App TickTick — Client ID |
| `TICKTICK_CLIENT_SECRET` | Sim | App TickTick — Client Secret |
| `TICKTICK_ACCESS_TOKEN` | Sim* | Access token gerado pelo `get_token.py` — alternativa ao refresh flow |
| `TICKTICK_REFRESH_TOKEN` | Sim* | Refresh token (opcional se ACCESS_TOKEN ainda for válido) |
| `TICKTICK_EXPIRES_AT` | Não | ISO 8601 da expiração do access token (opcional) |
| `NOTION_TOKEN` | Sim | Bearer token da integration do Notion |
| `NOTION_DB_TASKS` | Não | Override do DB ID (default: `17ef090ea3ca81829678e3ca4b82a8a6`) |
| `OPENAPI_MCP_HEADERS` | Fallback | Usado pra extrair `NOTION_TOKEN` se não estiver direto |

\* Pelo menos um dos dois (`TICKTICK_ACCESS_TOKEN` ou `TICKTICK_REFRESH_TOKEN`) é obrigatório. O TickTick nem sempre retorna `refresh_token` no fluxo OAuth — nesse caso use `TICKTICK_ACCESS_TOKEN` diretamente (token tem vida longa, ~6 meses).

---

## APIs

| API | Endpoint base | Auth | Rate limit |
|---|---|---|---|
| TickTick OAuth | `https://ticktick.com/oauth/token` | client_id + client_secret + refresh_token | — |
| TickTick Open API | `https://api.ticktick.com/open/v1` | Bearer access_token | 0.2s entre chamadas |
| Notion | `https://api.notion.com/v1` | Bearer `NOTION_TOKEN` | 0.4s entre chamadas |

### Endpoints TickTick relevantes

| Método | Path | Uso |
|---|---|---|
| GET | `/project` | Lista projetos |
| GET | `/project/{id}/data` | Tasks ativas + colunas do projeto |
| GET | `/project/{id}/task/{tid}` | Task individual (retorna mesmo se completada) |
| POST | `/task` | Cria task |
| POST | `/task/{id}` | Atualiza task |
| POST | `/project/{pid}/task/{tid}/complete` | Completa task |

A API do TickTick **só retorna tasks ativas** (status=0) no `/project/{id}/data`. Tasks completadas não aparecem — use `GET /project/{id}/task/{tid}` para buscar individualmente.

### Endpoints Notion relevantes (Blocks API)

| Método | Path | Uso |
|---|---|---|
| GET | `/blocks/{page_id}/children` | Lista blocos do corpo da página |
| PATCH | `/blocks/{page_id}/children` | **Appenda** blocos ao corpo da página (**PATCH**, não POST) |
| DELETE | `/blocks/{block_id}` | Deleta um bloco |

**Atenção**: a Blocks API usa **PATCH** para append, diferente do resto da API Notion que usa POST. Usar POST retorna `400 invalid_request_url`.

---

## Mapeamento ticktick_id ↔ notion_page_id

A Data Table do n8n **não tem REST API pública**. Em vez dela, o mapping mora no próprio Notion como duas propriedades adicionadas ao Tasks DB:

- `TickTick_ID` (rich_text)
- `TickTick_Project_ID` (rich_text)

Lookup TT→Notion: `query` com `filter: TickTick_ID equals <id>`. Lookup Notion→TT: lê a propriedade direto da página.

A Data Table `aXp1FwU02oMI52Pr` que existia no n8n não é mais usada — pode ser arquivada quando o sync estiver estável.

---

## Field mapping

### Task fields

| TickTick | Direção | Notion (property) | Notas |
|---|---|---|---|
| `title` | ↔ | `Name` (title) | Direto |
| `id` | TT→N | `TickTick_ID` (rich_text) | Escrito no create do Notion e no bootstrap |
| `projectId` | TT→N | `TickTick_Project_ID` (rich_text) | Escrito no create do Notion |
| `projectId` | TT→N | `Tags` (relation) | Via `PROJECT_TAG_MAP` |
| `tags[]` | ↔ | `Labels` (multi_select) | Notion auto-cria opções faltantes |
| `priority` | ↔ | `Priority` (select) | Ver tabela abaixo |
| `dueDate` | ↔ | `Due` (date) | ISO 8601 |
| `isAllDay` | ↔ | (controla formato do `Due`) | TT `isAllDay=true` → Notion grava date-only (sem hora). Notion date-only → TT recebe `isAllDay=true` + `T00:00:00+0000` |
| `content` / `desc` | ↔ | `Description` (rich_text) | Truncado em 2000 chars |
| `items[]` | ↔ | to-do blocks (page body) | `status` 0/1/2 ↔ `checked`; sortOrder ↔ índice × 1000 |
| `status=2` | TT→N | `Status="Done"` + `Completed=hoje` | Task desaparece do scan; detectada via GET individual |
| `status=0` | TT→N | (preserva valor existente) | No update; em create vira `To Do` |

### Status mapping

`Status` no Notion é tipo **`status`** (não `select`). Payload usa `{"status": {"name": "Done"}}`.

| TickTick | Notion (TT→Notion) |
|---|---|
| `status=0` (active) | `To Do` no create; **preserva** valor existente no update (Backlog/Doing/Homolog/Blocked) |
| `status=2` (completed) | `Done` + `Completed = data hoje (BRT)` |

| Notion (Notion→TT) | TickTick |
|---|---|
| `Done` | POST `/project/{pid}/task/{tid}/complete` |
| Qualquer outro status (Backlog/Blocked/To Do/Doing/Homolog) | **Não propaga** — TickTick não tem esses estados refinados |

### Priority mapping

| TickTick | Notion |
|---|---|
| 0 (none) | `🧀Medium` (default) |
| 1 (low) | `🧊Low` |
| 3 (medium) | `🧀Medium` |
| 5 (high) | `🚨High` |

| Notion | TickTick |
|---|---|
| `💡Idea` | 0 |
| `🧊Low` | 1 |
| `🧀Medium` | 3 |
| `🚨High` | 5 |
| `☠️Mandatory` | 5 |

### Project ↔ Tag mapping (hardcoded)

Em `main.py` como `PROJECT_TAG_MAP` e `TAG_PROJECT_MAP`.

| TickTick Project | TickTick Project ID | Notion Tag | Tag Page ID |
|---|---|---|---|
| 🎥Art - Hobbies | `69e7f75bebd7ba0000000170` | Art / Hobbies | `249f090e-a3ca-801f-9900-e41c1b4d9aa1` |
| 🧠Knowledge | `69e7f722ebd7ba0000000158` | Knowledge | `17ef090e-a3ca-817b-83e4-d2646ea5bd5c` |
| 💪Health | `69e7f6fcebd7ba0000000136` | Saúde | `18ef090e-a3ca-80c2-8d7f-dddc3941cfab` |
| 🤑Finances | `69e7f6d4ebd7ba0000000128` | Finanças | `249f090e-a3ca-8007-9533-f30ff0c10a7c` |
| 🙅Personal - Social | `69e7f665ebd7ba00000000d2` | Pessoal | `18ef090e-a3ca-8091-816f-fcd510922d01` |
| 🛒Lista de Compras | `688fa749ebcebb000000056c` | Lista de Compras | `34bf090e-a3ca-81fe-b23b-f315e52e650a` |
| 🎂 Aniversários | `69e6693a8f08e6e745ec0032` | Pessoal | `18ef090e-a3ca-8091-816f-fcd510922d01` |
| 💼Work | `69f3cac2ebd7f6000000034e` | Work | `18ef090e-a3ca-80de-8ea8-ca9712491d19` |
| 🏐Misc | `6785ccb7c71c710000000221` | *(sem tag)* | — |
| Inbox | `inbox` | *(sem tag)* | — |

**Reverse lookup** (Notion → TickTick): a tag "Pessoal" mapeia pra `🙅Personal - Social` (não pra Aniversários, que é um subset).

**Projetos ignorados** (não sincronizados):
- 📅Google Calendar (`6861fbd9dca24397f378985b`)
- 🗒Notion (`6840ec738f0872956c61e890`)

Projeto novo no TickTick que não está no `PROJECT_TAG_MAP` → log warning e skip. Adicionar manualmente.

---

## Loop prevention

Toda sync passa por `should_skip_sync(cache_entry, direction, source_modified)`:

1. Se `cache.<source>_modified == source.modified` → skip ("unchanged").
2. Se `cache.last_synced_by == lado_oposto` E `(now - last_synced_at) < 10 min` → skip ("recent-echo").

Sem isso, um update em A → escrita em B → trigger de update em A → loop infinito.

**Após Notion→TT update**: faz um `GET /project/{pid}/task/{tid}` para confirmar o `modifiedTime` real antes de salvar no cache. O `update_task` pode retornar um `modifiedTime` diferente do que o próximo scan vai ver, o que quebraria o check "unchanged".

---

## Saída (stdout)

JSON em uma única linha. Logs vão pro stderr.

### `--direction tt-to-notion` / `--direction notion-to-tt`

```json
{
  "ok": true,
  "mode": "tt-to-notion",
  "dry_run": false,
  "timestamp": "2026-04-26T13:44:00+00:00",
  "scanned": 42,
  "created": 3,
  "updated": 5,
  "completed": 1,
  "skipped": 34,
  "errors": []
}
```

`tt-to-notion` inclui `completed: <int>` (tasks marcadas Done no Notion por terem sumido do scan TT).
`notion-to-tt` também inclui `completed: <int>` (tasks marcadas Done no TT).

### `--bootstrap`

```json
{
  "ok": true,
  "mode": "bootstrap",
  "dry_run": false,
  "timestamp": "...",
  "matched": 12,
  "created_in_notion": 4,
  "created_in_tt": 7,
  "errors": []
}
```

`ok: false` quando `errors` não está vazio. Exit sempre 0 — n8n parseia o campo `ok`.

---

## Code nodes n8n

### TickTick → Notion (Schedule a cada 5min)

```javascript
const { execSync } = require('child_process');
const result = execSync(
  '/opt/venv/bin/python3 /home/node/scripts/ticktick_notion_sync/main.py --direction tt-to-notion',
  { encoding: 'utf8', timeout: 180000 }
);
return [{ json: JSON.parse(result.trim().split('\n').pop()) }];
```

### Notion → TickTick (Schedule a cada 5min, offset 2min30)

```javascript
const result = execSync(
  '/opt/venv/bin/python3 /home/node/scripts/ticktick_notion_sync/main.py --direction notion-to-tt',
  { encoding: 'utf8', timeout: 180000 }
);
return [{ json: JSON.parse(result.trim().split('\n').pop()) }];
```

### Bootstrap (Manual Trigger, 1x)

```javascript
const result = execSync(
  '/opt/venv/bin/python3 /home/node/scripts/ticktick_notion_sync/main.py --bootstrap',
  { encoding: 'utf8', timeout: 600000 }
);
return [{ json: JSON.parse(result.trim().split('\n').pop()) }];
```

---

## Bootstrap — passo a passo

1. Registrar app no TickTick em https://developer.ticktick.com/manage com `OAuth Redirect URL = http://localhost:8888/callback`.
2. Adicionar `TICKTICK_CLIENT_ID` e `TICKTICK_CLIENT_SECRET` no `.env` da raiz.
3. Localmente: `python3 ticktick_notion_sync/get_token.py` — abre o browser, autoriza, gera `.last_sync.json`.
4. Copiar o `.last_sync.json` pro VPS (`scp` para `/opt/n8n-scripts/ticktick_notion_sync/`).
5. Adicionar `TICKTICK_CLIENT_ID` / `TICKTICK_CLIENT_SECRET` nas env vars do Dokploy.
6. Push do código → webhook `GitHub Scripts Sync` faz `git pull` no VPS.
7. Rodar o workflow Bootstrap **uma vez** (popula `TickTick_ID` em todas as tasks que casam por título).
8. Ativar os dois Schedule Triggers (TT→Notion e Notion→TT) com offset.

---

## Edge cases

1. **Delete não propaga.** Task deletada num lado fica órfã no outro. Limpar manualmente.
2. **Subtasks** (checklist do TickTick): sincronizados como to-do blocks no corpo da página Notion (bidirecional). Subtasks com `parentId` (hierarquia real) não são suportados — só os `items[]` de checklist flat.
3. **Recurring tasks**: configuração não sincroniza. Cada ocorrência individual sincroniza como task normal.
4. **Labels novas**: o Notion auto-cria opções no `Labels` quando recebe um nome novo (multi_select). TickTick também aceita tags novas no payload.
5. **Notion `Tags` relation é limit:1** — sempre uma única relação. Se a página tiver mais de uma, só a primeira é considerada no reverse lookup.
6. **Timezone / all-day**: TickTick manda dueDate UTC com offset (`+0000`). Notion aceita o ISO direto. Para tasks all-day (`isAllDay=true`), o TickTick devolve `dueDate` como `T09:00:00+0000` (= 06:00 BRT) — o sync detecta o flag e grava no Notion como date-only (sem hora). No reverso, Notion date-only vira `isAllDay=true` no TickTick. `Completed` é só data — usamos o dia atual em BRT (UTC-3).
7. **Status no Notion**: tipo `status` (não `select`) — payload usa `{"status": {"name": "Done"}}`, não `{"select": ...}`.
8. **Checklist — to_do blocks vs. outros blocos**: só blocos `type == "to_do"` são gerenciados pelo sync. Parágrafos, imagens e outros tipos no corpo da página nunca são tocados.
9. **Checklist vazio (TT→Notion)**: se `items[]` ficar vazio no TickTick, o próximo sync TT→Notion deleta todos os to_do blocks da página Notion. Por design — checklist fica em sync nos dois lados.
10. **Notion→TT sem blocos**: se a página Notion não tiver to_do blocks, a chave `items` é omitida do payload do TickTick — o checklist existente no TT é preservado. Para limpar o checklist via Notion, delete os to_do blocks manualmente.
11. **TickTick `items[].status`**: valores 0 (unchecked), 1 (completed com timestamp), 2 (checked). O sync trata 1 e 2 como `checked: true` no Notion.
12. **Completed TT→Notion**: tasks completadas no TickTick somem do `/project/{id}/data`. O sync detecta via "missing from scan" → GET individual → se `status=2`, marca Done no Notion. Custo: 1 query Notion + 1 GET TickTick por task ausente não-Done.

---

## TODOs abertos

- [ ] Paginação no TickTick `/project/{id}/data` (a API não documenta, monitorar projetos com >100 tasks)
- [ ] Detectar deletes (task removida) — hoje fica órfã (o "missing from scan" faz GET e se não achar, ignora)
- [x] Sync do conteúdo de subtasks do TickTick — implementado como to_do blocks (items[] flat)
- [x] Completar task no Notion quando completada no TickTick — detectado via "missing from scan" + GET individual
- [ ] Recurring task config sync (provavelmente não factível direto)
- [ ] Notion `Project` relation: hoje não sincroniza com nada do TickTick
