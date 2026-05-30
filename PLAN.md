# Design: Personal Assistant com Google ADK

## Contexto

Os scripts deste repo hoje são pipelines lineares: recebem input, chamam o Gemini uma vez, executam lógica Python, escrevem em Notion/BQ. Funcionam bem para automações batch agendadas, mas não suportam interação conversacional, raciocínio multi-step nem coordenação entre domínios.

Este design introduz o **Google ADK** como camada de agência sobre os scripts existentes — sem reescrever o que funciona, adicionando o que falta.

---

## Princípio central: híbrido batch + agente

Dois modos coexistem:

| Modo | Quando usar | Custo | Implementação |
|---|---|---|---|
| **Batch** | Sync agendado, processamento em volume | ~$0.003/exec | Script Python direto (atual) |
| **Interativo** | Perguntas e ações sob demanda via Telegram | ~$0.002–0.01/query | ADK Agent |

A chave é **não migrar o que já funciona**. O digest diário do Lucy, os syncs de anime/spotify/séries — continuam como estão. O ADK adiciona uma camada de interação em cima.

---

## Arquitetura

```
Telegram (usuário)
    ↓
Coordinator Bot (Python persistente no VPS)
    ├── Nami Agent      → Notion (finanças)
    ├── Lucy Agent      → Gmail IMAP
    ├── Tasks Agent     → TickTick / Notion tasks
    ├── Media Agent     → Notion (séries + filmes + anime)
    ├── Books Agent     → Notion (livros)
    └── knowledge_tool  → Vertex AI RAG corpus (Obsidian vault via Drive)

n8n (continua como hub de automações batch)
    ├── Schedule 8h → lucy/main.py (digest diário, não muda)
    ├── Schedule 8h → coordinator /briefing (morning briefing unificado)
    ├── anime_sync, mal_sync, spotify_sync, series_sync, etc. (não mudam)
    └── GitHub Sync webhook (não muda)

BigQuery
    ├── spotify.streaming_history (já existe)
    ├── media.series_history      (espelho novo)
    ├── media.movies_history      (espelho novo)
    ├── media.anime_history       (espelho novo)
    ├── media.books_history       (espelho novo)
    └── coordinator.action_logs   (novo — observabilidade)
```

---

## Componentes

### Coordinator (novo)

Bot Telegram autônomo com ADK. Roda como processo persistente no Docker.

**Não usa n8n para mensagens interativas** — registra seu próprio webhook Telegram e responde diretamente. N8n ainda chama o coordinator via HTTP para o briefing matinal.

```python
coordinator = Agent(
    name="coordinator",
    model="gemini-2.0-flash",
    instruction="""
        Você é um assistente pessoal. Delegue para os especialistas:
        - Nami: finanças e transações no Notion
        - Lucy: emails e Gmail
        - Tasks: tarefas no TickTick/Notion
        - Media: séries, filmes e anime
        - Books: livros
        Combine agentes quando necessário. Responda em português. Seja direto.
    """,
    sub_agents=[nami_agent, lucy_agent, tasks_agent, media_agent, books_agent]
)
```

**Sessão**: uma por `chat_id` do Telegram, `InMemorySessionService` (memória dentro da sessão, reinicia com o container — aceitável para começar).

**Estrutura de arquivos:**
```
coordinator/
├── main.py          # Telegram bot loop (python-telegram-bot)
├── agent.py         # ADK coordinator + sub_agents
├── Dockerfile
└── CLAUDE.md
```

---

### Padrão de cada agente especialista

Cada agente tem:
- `tools.py` — funções puras extraídas do `main.py` existente
- `agent.py` — definição ADK usando as tools

O `main.py` original **não muda** — continua sendo chamado pelo n8n para batch.

```
nami_finance_agent/
├── main.py        ← não muda (batch via n8n)
├── tools.py       ← NOVO: create_transaction, query_expenses, etc.
└── agent.py       ← NOVO: Agent(tools=[...])

lucy_email_agent/
├── main.py        ← não muda (digest diário via n8n)
├── tools.py       ← NOVO: fetch_emails, label_and_archive, search_emails
└── agent.py       ← NOVO: Agent(tools=[...])
```

---

### Agentes por domínio

**Nami Agent** (finanças)
- Tools: `create_transaction`, `query_expenses`, `update_transaction`, `delete_transaction`
- Fonte: Notion 💰 Transações

**Lucy Agent** (email — interativo)
- Tools: `fetch_emails(filtros)`, `label_and_archive(uid, label)`, `search_emails(query)`, `get_email_body(uid)`
- Fonte: Gmail IMAP
- Nota: o digest batch diário continua em `main.py` — Lucy interativa é complementar

**Tasks Agent** (tarefas)
- Tools: `get_tasks_today`, `create_task`, `complete_task`, `list_overdue`
- Fonte: TickTick API / Notion (reusar lógica do `ticktick_notion_sync`)

**Media Agent** (séries + filmes + anime)
- Tools: `get_watching_now`, `get_watch_history(periodo)`, `mark_episode_watched`, `get_stats`
- Fontes: Notion (estado atual) + BigQuery (histórico analítico)
- Combina dados de `series_sync`, `gustavoboxd`, `anime_sync`

**Books Agent** (livros)
- Tools: `get_current_book`, `get_read_history`, `add_book`, `mark_finished`
- Fontes: Notion + BigQuery mirror

---

### BigQuery mirror

Scripts de sync que hoje escrevem só no Notion passam a escrever também no BQ ao final da execução:

```python
# Adição em series_sync/main.py, gustavoboxd/main.py, etc.
update_notion_page(data)
upsert_bigquery(data, table="media.series_history")  # linha nova
```

**Schemas:**

```sql
media.series_history:    title, status, season, episode, date_watched, rating, genre, source
media.movies_history:    title, year, date_watched, rating, genre, director, source
media.anime_history:     title, status, episode, date_watched, rating, source (MAL)
media.books_history:     title, author, status, date_started, date_finished, pages, rating

coordinator.action_logs: timestamp, user_id, message, agent_called, tool_called,
                         status, latency_ms, tokens_used, cost_usd
```

**Por que BQ além do Notion:**
- Joins cross-domain (tempo em séries vs. livros vs. spotify)
- Queries analíticas rápidas sem rate limit da Notion API
- Histórico imutável de consumo
- Custo praticamente zero para volume pessoal

---

### Morning Briefing

Um endpoint no coordinator chamado pelo n8n às 8h que consulta todos os agentes e sintetiza:

```
💰 Finanças: gastou R$340 ontem. Fatura Nu fecha em 8 dias.
✉️ Emails: 3 prioritários, 1 precisa de resposta hoje.
📋 Tarefas: 4 pendentes hoje, 2 atrasadas.
📺 Mídia: faltam 2 episódios pra terminar Severance S2.
📚 Leitura: 47% de O Problema dos Três Corpos.
```

Esse tipo de síntese só é possível com o coordinator — scripts isolados não conseguem fazer isso.

---

### Knowledge (Obsidian)

Camada de acesso à base de conhecimento pessoal do Obsidian. O coordinator consulta o vault quando o usuário faz perguntas sobre anotações, projetos, referências ou estudos.

#### Primário: Vertex AI RAG (Agent Builder + GCS)

**Pipeline completo:**
```
Obsidian vault (local)
    ↓ já sincronizado (Google Drive)
Google Drive (pasta do vault)
    ↓ fonte nativa — sem GCS, sem script intermediário
Vertex AI Agent Builder — Data Store
    ↓ chunking + embeddings + índice vetorial (automático)
Coordinator (ADK) — knowledge_tool
    ↓ busca semântica por consulta
Resposta sintetizada pelo Gemini
```

O vault já está no Google Drive — isso elimina toda a camada de sync. O Vertex AI Agent Builder suporta Google Drive como fonte nativa: você aponta para a pasta do vault, ele indexa os `.md` e reindexes automaticamente quando os arquivos mudam. Zero código de infraestrutura.

**Integração ADK:**
```python
from google.adk.tools import VertexAiRagRetrieval

knowledge_tool = VertexAiRagRetrieval(
    rag_corpus="projects/seu-projeto/locations/us-central1/ragCorpora/xxx",
    similarity_top_k=5,
)

coordinator = Agent(
    ...
    tools=[..., knowledge_tool],
    sub_agents=[nami_agent, lucy_agent, ...]
)
```

**Estimativa de custos (uso pessoal):**

| Componente | Custo |
|---|---|
| GCS storage (vault ~100MB de texto) | ~US$0,00 (free tier 5GB) |
| Vertex AI Search — indexação | ~US$0,00 (índice pequeno) |
| Vertex AI Search — consultas | US$4,00 / 1.000 queries |
| Gemini 1.5 Flash (geração) | ~US$0,075 / 1M tokens input |

**Total estimado**: ~US$1,20–2,00/mês para ~300 perguntas/mês (10/dia). Principal custo: US$4/1.000 queries do Vertex AI Search.

**Casos de uso:**
```
"O que anotei sobre arquitetura de sistemas?"
→ knowledge_tool busca semanticamente no vault → coordinator sintetiza

"Cria uma tarefa baseada no projeto que planejei em [[Projeto ADK]]"
→ knowledge_tool recupera a nota → Tasks agent cria as subtarefas

"Tenho alguma anotação sobre Vertex AI?"
→ knowledge_tool → lista notas relevantes com trechos
```

#### Estrutura ideal das notas para RAG

A busca semântica do Vertex AI enxerga os arquivos de forma "plana" — pastas não importam. O que importa é o conteúdo interno de cada `.md`.

**1. YAML Frontmatter (metadados estruturados)**
O Vertex AI lê as chaves do frontmatter e pode usá-las como filtros. Padrão a adotar:
```yaml
---
title: Nome descritivo da nota
tags: [categoria, subcategoria]
date: YYYY-MM-DD
status: ativo | concluido | referencia
tipo: resumo | projeto | area | recurso
---
```

**2. Headings para chunking inteligente**
O Vertex quebra o texto em chunks baseado em cabeçalhos. Use `##` e `###` para separar seções — o agente consegue isolar só o bloco relevante para a pergunta.
- Evite: bloco contínuo de texto misturando ideias
- Prefira: `## Conceito`, `## Aplicações`, `## Pontos de Ação`

**3. Notas atômicas**
Um arquivo = um assunto. Notas genéricas como `Anotacoes_Gerais.md` confundem o agente. Para perguntas amplas, o modelo cruza arquivos separados automaticamente.

**4. Títulos de arquivo descritivos**
O nome do `.md` tem peso alto na relevância da busca.
- Evite: `Ideia_1.md`, `Aula_3.md`
- Prefira: `Configuracao_Variaveis_Ambiente_n8n.md`, `Design_Sistema_ADK_Personal_Assistant.md`

**5. Links internos (`[[ ]]`) não são navegados**
O agente lê texto literal — não "clica" em links internos. Cada nota precisa ter contexto suficiente para ser entendida sozinha.

---

#### Plano B: ChromaDB (self-hosted, $0)

Se o custo do Vertex AI Search for relevante, ChromaDB no VPS resolve com zero custo adicional:

```python
def search_knowledge(query: str) -> list[dict]:
    """Busca semanticamente na base de conhecimento pessoal (notas Obsidian)."""
    results = chroma_client.query(query_texts=[query], n_results=5)
    return [{"content": doc, "source": meta["source"]} 
            for doc, meta in zip(results["documents"][0], results["metadatas"][0])]
```

Requer:
1. Script local que sincroniza vault → VPS via rsync
2. Job no VPS que gera embeddings (`text-embedding-004`) e upserta no ChromaDB

A API de busca é idêntica — migrar Vertex → ChromaDB é trocar a implementação de `search_knowledge` sem tocar no coordinator.

---

## Dependências novas

```
google-adk
fastapi
uvicorn
python-telegram-bot
```

Adicionadas ao `coordinator/Dockerfile`. Os scripts existentes não mudam suas dependências.

---

## Migração incremental (ordem recomendada)

### Fase 1 — Coordinator + Nami
- Cria `nami_finance_agent/tools.py` extraindo funções do `main.py`
- Cria `nami_finance_agent/agent.py`
- Cria `coordinator/` com Nami como único sub-agente
- Substitui o workflow Telegram do Nami no n8n pelo coordinator
- **Entrega**: Nami continua funcionando + ganha memória de sessão

### Fase 2 — Lucy interativa
- Cria `lucy_email_agent/tools.py`
- Adiciona Lucy ao coordinator
- O digest batch diário não muda
- **Entrega**: queries sobre email via Telegram ("tem algo urgente?")

### Fase 3 — BQ mirror
- Adiciona `upsert_bigquery()` nos scripts de series/filmes/anime/livros
- **Entrega**: histórico analítico cross-domain disponível

### Fase 4 — Media + Books + Tasks agents
- Cria agents para os três domínios
- Adiciona ao coordinator
- **Entrega**: sistema completo + morning briefing

### Fase 5 — Knowledge (Obsidian)
- Configura sync Obsidian → Google Drive (plugin no Obsidian)
- Cria corpus no Vertex AI RAG apontando para a pasta do Drive
- Adiciona `knowledge_tool` (VertexAiRagRetrieval) ao coordinator
- **Entrega**: coordinator responde perguntas sobre o vault + combina conhecimento com ações

---

## Verificação por fase

**Fase 1:**
```bash
# Testa Nami direto
python nami_finance_agent/agent.py --query "Gastei 89 no Rappi"
# Testa coordinator HTTP
curl -X POST localhost:8080/chat -d '{"user_id":"test","message":"Gastei 50 no mercado"}'
# Verifica sessão (segunda mensagem referencia a primeira)
curl -X POST localhost:8080/chat -d '{"user_id":"test","message":"na verdade era 45"}'
```

**BQ mirror:**
```sql
SELECT * FROM media.series_history ORDER BY date_watched DESC LIMIT 5
```

**Morning briefing:**
```bash
curl -X POST localhost:8080/briefing -d '{"user_id":"seu_chat_id"}'
```

---

## Decisões em aberto (para evolução futura)

- **Persistência de sessão**: InMemorySessionService para começar. Se reinicializações forem problema, migrar para SQLite local ou Firestore.
- **Autenticação do endpoint**: coordinator exposto só internamente no Docker network — sem auth por ora.
- **Gemini model**: `gemini-2.0-flash` para todos os agentes. Pode escalar para Gemini Pro no coordinator se precisar de raciocínio mais complexo.
- **Parallel tool calls**: o ADK suporta, mas requer configuração explícita. Considerar para o morning briefing (todos os agentes em paralelo).
