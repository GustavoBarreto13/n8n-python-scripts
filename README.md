# n8n Python Scripts

Scripts Python usados nos workflows do n8n. Cada script é um módulo independente chamado via `spawnSync` em Code nodes, imprime JSON no stdout e retorna para o n8n.

## Scripts

### `anime_sync`

Sincroniza animes "Currently Airing" do Notion com dados de episódios de Jikan/AniList/TMDB. Atualiza metadados da Season page e cria/atualiza páginas de Episode com thumbnails e datas de exibição.

**Trigger**: workflow `MyAnimeBoxd` (`N_l9odG0de5B1LlSXQTdO`)

### `mal_sync`

Delta sync da lista do MyAnimeList → database "Animes" do Notion, baseado em `updated_at`. Gerencia tokens OAuth do MAL com refresh automático.

**Trigger**: workflow `MAL Sync` (`RA5ojUMIbU7Kma6M`)

### `books_sync`

Busca metadados de livros (Google Books com fallback para Open Library) usando ISBN ou Título + Autor e atualiza a página no Notion com propriedades como Author, Pages, Description, Publish Date, ISBN-10 e Cover.

**Trigger**: workflow `GustavoBooks` (`YW1MqVhd6zR-ufOvrxJIR`)

### `series_sync`

Webhook do Notion + Schedule diário → TMDB → cria/atualiza páginas de série, temporadas e episódios no Notion.

**Trigger**: workflow `Series Boxd (Python)` (`QNmGcoi1Oad9s1uW`)

### `ticktick_notion_sync`

Sincronização bidirecional entre TickTick e a database "Tasks" do Notion. Opera em três modos: `tt-to-notion`, `notion-to-tt` e `bootstrap`.

**Trigger**: projeto n8n `FoPcj3MGlsL6FVB5`

### `nami_finance_agent`

Extrai transações financeiras de texto ou áudio via API do Gemini (incorporando a personalidade da Nami de One Piece) e registra na database "Transações" do Notion.

**Trigger**: workflow `Nami Finance Agent` (`UyRklhswVdxJwy22`)

### `gustavoboxd`

Dois modos no mesmo workflow (`0CvDslbxVNVeg0uv`):
- **Webhook** (`--page-id`): enriquece uma página de filme no Notion com metadados do OMDB (diretor, gênero, poster) e backdrop do TMDB, disparado pela automação do Notion ao criar uma página.
- **Sync diário** (`--yesterday`): busca o RSS do Letterboxd, filtra filmes assistidos ontem e cria/atualiza páginas no Notion. Roda às 08:00 via Schedule Trigger.

**Trigger**: workflow `GustavoBoxd — Webhook (OMDB)` (`0CvDslbxVNVeg0uv`) — Notion Webhook + Schedule (08:00)

### `lucy_email_agent`

Processa emails do Gmail via IMAP, classifica com Gemini 2.0 Flash incorporando a personalidade da Lucy (Cyberpunk Edgerunners), aplica labels e envia digest diário no Telegram.

**Trigger**: workflow `Lucy Gmail Digest` (`FXeSRs23jMJvIUuj`) — Schedule 08:00

### `spotify_sync`

Sincroniza o histórico de reproduções do Spotify para uma tabela particionada no BigQuery. Dois modos:
- **Polling horário** via API `/me/player/recently-played` → MERGE no BigQuery em `(ts, spotify_track_uri)`.
- **Import histórico** (`--import-history`) via JSONs do GDPR (Extended Streaming History) — carga única com `WRITE_TRUNCATE`.

**Trigger**: workflow `Spotify History Sync` (`KftB75QdQHXbIzgj`) — Schedule 1h

### `youtube_history_sync` *(planejado)*

Mesma arquitetura do `spotify_sync` para histórico do YouTube via Google Takeout. Sem polling — a API do YouTube não expõe watch history. Plano detalhado em [`youtube_history_sync/PLAN.md`](youtube_history_sync/PLAN.md).

---

## Estrutura

```
n8n-python-scripts/
├── requirements.txt
├── anime_sync/
│   └── main.py
├── books_sync/
│   └── main.py
├── mal_sync/
│   └── main.py
├── series_sync/
│   └── main.py
├── ticktick_notion_sync/
│   ├── main.py
│   └── get_token.py            ← gera tokens OAuth do TickTick localmente
├── nami_finance_agent/
│   └── main.py
├── gustavoboxd/
│   └── main.py
├── lucy_email_agent/
│   └── main.py
├── spotify_sync/
│   ├── main.py
│   └── get_token.py            ← gera refresh_token OAuth do Spotify localmente
└── youtube_history_sync/
    └── PLAN.md                 ← a implementar
```

Cada pasta tem seu próprio `CLAUDE.md` com contexto detalhado do módulo.

---

## Deploy

Push no GitHub → webhook → n8n roda `git pull` automaticamente em `/home/node/scripts`. Sem rebuild, sem redeploy.

```bash
git add .
git commit -m "fix: descrição"
git push
```

Para detalhes de infraestrutura, padrões de código e troubleshooting, ver [CLAUDE.md](CLAUDE.md).
