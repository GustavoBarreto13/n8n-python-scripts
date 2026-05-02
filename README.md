# n8n Python Scripts

Scripts Python usados nos workflows do n8n. Cada script é um módulo independente chamado via `execSync` em Code nodes, imprime JSON no stdout e retorna para o n8n.

## Scripts

### `anime_sync`

Sincroniza animes "Currently Airing" do Notion com dados de episódios de Jikan/AniList/TMDB. Atualiza metadados da Season page e cria/atualiza páginas de Episode com thumbnails e datas de exibição.

**Trigger**: workflow `MyAnimeBoxd` (`N_l9odG0de5B1LlSXQTdO`)

### `mal_sync`

Delta sync da lista do MyAnimeList → database "Animes" do Notion, baseado em `updated_at`. Gerencia tokens OAuth do MAL com refresh automático.

**Trigger**: workflow `MAL Sync` (`RA5ojUMIbU7Kma6M`)

### `ticktick_notion_sync`

Sincronização bidirecional entre TickTick e a database "Tasks" do Notion. Opera em três modos: `tt-to-notion`, `notion-to-tt` e `bootstrap`.

**Trigger**: projeto n8n `FoPcj3MGlsL6FVB5`

### `lucy_digest` *(em desenvolvimento)*

Processa emails do Gmail com a personalidade da Lucy (Cyberpunk Edgerunners) e envia digest diário pro Telegram.

---

## Estrutura

```
n8n-python-scripts/
├── requirements.txt
├── anime_sync/
│   └── main.py
├── mal_sync/
│   └── main.py
├── ticktick_notion_sync/
│   ├── main.py
│   └── get_token.py        ← gera tokens OAuth do TickTick localmente
└── lucy_digest/
    └── main.py
```

---

## Deploy

Push no GitHub → webhook → n8n roda `git pull` automaticamente em `/home/node/scripts`. Sem rebuild, sem redeploy.

```bash
git add .
git commit -m "fix: descrição"
git push
```

Para detalhes de infraestrutura, padrões de código e troubleshooting, ver [CLAUDE.md](CLAUDE.md).
