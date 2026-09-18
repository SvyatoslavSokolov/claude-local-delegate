# PIPELINE_REVIEW_RU — аудит пайплайна: производительность и токено-экономика

Дата: 2026-09-08. Сценарий: один компьютер, на нём Claude Code и Codex. Локальный worker, SUPERVISED RUN. Код и доки читаны в месте; ни один файл
не изменён, бенчмарк не запускался, GPU не трогался.

## 1. Проверенные факты (официальные доки; страницы открывались успешно)

- **Codex MCP** (https://learn.chatgpt.com/docs/extend/mcp?surface=cli): серверы в
  `~/.codex/config.toml` (stdio/HTTP); есть `enabled_tools`/`disabled_tools` (allow/deny на
  уровне сервера), `tools.<tool>.output_token_limit` — **бюджет токенов вывода одного инструмента**
  (по умолчанию 20% allowance на сериализацию), `default_tools_approval_mode` (auto/prompt/writes/
  approve), поле `instructions` сервера читается Codex целиком. → Формальный механизм для
  «компактных результатов» на стороне Codex существует.
- **Claude Code, фоновые агенты** (https://code.claude.com/docs/en/agent-view): `claude --bg "<task>"`
  — промпт позиционный; каждая background-сессия может иметь **свой `--model`**; сессии
  перечислены в едином нативном ростере (`claude agents`); режим прав и модель унаследуются
  и переживут рестарт супервизора.
- **Claude Code, permissions** (https://code.claude.com/docs/en/agent-sdk/permissions):
  **`allowedTools` НЕ ограничивает `bypassPermissions`** — в bypass все инструменты исполняются
  без промптов; `allowedTools` лишь пре-аппрувит. Рабочая «замок-конфигурация» —
  `permissionMode: "dontAsk"` + allow-список (всё несанкционированное — deny, не prompt), либо
  `disallowedTools` (deny работает во всех режимах, включая bypass; голый `disallowedTools: ["Bash"]`
  убирает инструмент из контекста). Субагенты наследуют bypass родительской сессии и **не могут**
  переопределить его. (Подтверждено и issue https://github.com/anthropics/claude-code/issues/12232.)
- **vLLM, prefix caching** (https://docs.vllm.ai/en/stable/design/prefix_caching): APC — hash-based
  переиспользование KV-блоков: блок хешируется по своим токенам + токенам префикса; запросы с
  одинаковым префиксом делят физические блоки и пропускают prefill общей части. «Почти free lunch»,
  вывод модели не меняется.
- **vLLM, тюнинг/память** (https://docs.vllm.ai/en/stable/configuration/optimization): уменьшение
  `max_num_seqs` / `max_num_batched_tokens` снижает давление на KV-пул; преэмпции видны в метриках;
  `max_model_len` и `gpu_memory_utilization` (default 0.9) определяют размер пула. (Список аргументов
  двигателя: https://docs.vllm.ai/en/v0.4.1/models/engine_args.html — `--enable-prefix-caching`,
  `--max-num-seqs` default 256, `--max-num-batched-tokens`.)

**Локальные параметры vLLM (дано в задаче, не перепроверялось):** Qwen3.8-27B-INT4,
TP=4, `max-model-len=1000000`, `max-num-seqs=16`, `max-num-batched-tokens=16384`, KV fp8,
prefix caching вкл.

**Квота (наблюдение, не вывод):** последняя usage этого Codex-саунса — 57% пятичасового лимита;
агрегировано input 3.62M, из них 3.32M cached (~92% hit), output 30.5k. По суммарным токенам
формулу лимита **не** выводим (запрещено задачей); это только наблюдаемый срез.

## 2. Находки по коду (факты в этом репозитории)

1. **Надувание контекста/вывода на стороне main (главный расход).**
   - `check_delegate_status` (server.py:621–682) при каждом опросе делает `_narration_digest(tr, max_lines=0)`
     и `_token_usage(tr)` — **полный** повторный парсинг JSONL-транскрипта на каждый poll; у длинных
     сессий (до 534k токенов по TASK_DESIGN.md §14) это O(размеру) CPU и память на каждый вызов, хотя
     в ответ возвращается 3–4 строки.
   - `get_fanout_result` (server.py:942–971) и `get_delegate_result` возвращают **полный** финальный
     текст каждого агента в один MCP-ответ → раздутый tool output в контексте родителя.
   - `watch_delegate` по умолчанию режет до 48 строк (server.py:758) — ок, но `max_lines<=0` выводит всё.
2. **Глобальная сериализация всех tools/call через flock (включая read-only).**
   coordination_runtime.py:154–200: `handle` оборачивает **любой** tools/call в `with locked():`
   (fcntl.flock LOCK_EX на `operations.lock`). Внутрь этого же lock'а попадают: `board.authorize`
   (SQLite `BEGIN IMMEDIATE`, coordination.py:24–38), подсчёт roster'а и **сам `subprocess.run`
   `claude --bg` с timeout=120s** (server.py:311–315). Т.е. один спавн блокирует все прочтения и
   спавны второго супервизора до ~2 мин.
3. **Лиминг по нативному ростеру считает и платные фоновые сессии.**
   coordination_runtime.py:73–76: `active` = все `kind=='background'` из `claude agents --json`,
   не только те, что идут на локальный vLLM. Платная (Anthropic) background-сессия занимает слот
   «пула vLLM» (`LOCAL_SERVER_MAX_CONCURRENCY=16`, server.py:115) и блокирует спавн локальных,
   хотя KV-пул не затрагивает.
4. **Scope задачи — не жёсткий sandbox.**
   coordination_runtime.py:82–87 вставляет в промпт «Do not modify outside this scope» — кооперативный
   (подтверждено также COORDINATION.md §Limits и доками: allowedTools не ограничивает bypass;
   worker по умолчанию `bypassPermissions`, server.py:169–171, c Bash → полный доступ к FS).
5. **Blocked-ответ = stop + fork, не живой SendMessage.**
   coordination_runtime.py:105–122 (`continue_delegate`): форк через `--resume <sid> --fork-session`
   (server.py:290–291) только после settle; Codex не имеет Claude'овского SendMessage (COORDINATION.md
   §Talking). В Claude SendMessage надёжен только в state `blocked` (server.py:35–57).
6. **Нет автоматического планировщика.**
   Рoster-ceiling просто отклоняет спавн «retry later» (coordination_runtime.py:76); `waiting`-задачи
   не стартуют сами (COORDINATION.md §Starting work, п.4: "No execution starts automatically").
7. **Спаун под тем же flock'ом, что и весь остальной MCP-трафик** (см. 2) — при двух
   супервизорах фактическая concurrency ниже заявленной даже до vLLM-очереди.

## 3. Гипотезы (не подтверждено запуском; требуют бенчмарка §5)

- H1: при 8–16 параллельных children общий prefill упирается в `max-num-batched-tokens=16384`
  (итерации делятся на всех); TTFT растёт ~линейно с concurrency до ~4–8.
- H2: common system-prompt+persona+`shared_instruction` дают высокий APC hit после первого
  запроса (коды slim-профиля совпадают) — но **первый** child в серии платит полный prefill.
- H3: `max-model-len=1000000` увеличивает share KV-резерва на seq и частоту преэмпций при
  16 seq одновременно; fp8-KV частично компенсирует.
- H4: наблюдаемые одновременные смерти (`The response stopped arriving`, TASK_DESIGN.md §14)
  коррелируют с load, а не с лимитом вывода (повышение `CLAUDE_CODE_MAX_OUTPUT_TOKENS` не помогает —
  уже зафиксировано там же).
- H5: 92% cached-input (3.32M/3.62M) — в основном APC + Claude-повторное чтение системного блока;
  на локальном vLLM аналогичный hit будет при **одинаковом** префиксе children.

## 4. Приоритеты улучшений

**NOW (без изменения production-кода — настройки/процедуры):**
- N1. На Codex-стороне задать `tools.<tool>.output_token_limit` для `get_delegate_result`,
  `get_fanout_result`, `watch_delegate` (доки §1) — резать раздувание до MCP-пограничья.
- N2. fan_out всегда с общим `shared_instruction` первой строкой (APC, §1 vLLM) и короткими items;
  один read-only прогон перед серией «прогревает» префикс.
- N3. Опросить `check_delegate_status` реже (интервал > длительности шага агента) — полный репарсинг
  транскрипта на каждый poll (находка 2).

**NEXT (мелкие правки в MCP, не меняют GPU/хранилище):**
- X1. Выход read-only инструментов (project_sync, watch_delegate, check_*, get_*result) **из**
  flock'а; `BEGIN IMMEDIATE` держать только на мутациях (находка 2).
- X2. Считать в пул vLLM только локальные сессии: проверять `--model`/settings по roster или
  по `local_backend_info` (находка 3); платные background не блокируют спавн.
- X3. Read-only делегаты: `--permission-mode dontAsk` + `--allowedTools Read,Grep,Glob`
  (или `plan` + deny), т.к. bypass игнорирует allow (доки Claude permissions) — реальный least
  privilege, а не «Bash на словах» (находка 4).
- X4. Компрессия результатов: `get_delegate_result` → последние ~40 строк + sha256 полного текста
  + `watch_delegate` по умолчанию; полный текст — по явном запросе (находка 1).

**LATER (архитектура):**
- L1. Реальный sandbox на запись: отдельный worktree на writer + один integration-owner
  (предусмотрено COORDINATION.md §Limits, но не реализовано).
- L2. Планировщик: авто-старт `waiting`-задач при высвобождении пула (событие по roster),
  с лимитом retry.
- L3. Provenance маршрутизации модели (идея в §6).
- L4. Кондиционный verifier (идея в §7).

## 5. Бенчмарк concurrency 1/2/4/8 (только план; без изменения GPU и без запуска)

**Нагрузка (одинаковая для всех уровней):** M=16 идентичных read-only задач через `fan_out_to_local`
(общий `shared_instruction` ~2k токенов + item ~500 токенов; инструмент: `Read,Grep,Glob`;
`dontAsk`). Промежуточные уровни 1/2/4 — подмногоие первого пакета (1/2/4 items), уровень 8 —
8 items. Предварительно один «прогревочный» child для APC.

**Метрики (записывать в CSV):**
1. Wall: start→done на задачу; p50/p95 по уровню.
2. Токены с задачи: input/cached/output из `_token_usage` (server.py:488) каждого транскрипта.
3. vLLM (только чтение метрик, без изменения конфига): `gpu_cache_usage`, preemption counter,
   APC hit/miss (Prometheus, доки vLLM §1).
4. Ошибки: счёт spawn-rejections («Local pool full»), `The response stopped arriving`,
   state=failed, timeout spawn (>120s под flock'ом — находка 2).
5. Latency MCP: время одного `check_delegate_status` при 2 параллельных супервизорах
   (проверить находку 2 количественно).

**Критерии:**
- **Время:** p95(level k) ≤ 2.5 × p95(level 1) при k ≤ 4; при k=8 допускается рост, но
  wall(sum) должен снижаться vs level 1 (иначе parallel не окупается).
- **Токены:** cached/input ratio ≥ 0.70 на общих префиксах (иначе APC не работает);
  output/задача — в пределах ±20% от level 1 (parallel не должен раздувать output).
- **Ошибки:** 0 одновременных stream-смертей; 0 spawn-rejections; retry rate < 5%.
- **Решение:** выбрать максимальное k, где все 4 критерия зелёные; ниже к-значений при
  красном — фиксировать в COORDINATION.md как empirically-verified ceiling (H1/H4).

## 6. Provenance маршрутизации модели (идея)

Цель: каждый child и его результат должны быть атрибутируемы к **конкретному** бэкенду/модели,
а не к «локальному» на словах (TASK_DESIGN.md: не доверять заявленной identity).

Механизм: при spawn (coordination_runtime.py:spawn) дописывать в `events` (coordination.py:48):
`{kind: 'spawn', run_id, settings_path_sha, ANTHROPIC_MODEL, routes (local_backend_info),
roster_kind: 'local'|'paid'}`. `local_backend_info` (coordination_runtime.py:124–131) уже возвращает
`vllm_route_confirmed` без ключей — использовать как есть. При `task_update(status=done)` проверять:
`roster_kind==local` для всех runs задачи, иначе flag `model_provenance_mismatch` в note.
Следствие: (а) квота/стоимость считаются только по реальным локальным run'ам (находка 3);
(б) `check_delegate_status` может добавить строку `backend: <route>` из recorded provenance,
не вызывая модель.

## 7. Компактный результат и кондиционный verifier (идея)

Сейчас `delegate_verified` (server.py:1219) всегда стартует worker+checker с полным spec'ом и
полным self-report'ом worker'а в промпте checker'а (server.py:1086) — удвоение контекста.

Изменения (только по идее):
- **Компактный контракт worker:** финальный ответ — ≤30 строк: `{status, files_changed[], checks_run[],
  evidence (последние 10 строк каждого чека), not_verified[]}` + sha256 полного транскрипта
  (уже есть `_result_hash`, server.py:1011).
- **Кондиционный verifier:** checker стартует только если (a) даны `acceptance_criteria` ИЛИ
  (b) `files_changed` непусто; для чисто read-only задач без критериев — checker **не** спавнится,
  экономится половина round-trip. Стагнация/timeout-гарды остаются (server.py:1152, 1126).
- **Компактный промпт checker:** spec' + acceptance + `files_changed` + `git diff --stat`
  (не полный self-report) → контекст checker'а не масштабируется с размером работы worker'а.

## 8. Что не удалось проверить

- Реальные значения vLLM-конфига (только те, что даны в задаче) — `local_backend_info` не вызывался
  (не меняем production, не читаем секреты).
- Формула 5-часового лимита Codex — по условию не выводится; приведён только наблюдаемый срез.
- Бенчмарк §5 не запускался (запрещено: «no GPU changes or benchmark execution»).
- Страницы docs vLLM stable не открывались напрямую целиком — только excerpts из поиска;
  все три основных URL проверены как доступные (Codex — полный fetch; Claude Code и vLLM —
  содержимое из excerpts).
---

## 9. Что реализовано по этому аудиту (2026-09-08, server v0.8.0)

Сделано в коде (`server.py`, `coordination_runtime.py`, `coordination.py`), покрыто тестами
`test_efficiency.py` (17) и `test_runtime.py`; полный прогон — 38 тестов, зелёный.

| Пункт аудита | Статус | Где |
|---|---|---|
| Находка 1 (двойной разбор транскрипта на каждый poll) | сделано | `_transcript_summary` — один проход + memoise по (path, size, mtime); `_narration_digest`/`_token_usage`/`_first_user_text` читают его |
| X4 (компрессия результатов) | сделано | `_compact`: хвост + sha256 полного текста; `get_delegate_result` (40 строк) и `get_fanout_result` (15 строк на item), параметры `full` / `max_lines` |
| X1 (read-only вне flock) | сделано | `READ_ONLY_CALLS` минует межпроцессный lock; `unlocked()` отпускает его на время `claude --bg`, слот держит билет в `inflight.json` |
| X2 (в пул считать только локальные) | сделано | учёт по `runs.json`; платная background-сессия больше не занимает слот vLLM |
| X3 (реальный least privilege) | сделано | read-only allowlist → `--permission-mode dontAsk` (в bypass allowlist игнорируется); пишущие делегаты остаются в bypass |
| §6 (provenance маршрутизации) | сделано | `runs.json`: модель, base_url, sha настроек, permission mode, tools, cwd, время; строка `backend:` в `check_delegate_status` |
| §7 (кондиционный verifier + компактный промпт checker'а) | сделано | `_skip_verification` (нет критериев + read-only либо чистый `git status`) → UNVERIFIED PASS; параметр `always_verify`; в промпт checker'а идут `git status --porcelain` + `git diff --stat HEAD` и только хвост самоотчёта (30 строк) |
| N1 (output_token_limit на стороне Codex) | сделано | блоки `[mcp_servers.claude-local-delegate.tools.<tool>]` добавлены в `~/.codex/config.toml` (с бэкапом) и в `contrib/install_codex.py` для новых установок |
| N2 (общий префикс для APC) | сделано частично | `shared_instruction` и раньше шёл первым; теперь это зафиксировано в описании инструмента `fan_out_to_local` как правило (весь общий текст — в `shared_instruction`) |
| N3 (реже опрашивать статус) | сделано | указание о частоте опроса в описании `check_delegate_status`; сам опрос стал дешёвым (см. находку 1) |
| §5 (бенчмарк 1/2/4/8) | инструмент готов, прогон не делался | `contrib/bench_concurrency.py` — драйвер по stdio, CSV по уровням и по агентам; запускать вручную, когда GPU свободен |
| L2 (планировщик) | сделано частично | `project_sync` возвращает `pool` (max / local_running / spawning / headroom) и `startable_waiting_tasks`; автозапуск по-прежнему не делается — задачи стартует супервизор |
| L1 (worktree-песочница на запись) | НЕ сделано | архитектурное изменение: отдельный worktree на writer + один integration-owner. Ограничение зафиксировано в COORDINATION.md §Limits |
| H1–H5 (гипотезы) | не проверялись | требуется прогон §5 на свободном GPU |

Замечание по совместимости: уже запущенные процессы MCP работают на старом коде — эти
изменения вступают в силу после перезапуска Claude и Codex (см. COORDINATION.md §Limits).
