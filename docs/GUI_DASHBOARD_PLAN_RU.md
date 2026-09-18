# План: GUI-дашборд оркестрации + приоритеты + качество

Дата: 2026-09-13. Статус: план, согласование. Rev.2 — добавлены
deep-research по готовым MCP/GUI и аудит замены самописных частей
(разделы 0б, 0в).

## 0в. Deep research: «а может, это уже сделано другим MCP/GUI?»

Исследовано (2026-09, GitHub/PyPI/README, ссылки в отчётах агентов). **Вывод:
подготовленного «взять и подключить» решения под ваш случай нет — и это
подтверждает план, а не опровергает его.**

| Проект (★, лицензия) | Что | Прогресс | Приоритет | Качество |
|---|---|---|---|---|
| Vibe Kanban (28k, Apache-2.0) | board для 10+ агентов, Claude Code первым | ✅ (diff/PR) | ✅ | ❌ |
| Claude-Code-Agent-Monitor (1k, MIT, React/SQLite/WS) | real-time дашборд Claude Code+Codex, Kanban, алерты | ✅ | ❌ | частичный |
| claudecodeui (13.6k, AGPL) | web-chat обёртка (сессии, файл-дерево, чат) | частично | ❌ | ❌ |
| claude-squad (8.4k, AGPL) | TUI + tmux + git-worktree | ❌ | ❌ | ❌ |
| LangGraph/AutoGen/CrewAI Studio | студия **внутри своего** фреймворка | ✅ | ❌ | ✅ (traces) |
| MCP kanban-серверы (1–78★) | задачи/приоритеты | частично | ✅ | ❌ |

Ключевое: **ни один поддерживаемый OSS-проект не даёт в одном UI** «ручное
редактирование приоритета живого multi-agent ранa + метрики качества
оркестрации» — везде пропущена ваша основная фича (качество) или
приоритет-редактор, ведущий в живой ран. Vibe Kanban ближайший, но
**README: проект sunsetting** → брать нельзя. Все «студии» требуют переписать
оркестрацию в свой фреймворк (та же ловушка, что с MAF). → Строить тонкий слой
на своих данных — единственное верное решение (см. раздел 2).

## 0. Вопрос «а нужен ли microsoft/agent-framework»

## 0. Вопрос «а нужен ли microsoft/agent-framework»

**Краткий ответ: нет, как ядро он вам не подходит. Идея DevUI/OTel — можно почитать, код — нет.**

Fakты (проверено по репозиторию, PyPI, докам Microsoft, 2026-09):

1. MAF (Python 1.18.0, MIT, ~еженедельные релизы) — это фреймворк
   *построения* агентных приложений: свой `Agent` (обёртка над чат-клиентом +
   провайдер), workflow-граф (executors/edges/supersteps), оркестрации
   (sequential/concurrent/handoff/group-chat/Magentic), чекпоинты, OpenTelemetry.
   Он выражает агент как *свой* объект с *своим* провайдером (OpenAI/Anthropic/
   Gemini/Ollama/...).
2. Ваша система — не «агентное приложение в фреймворке», а **делегирование поверх
   нативных сессий Claude Code** (`claude --bg` со своим `--settings`/`--tools`).
   MAF не может выразить сущность «нативный `claude --bg` agent»: это не чат-
   клиент в его модели, а отдельный процесс с транскриптом на диске. Встроить
   MAF — значит переписать модель исполнения (workers, fan-out, verified loop,
   координирующую доску) под его workflow-граф. Это замена ядра ради ядра.
3. Всё, что в MAF *похоже* на ваши три фичи:
   - **DevUI** (`agent-framework-devui --pre`): web-UI + трассировщик OTel,
     «explicitly a sample app, not intended for production use». И это UI *для
     прогона MAF-агентов*, а не для доски задач. Под вашу `coordination.sqlite3`
     не подключается — несовместимая модель.
   - **Чекпоинты/резюм** (`FileCheckpointStorage` и т.п.) — относятся к его
     workflow-инстансам; у вас аналог уже есть (append-only `events` + `runs`),
     и он лучше вписан в вашу модель.
   - **505 открытых issues**, известные дефекты чекпоинтов (#8181 «saves state
     it cannot restore», #8201, #8160) — пост-1.0, API ещё ломается.
4. HN/сообщество: «come back in a few years», «anything beyond a while-loop is
   a play to trap you into an ecosystem». Для вашей задачи (уже работающего
   стека, где ядро — оркестратор-модель + MCP + sqlite) экосистемный захват —
   именно то, чего вы не хотите.

**Вывод:** MAF добавит только неудобства (новый абстракционный слой, зависимость,
конвергенция вашей модели под чужую) и не даст ни одной из ваших трёх фич
готовой. Ваши три фичи (прогресс, приоритеты, качество) строятся на **уже
существующих данных** в `~/.claude-local-delegate/` — им нужен *тонкий слой
визуализации и управления*, а не фреймворк. Ориентир по MAF — только как
референс того, как они решили UI (SPA + polling/WS + OTel-таймлайн).

## 0б. Аудит: что из самописного заменить готовым (аналог «до Serena»)

Research + чтение кода дали три кандидата. Решения:

**🔴 ЗАМЕНИТЬ (высокая ценность, низкий риск) — 4 дубля transcript-JSONL.**
Тот самый «до-Serena» паттерн: одна и та же логика (стриминг JSONL + dedup
usage по `message.id` + dedup tool_use) написана **четыре** раза:
- `server.py:_iter_events/_transcript_summary/_tail_transcript` (~220 строк)
- `metrics.py:transcript_stats`
- `contrib/history_stats.py:stream_transcript`
- `analysis/analyze.py:stream_transcript`

Мature-замена существует: **`claude-agent-sdk`** (официальный Anthropic, MIT,
~8.1k★, v0.2.152 — «Claude Code as a library»). Его `get_session_messages()` /
`get_session_info()` / `list_sessions()` читают **те же**
`~/.claude/projects/*.jsonl`. → Новый фаза 0: вынести один модуль
`transcript.py`; сначала stdlib-обёртка, а при желании — подменить внутренность
на SDK. Это и есть «сереновский» ход: один официальный API вместо 4 парсеров.

**🔴 НЕ ТРОГАТЬ — спавн-модель `claude --bg` (`_spawn_native_agent`/`_parse_bg_id`).**
SDK *мог* бы заменить спавн, но он запускает `claude` как **прикреплённый
subprocess** (interactive `ClaudeSDKClient` / one-shot `query()`), а не detached
`--bg` агента — это **другая философия исполнения**. Ваша архитектура (README)
сознана: делегирование = нативная отсоединённая `claude --bg` сессия, видимая в
`claude agents`, «first-class inspectable, не bespoke entity». SDK здесь —
откат, а не прогресс. **Ядро спавна остаётся.**

**🟡 ОПЦИОНАЛЬНО — `code_nav.py:_simple_yaml_load` (~70 строк) → PyYAML.**
Вступает в конфликт с политикой «stdlib only, 0 зависимостей». Решение за вами;
по умолчанию не трогаем.

**✅/❌ Не нужно:** `ccusage` (18.5k★) — ваш `metrics.py` уже хорош и stdlib.
Task Master / Linear MCP — ваша `coordination.sqlite3` уже *есть* local
task-board (после фазы 1); SaaS-board только добавляет привязку.

Оркестрация-качество: research подтвердил — **готового scorer'а нет нигде**
(OTel→Langfuse/Helicone/AgentOps дают traces, но не «качество делегирования»).
Считаем сами (фаза 3).

## 1. Что уже есть на диске (данные для дашборда)

Корень состояния: `STATE_DIR = ~/.claude-local-delegate`
(`CLAUDE_LOCAL_DELEGATE_STATE_DIR` для override), server.py:190,
coordination_runtime.py:28.

| Источник | Что | Формат |
|---|---|---|
| `coordination.sqlite3` | таблицы `tasks` (475 строк), `events` (656) | sqlite; `tasks.body` = JSON задачи (id, project, task_key, owner, summary, mode, paths, depends_on, status, runs[], note, created_at, updated_at); `events.body` = `{task_id, owner, kind, text, at}` |
| `runs.json` | ростер всех запущенных/бывших делегаций | JSON |
| `metrics.jsonl` | append-only лента: `spawn` (server.py:608), `rate` (server.py:1044) с quality/worth_it + transcript_stats (metrics.py:75-198) | JSONL |
| `runs/<id>/{meta.json,mcp-config.json}` | мета + конфигурация каждого запуска | JSON |
| `verified/*.json` | состояния work→check→revise циклов | JSON |
| `batches/*.json` | fan-out батчи | JSON |
| `inflight.json`, `session-starts.json`, `gateway-status.json` | пул-тикеты, когорта сессий, статус враты | JSON |

Статусы задач: `active | paused | waiting | done | cancelled`
(enum coordination.py:128). Сталeness-флаг `>900s` считается в `sync`
(coordination.py:160). Приоритетов/упорядочивания **нет вообще**: сортировка по
`rowid` (FIFO, coordination.py:46), `depends_on` только записывается, не блокирует
(coordination.py:88-90). HTTP/UI/push поверхности **нет** — только MCP stdio;
единственный «push-like» механизм — pull-курс `project_sync(after_event=N)`.

Это ключевое: **всё, что вы хотите видеть, уже на диске в машиночитаемом виде.**
Не хватает только (a) поля приоритета, (b) UI, (c) агрегаций качества.

## 2. Архитектура решения

Принцип: **не трогать ядро, добавить один thin-слой** — отдельный процесс
`dashboard.py` (read-mostly HTTP + 3-4 write-операции), который общается с теми же
файлами, что и MCP-сервер. Конкуренция в SQLite уже арбитражна
(`Board.transaction` = `BEGIN IMMEDIATE`, coordination.py:29-43) — веб-процесс
получает ту же атомарность, что и MCP.

```
┌──────────────────────┐        ┌──────────────────────┐
│ claude-local-delegate│        │  dashboard.py (новый) │
│ MCP stdio (сервер)   │        │  HTTP 127.0.0.1:PORT  │
│ делегаты, MCP-клиент │        │  read: sqlite, jsonl  │
│                      │        │  write: priority, ... │
└──────────┬───────────┘        └──────────┬───────────┘
           │        BEGIN IMMEDIATE        │
           ▼                               ▼
     ~/.claude-local-delegate/ (sqlite + jsonl + json)
```

Техно-стек: **stdlib only** (как и весь проект — ноль внешних зависимостей):
`http.server.ThreadingHTTPServer` + JSON API + одностраничный SPA (vanilla JS,
polling ~3-5c). WebSocket в stdlib нет — polling на 3c для 475 задач/656 событий
дешёв и надёжнее; если захочется «live» — WebSocket добавим позже через
`websockets` как опцию. SPA — один `index.html` (~400 строк) без build-шага.

Альтернатива (отвергнута): встроить UI в MCP-сервер (`server.py`) — нельзя:
сервер — stdio-процесс, привязанный к одному MCP-клиенту; HTTP-слушатель в нём
ломает модель «один сервер на один клиент» и усложняет crash-объединение.

## 3. Фазы

### Фаза 0 — `transcript.py`: 4 парсера → 1 (optional, рекомендую)

0.0.1. Новый модуль `transcript.py` (stdlib): единые `iter_events(path)`,
`summary(path)` (prompt/digest/usage с dedup по `message.id`), `stats(path)`
(то, что сейчас в `metrics.transcript_stats`), `tail(path)`.
0.0.2. `server.py`, `metrics.py`, `contrib/history_stats.py`,
`analysis/analyze.py` — переключить вызовы на `transcript.py`; удалить дубли.
Внутренность `stats` — та же (dedup по `message.id` — уже отлажен
backlog-тестами); при желании позже подменить на `claude-agent-sdk`
(`get_session_messages`) за одним интерфейсом.
0.0.3. Тесты: существующие `test_metrics.py`/`test_efficiency.py` прогоняются
через новый модуль без изменений результатов (parity-check).

Эта фаза независима от фаз 1-3, чистый рефакторинг-выигрыш, откат = revert.

### Фаза 1 — Поле приоритета (ядро, ~2ч)

1.1.1. `coordination.py`: добавить `priority` (int, default 0; больше = выше) в
тело задачи в `claim` (coordination.py:91-93), принять в `update`, не менять
схему БД (body — JSON, backward-compat: отсутствие поля = 0).
1.1.2. `sync` (coordination.py:158-163): сортировка `active` по
`(-priority, rowid)` — так MCP-оркестратор тоже увидит порядок очереди.
1.1.3. MCP-схема: `task_claim`/`task_update` — опц. аргумент `priority`
(coordination.py:236-241), так что приоритет меняется и из MCP, и из GUI.
1.1.4. Тесты: `test_coordination.py` — claim с priority, update priority,
упорядочивание в sync, backward-compat (старые записи без поля).

Риски: минимальные. Все правки — additive; `tasks.body` — blob-JSON, миграция
не нужна.

### Фаза 2 — Дашборд (новый процесс, ~1 день)

`dashboard.py` в корне репо (или `contrib/dashboard.py`) + `static/index.html`.
Запуск: `python3 dashboard.py --port 8321` → `http://127.0.0.1:8321`.
Бинд только loopback; без auth (документировать: локальный инструмент, не
публиковать наружу).

API (все read = прямой read-only доступ к sqlite/jsonl; write = те же
транзакции `Board`):

- `GET /api/board?project=<abs path>` — все задачи проекта (все статусы,
  priority, owner, stale, runs), подсчёты по статусам.
- `GET /api/events?project=..&after=<id>&limit=50` — событийная лента
  (та же таблица, что и `project_sync`, но без сжатия — GUI хочет всё).
- `GET /api/runs` — ростер из `runs.json` + `inflight.json`: кто сейчас работает
  (id, task, age, state: working/blocked/done/failed), батчи, verified-циклы.
- `GET /api/quality?window=7d` — агрегаты из `metrics.jsonl` (см. фазу 3).
- `POST /api/task/priority` `{task_id, priority}` — write через `Board`
  (transaction `BEGIN IMMEDIATE`, как `update`).
- `POST /api/task/note` `{task_id, message}` — write через `Board.note`.
- `POST /api/task/takeover` `{task_id, status}` — то же, что `task_update
  takeover=true` (та же валидация: idle > 1800s, runs settled).

SPA, 4 вкладки:

1. **Доска** (главная): канбан-колонки `active → paused/waiting → done/cancelled`
   по выбранному проекту; карточка: task_key, summary, owner, elapsed, stale-
   маркер, **priority (клик = слайдер/число → POST)**, runs-бейджи. Счётчики
   сверху: N актив / M сделано / K осталось (по depends_on, если задано).
   Фильтр по проекту (список из `SELECT DISTINCT project`).
2. **Запуски**: живая таблица делегатов (runs + batches + verified), кто что
   делает, сколько секунд работает, state. Обновление polling'ом 3c.
3. **События**: таймлайн `events` (по курсору `after_event` — тот же механизм,
   что у MCP, но без лимита 20; авто-скролл вниз, filter по kind).
4. **Качество**: см. фазу 3.

Обновление: `setInterval(fetch, 3000)` по активным вкладкам; индикатор «updated
Nс назад». Данные на диске уже сжатые (body-JSON, event text ≤2000) — polling
лёгкий.

Тесты: `tests/test_dashboard.py` — HTTP API против tmp-STATE_DIR: board/events/
runs/quality read, priority/note/takeover write, loopback-only bind,
backward-compat (БД без priority-поля).

### Фаза 3 — Оценка качества оркестрации (~2-3ч)

Данные уже есть (`rate_delegate` → `rate`-события в metrics.jsonl с
quality/worth_it + transcript_stats; `history_stats.py` — когорта
session-starts). Нужно: агрегации + тренд + UI.

Метрики (все из `metrics.jsonl` + `coordination.sqlite3`, window 24h/7d/30d):

- **Исход делегаций**: доля `done` vs `cancelled` (из board); share of tasks
  с takeover (маркер «сломанного» оркестратора).
- **Оценка работы делегата**: median/mean/p25-p75 `quality` (0-100) и
  `worth_it` (0-100) из rate-событий; доля `worth_it ≥ 50` («делегирование
    окупило себя»).
- **Эффективность**: median duration, output tokens, api_calls, peak_context
  (transcript_stats, metrics.py:89-198) — тренд против базлайна из
  ARCHITECT_BRIEF (804s, 19 calls, 25k tokens, 67k peak).
- **Переработка**: доля verified-запусков, прошедших ≥1 ревизии (из
  `verified/*.json`); доля blocked/drift (из runs.json states).
- **Суммарный индекс «здоровья оркестрации»**: один число 0-100 = взвешенное
  (качество делегаций 40%, успех задач 30%, эффективность 20%, отсутствие
  takeover/revise 10%) — настраиваемые веса в `--config`.

UI вкладки «Качество»: карточки-гаджи + 2 графика (timeline quality/worth_it;
timeline duration/tokens) — чистый canvas/SVG без библиотек (или один CDN-
bundle типа `chart.js` — на ваш выбор; stdlib-путь = canvas вручную).

Отчёт: `dashboard.py --report` → `quality_report.md` (CLI-аналог GUI для
архива), данные — те же агрегаторы.

## 4. Что мы сознательно НЕ делаем (и почему)

- **Не вводим MAF** (решение в §0). Никаких workflow-графов, agents, OTel —
  у вас уже есть свой, проверенный эквивалент (MCP-инструменты + sqlite-доска +
  jsonl-лента), и он дешевле в поддержке.
- **Не переносим данные в новую БД**: читаем существующие файлы напрямую
  (read-only + те же транзакции). Zero-миграция.
- **Не делаем auth/HTTPS/мультихост**: tool на loopback одной машины. Если
  понадобится доступ с другой машины — SSH-туннель, а не встроенный TLS.
- **Не делаем push-WebSocket в фазе 2**: polling 3c при 475 задачах не
  заметен; WebSocket — опция на потом (одна зависимость `websockets`).

## 5. Риски и меры

| Риск | Мера |
|---|---|
| Гонка записей web-процесс vs MCP-сервер | Одна и та же `Board.transaction` (`BEGIN IMMEDIATE`, sqlite timeout 15s) — арбитр уже есть. |
| GUI пишет задачу, которой не видит оркестратор | Приоритет/note/takeover — idempotent-операции над стабильным `task_id`; оркестратор увидит в следующем `project_sync`. |
| `metrics.jsonl` растёт без界 | Read-по offset (запоминаем file offset+line), не парсим всё с нуля на каждый запрос; rotate по `--max-mb` (опция). |
| Loopback-порт перехвачен | Бинд `127.0.0.1:8321`, `--port` переопределяется; документировать. |
| Backward-compat старых БД | `priority` — опц. поле в JSON-body; отсутствие = 0. Никто не парсит sqlite-схему кроме `Board`. |

## 6. Оценка объёма и порядок

| Фаза | Объём | Оценка | Блокирует |
|---|---|---|---|
| 1. Приоритет в ядре | +~60 строк в coordination.py, +тесты | 2ч | фазу 2 (приоритет-UI) |
| 2. dashboard.py + SPA | ~800-1200 строк (py) + ~400 (html/js) | 1 день | фазу 3 |
| 3. Качество (агрегаты + UI + отчёт) | ~300-400 строк | 2-3ч | — |

Порядок строго 1→2→3: приоритет сначала (иначе GUI пишет поле, которого нет в
ядре), дашборд на read-only данных (даже без фазы 1 уже полезен), качество
последним (нужен стабильный каркас API).

Всё три фазы — аддитивные, откат = удаление файла/поля, ядро не ломается на
каждом шаге.