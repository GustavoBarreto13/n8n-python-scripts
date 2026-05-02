#!/usr/bin/env python3
"""Debug: mostra todas as tasks do TickTick que têm items[] (checklist)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from ticktick_notion_sync.main import _load_dotenv, TickTickAuth, TickTickClient

_load_dotenv()

state_file = Path(__file__).parent / ".last_sync.json"
state = json.loads(state_file.read_text())

auth = TickTickAuth(state=state)
tt = TickTickClient(auth)

projects = tt.list_projects()
found = 0
for p in projects:
    data = tt.get_project_data(p["id"])
    if not data:
        continue
    for task in data.get("tasks") or []:
        items = task.get("items")
        if items:
            found += 1
            print(json.dumps({
                "project": p.get("name"),
                "title": task.get("title"),
                "id": task.get("id"),
                "items": items,
            }, ensure_ascii=False, indent=2))

if not found:
    print("Nenhuma task com items[] encontrada.")
