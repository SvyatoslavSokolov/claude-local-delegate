# claude-local-delegate — установка (по шагам)

Короткий README на русском: как развернуть систему и в какой последовательности.
Полный обзор — в `README.md`, контракт — в `REQUIREMENTS.md`, архитектура — в
`ARCHITECTURE.md`, аналитика — в `docs/ANALYTICS.md`, переезд на другую машину —
в `MIGRATION_AND_USAGE_RU.md`.

**Что это:** MCP-серверы, которые позволяют супервайзеру (Claude Code или Codex)
давать самодостаточную задачу **нативному фоновому агенту `claude --bg`,
работающему на локальной модели** (vLLM), а саму сессию-супервайзер оставить на
обычной (оплачиваемой) модели. Каждая делегация — это реальный, видимый в
`claude agents` session Claude Code с транскриптом на диске.

---

## Шаг 0. Что нужно до установки (требования)

| Требование | Замечание |
|---|---|
| `claude` CLI в `PATH` | Claude Code. Спавн — это реальный процесс `claude --bg`. |
| Python 3 (≥3.8), **только stdlib** | `pip install` не нужен. Любой модуль и любой скрипт `contrib/` работают на стандартной библиотеке. |
| Локальный gateway модели | Anthropic-совместимый HTTP(S)-gateway (напр. vLLM + LiteLLM), достижимый с этой машины. |
| Профиль настроек (settings) | JSON для `--settings`, указывающий Claude Code на gateway. **Содержит секреты — никогда не коммитить.** |
| Персоны (по умолчанию) | `agents/local-worker.md` (writer) и `agents/local-checker.md` (checker для verified-цикла). |

Путь профиля по умолчанию: `~/.claude/vllm.delegate.settings.json`
(фолбэк: `~/.claude/vllm.settings.json`; override: env
`CLAUDE_LOCAL_DELEGATE_SETTINGS`). В нём должны быть явно заданы
`ANTHROPIC_BASE_URL` (http(s) на локальный gateway), `ANTHROPIC_MODEL` и
`ANTHROPIC_API_KEY`.

---

## Шаг 1. Клонировать репозиторий

```bash
git clone https://github.com/SvyatoslavSokolov/claude-local-delegate.git
cd claude-local-delegate
```

Код не меняется по пути (entry points в реестре MCP — **абсолютные** пути к
`server.py` и `code_nav_server.py`), поэтому клонируйте в то место, где хотите
держать его навсегда, либо правьте пути в шаге 4.

---

## Шаг 2. Создать профиль настроек (секреты!)

Положите свой профиль в `~/.claude/vllm.delegate.settings.json` (пример
структуры — в вашем vLLM-стэке). Файл содержит `ANTHROPIC_API_KEY` — **это
секрет**: не коммитить, копировать между машинами только вручную (out-of-band).

Проверьте, что профиль валиден и gateway на месте:

```bash
python3 -c "import sys; sys.path.insert(0,'.'); from local_backend import inspect, profile; \
import os; p=os.path.expanduser('~/.claude/vllm.delegate.settings.json'); \
print(profile(p) and 'profile OK'); print(inspect(p))"
```

---

## Шаг 3. Подготовить персоны (по умолчанию)

Убедитесь, что персоны на месте (в репозитории они не хранятся — это
per-machine файлы в `agents/`):

```bash
ls -la agents/local-worker.md agents/local-checker.md
```

Если их нет — делегации будут работать, но без персоны (graceful degrade).
Создайте их по образцу того, как вы их используете сейчас.

---

## Шаг 4. Зарегистрировать ДВА MCP-сервера (user scope)

В `~/.claude.json` (конфиг user scope Claude Code), блок `mcpServers`, добавьте
**оба** сервера:

```json
{
  "mcpServers": {
    "claude-local-delegate": {
      "command": "python3",
      "args": ["/абсолютный/путь/к/claude-local-delegate/server.py"]
    },
    "code-nav": {
      "command": "python3",
      "args": ["/абсолютный/путь/к/claude-local-delegate/code_nav_server.py"]
    }
  }
}
```

> `code-nav` — только **роутер карты репозитория** (выбирает дочерние карты и
> пути по `docs/design/repository_map.yaml`). Семантическая навигация (символы,
> определения, референсы) — у **Serena** (отдельный MCP), выдаётся read-only
> делегатам. Делегированный агент **не** получает рекурсивный
> `claude-local-delegate` MCP, но сохраняет роутер карты + read-only Serena.

> Для **Codex** есть установщик:
> `python3 contrib/install_codex.py --home <dir>` (сначала без `--apply` —
> dry-run, показывает план).

---

## Шаг 5. (Рекомендуется) Автонаблюдение: hooks + bgIsolation

Две машинные вещи в `~/.claude/settings.json` делают цикл наблюдения
автоматическим:

```json
{
  "worktree": { "bgIsolation": "none" },
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "python3 /home/YOU/.claude/hooks/delegate-pool-status.py",
        "timeout": 25 } ] }
    ]
  }
}
```

- `worktree.bgIsolation: "none"` — **обязательно**, иначе delegate-writer не
  сможет `Write`/`Edit` в основном checkout (Claude Code уводит фоновые
  сессии в worktree).
- Hook на каждый ход запускает `contrib/delegate-pool-status.py` и injectит
  одну строку: сколько делегатов работает, кто `blocked`, и напоминание
  вынести механическую работу через `delegate_to_local`, когда активных <
  `CLAUDE_DELEGATE_TARGET` (по умолчанию 3).

Скопируйте скрипт хука:

```bash
mkdir -p ~/.claude/hooks
cp contrib/delegate-pool-status.py ~/.claude/hooks/
```

Затем один раз откройте `/hooks` (или перезапустите), чтобы watcher подхватил
изменения. `3` — колено throughput/latency на одном GPU vLLM (2→3 = +42%
tok/s, 3→4 = +6%); поднимайте `CLAUDE_DELEGATE_TARGET` только для unattended
batch-задач.

---

## Шаг 6. Перезапустить Claude Code

MCP-процесс запускается при старте сессии — `server.py` **не** перезагружается
на лету. Полностью перезапустите Claude Code (и Codex, если регистрировали).

---

## Шаг 7. Smoke-тест (~5 мин)

```bash
# 1) vLLM/LiteLLM отвечает:
curl -s <ANTHROPIC_BASE_URL>/model/info -H "Authorization: Bearer <KEY>" | head
# 2) MCP подключён (в сессии): инструменты delegate_to_local / project_sync доступны
# 3) минимальная делегация (в Claude-сессии):
#      project_sync(project="$REPO")
#      task_claim(project="$REPO", task_key="smoke-hello", mode="write", paths=["smoke.txt"])
#      delegate_to_local(task="Создай smoke.txt с текстом 'ok'", allowed_tools="Read,Write", task_id=<id>)
#      get_delegate_result(<run_id>, wait_seconds=900)
```

Убедитесь, что `claude agents` показал фонового агента, а результат пришёл.

---

## Шаг 8. Аналитика и качество (закрытый цикл)

После нескольких делегаций:

```bash
python3 contrib/report.py --days 0        # весь отчёт: cohort, trend, health, insights
python3 contrib/report.py --days 0 --csv delegations.csv   # плоская CSV для pandas
```

Одна команда, stdlib, без GUI, без работающего сервера. Подробности и
настраиваемые пороги — в `docs/ANALYTICS.md`.

---

## Чек-лист порядка (кратко)

1. Требования: `claude`, Python3 (stdlib), локальный vLLM gateway.
2. `git clone`.
3. Профиль настроек `~/.claude/vllm.delegate.settings.json` (**секреты**).
4. Персоны `agents/local-{worker,checker}.md`.
5. Два MCP в `~/.claude.json` (`claude-local-delegate` → `server.py`,
   `code-nav` → `code_nav_server.py`).
6. (Рекомендуется) `worktree.bgIsolation:none` + hook `delegate-pool-status.py`.
7. Перезапуск Claude Code / Codex.
8. Smoke-тест.
9. `python3 contrib/report.py` — проверить, что цикл качества видит данные.

---

## Разрешение / секреты

- `ANTHROPIC_API_KEY` живёт **только** в профиле настроек — никогда в git.
- Репо не хранит персоны, профиль настроек, `~/.claude.json` или hooks — всё
  это per-machine и живёт в `~/.claude/`.
- Коммиты без AI-атрибуции (глобальное правило пользователя).