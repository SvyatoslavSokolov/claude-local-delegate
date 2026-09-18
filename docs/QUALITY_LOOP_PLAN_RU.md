# План: замкнуть цикл «аналитика → улучшение пайплайна»

Дата: 2026-09-13. Статус: план, согласование.
Объём намеренно минимальный. **Дашборд и приоритет — не в этом плане**
(оставлены на дискрецию основной модели; GUI_DASHBOARD_PLAN_RU.md не трогаем).

Цель: одна самодостаточная, максимально простая система качества/статистики
с простым кодом, по которой видно, что в пайплайне деградирует и что именно
поменять. Не «ещё один скрипт-отчёт», а **замкнутый контур**: метрика →
инсайт → именованное действие.

## 0. Почему текущая аналитика не замыкает цикл (аудит, 2026-09)

4 скрипта (metrics.py, history_stats.py, analyze.py, delegate_report.py) — все
**диагностические**: выдают числа/проценты, но:
- нет **тренда во времени** (worth_it/quality по дням/неделям);
- **blocked-категории не сохраняются** — `_classify_blocked` (server.py:931)
  считается per-poll и теряется;
- **`verified/*.json` и `batches/*.json` не читаются никем** — revise-rate и
  batch-success не считаются вообще;
- **нет дельты к базлайну** (804s / 19 calls / 25k tok / 67k peak) — только
  абсолютные пороги;
- **нет mapping метрика→действие** — только 5 статических prompt-recommendations.

Дубли (тот же «до-Serena» паттерн, внутри кода): parse_timestamp ×3
(metrics.py:31, history_stats.py:43, analyze.py:63); percentile ×3;
transcript streaming ×3; load_roster_states ×2; build_index ×2.

Ключевой вывод: (c) verified и (d) batches **уже лежат на диске**, просто их
никто не читает. Значит перестраивать систему НЕ нужно — нужно читать то, что
есть, + сохранить то, что теряется (blocked). Минимум изменений.

## 1. Архитектура: 3 маленьких файла, без дашборда

Принцип: **один вход (state dir), один выход (отчёт), один shared-модуль**.
Тот же stdlib-only, loopback не нужен (CLI, не HTTP). Никакого GUI.

```
        ┌────────────────────────────────────────────┐
        │  shared:  metrics.py (раздуть до "ядро")    │
        │  parse_timestamp, percentile, stream_transcript
        │  + новые: load_verified, load_batches       │
        └───────────────┬────────────────────────────┘
                        │ (все скрипты импортят отсюда)
   ┌────────────────────┼───────────────────────────────┐
   │                    │                               │
   ▼                    ▼                               ▼
persist blocked       report.py (НОВЫЙ)        delegate_report.py
(в server.py, 1       "insight" emitter:       (остаётся; переиспользует
 append/event)         метрика → именованное    shared metrics.py)
```

Три файла — и всё. Дашборда нет: отчёт = один вызов CLI, печатает в терминал
(и опц. `--md` в файл). Это и есть «самодостаточная простейшая система».

## 2. Фазы (все аддитивные, откат = revert)

### Фаза A — Shared core в `metrics.py` (убрать дубли, ~2ч)

A.1. В `metrics.py` (сейчас 197 строк) вынести единые:
- `parse_timestamp` (уже есть, metrics.py:31)
- `percentile(values, p)` — заменить три копии nearest-rank
- `stream_transcript(path)` — единый итератор (заменить 3 копии; логика та же,
  dedup usage по `message.id` уже отлажена backlog-тестами)
- **новые** readers (сейчас не читаются никем):
  - `load_verified(state_dir)` → список {vid, phase, iteration, history[]} из
    `verified/*.json`
  - `load_batches(state_dir)` → {batch_id → agent_ids, outcome} из `batches/*.json`
A.2. `contrib/history_stats.py`, `analysis/analyze.py`, `contrib/delegate_report.py`
— переключить import на `metrics.*`; удалить локальные копии. Парити-тест:
вывод history_stats/analyze **не меняется** (сравниваем на live state dir до/после).

### Фаза B — Сохранить то, что теряется (blocked), ~1ч

B.1. В `server.py`: когда `check_delegate_status`/`_classify_blocked` (server.py:931)
определяет blocked-категорию (hook-gate / mcp-tool-missing / websearch-broken /
permission-prompt / needs-input / unknown) — **один** append в metrics.jsonl:
`{event:'blocked', run_id, category, at, waiting_for}`. Один append per переход
в blocked (не per-poll, чтобы не раздувать ленту). Это **единственная** запись,
которой сейчас нет на диске; verified/batches уже есть.

### Фаза C — `report.py`: один самодостаточный отчёт + инсайты (~2ч)

`python3 contrib/report.py [--days 14] [--md out.md] [--csv out.csv]`
→ один вызов, всё в терминал (+ опц. flat CSV для pandas). Считает от
metrics.jsonl + coordination.sqlite3 + verified/ + batches/:

**Блок 1 — Тренд (что нет сейчас).**worth_it и quality по неделям (p50 + count)
— видно, деградирует ли делегирование в целом.

**Блок 2 — Health-index по категориям пайплайна** (каждая → именованное действие):
- **blocked-rate по категории** (из Фаза B + server.py:918-928): если
  `mcp-tool-missing` > X% → «добавь этот tool в DEFAULT_ALLOWED_TOOLS / проверь
  user-scope MCP»; `permission-prompt` > X% → «переведи задачу на writer-профиль
  или уточни dontAsk-gate».
- **verified revise rate** (из load_verified): доля verified-ранов с ≥1 ревизией.
  Высокий → «критерии приёмки слишком расплывчаты — жёстче acceptance_criteria».
- **fan-out batch success** (из load_batches): доля батчей, где ≥1 агент failed.
  Высокий → «бэч слишком широкий — сузь shared_instruction / разбей items».
- **дельта к базлайну** (p50 duration / api_calls / output_tokens / peak_context
  против 804s / 19 / 25k / 67k): растёт → «context/overhead растёт — проверь
  --tools grant, рост system-prompt».
- **по complexity×profile** (из delegate_report.py, переиспользуя shared):
  est/actual ratio по профилям → «fast-профиль на think-задачах → переключи на
  think», и т.д.

**Блок 3 — Инсайт-эмиттер (это и есть «замкнуть цикл»).**Каждая выше метрика
через **набор статических правил** превращается в **одну строку «insight →
именованное действие»**, отсортированную по серьёзности. Формат:
```
[!] blocked mcp-tool-missing 38% (порог 15%)
    → добавить mcp__ParallelSearch в DEFAULT_ALLOWED_TOOLS
[!] verified revise-rate 62% (порог 30%)
    → жёстче acceptance_criteria в delegate_verified
[!] p50 duration +41% к базлайну (1134s vs 804s)
    → проверить --tools grant и рост fixed overhead
```
Пороги — константы наверху файла (настраиваются одним местом, не хардкод по
всему коду). Это и есть «по аналитике улучшать пайплайн»: отчёт сам говорит,
что менять.

Пороги по умолчанию (настраиваются, начало): blocked>15%, revise>30%,
delta>25%, batch_fail>20%.

**Блок 4 — Плоская per-task CSV (для pandas).**
`python3 contrib/report.py --csv out.csv` (по умолчанию
`~/.claude-local-delegate/reports/delegations.csv`) — **одна строка на делегацию**
(run), flat/long-формат, без вложенного JSON — ровно то, что грузится
`pd.read_csv`. Колонки (все уже есть на диске, join по run_id/task_id):

| колонка | источник |
|---|---|
| `run_id, task_id, task_key, name, project, cwd` | spawn (metrics.jsonl) + runs.json |
| `created_at, finished_at, duration_s` | spawn.at + rate.stats |
| `complexity, est_minutes, profile, model, read_only, prompt_chars` | spawn |
| `quality, worth_it` | rate (0–100, оставляем — это основа оценки) |
| `api_calls, output_tokens, input_tokens, cached_input_tokens, peak_context, thinking_chars, tool_calls` | rate.stats |
| `blocked_category` | Фаза B (пусто, если не блокировался) |
| `verified`, `verified_iterations` | load_verified (0 если не verified-ран) |
| `batch_id` | load_batches (пусто, если одиночный ран) |
| `task_status` | coordination.sqlite3 (done/cancelled/active) |

Это и есть персистентный per-task quality/worth_it-данные: тренды, scatter
`quality` vs `duration_s`, boxplot по complexity×profile и любой другой график
строятся на `df = pd.read_csv('delegations.csv')` без какого-либо GUI.
Вывод CSV — один проход по metrics.jsonl + три read-only lookup-таблицы;
существующий отчёт в терминал при этом не меняется.

## 3. Что сознательно НЕ делаем

- **Никакого GUI/дашборда/HTTP** (срезано по вашему решению) — только CLI-отчёт.
- **Никакого поля приоритета** (срезано).
- **Не трогаем спавн `claude --bg`** — ядро дизайна; SDK-замена спавна = откат.
- **Не вводим MAF** и не подключаем внешние observability-платформы
  (Langfuse/Helicone/AgentOps) — self-host, stdlib, локально; «оркестрация-
  качество» как метрика готового инструмента не существует (research).
- **Не создаём 4-ю копию парсера** — всё через shared `metrics.py`.

## 4. Объём и порядок

| Фаза | Что | Оценка | Блокирует |
|---|---|---|---|
| A. shared metrics.py | убрать 6+ дублей, +load_verified/load_batches | 2ч | B, C |
| B. persist blocked | 1 append/event в server.py | 1ч | C (blocked-блок) |
| C. report.py + инсайты + CSV | один отчёт: тренд + health-index + insight-эмиттер + **flat per-task CSV** (quality/worth_it и т.д.) | 2ч | — |

Порядок A → (B ∥ C): A первым (остальные импортят shared), B и C независимо.
Всё откатывается отдельно; ядро (spawner, coordination) на каждом шаге не ломается.

## 5. Риски

| Риск | Мера |
|---|---|
| Блок B добавляет записи в metrics.jsonl | Один append per переход в blocked (не per-poll) — лента не раздувается; read по offset. |
| report.py читает verified/batches, которых может не быть | Graceful: пустые dirs → блок «нет данных», не краш. |
| Парити после дедупликации (фаза A) | Парити-тест: вывод history_stats/analyze на live state до/после совпадает. |
| «Инсайт»-правила ложные | Пороги — константы наверху, настраиваются; формат «insight → действие» легко править без кода-логики. |

## 6. Что это даёт в итоге

Одна команда (отчёт в терминал):
```
python3 contrib/report.py --days 14
```
→ тренд worth_it/quality, health-index по 5 категориям пайплайна
(blocked/verified/fan-out/baseline/complexity×profile), и **список
«insight → именованное действие»** — что в пайплайне деградирует и что поменять.

Плюс плоская CSV для pandas (per-task quality/worth_it — оставляем как вы просили):
```
python3 contrib/report.py --csv ~/.claude-local-delegate/reports/delegations.csv
```
→ `df = pd.read_csv(...)` — одна строка на делегацию, все метрики flat. На ней
строятся любые графики (тренд, scatter quality×duration, boxplot по
complexity×profile) без GUI. Самодостаточная, stdlib, без GUI, без нового ядра.
Это и есть замкнутый контур.