# Миграция `claude-local-delegate` на другой Linux-компьютер + повседневное использование

Документ описывает (а) перенос всей системы на новую Linux-машину и (б) ежедневные паттерны работы.
Все пути — абсолютные. **Ни один ключ/токен не вставляется в этот файл** — секреты живут в
конфигурационных файлах, которые переносятся в зашифрованном/ограниченном виде.

Репозиторий сейчас: `/home/svyatoslav/Projects/claude-code-mcp/claude-local-delegate`.
Обозначим его на новой машине как `$REPO` (абсолютный путь).

---

## Часть A. Миграция

### A0. Принципы (сначала, чтобы не сломать)

1. **Ваш сценарий: один новый компьютер, на нём одновременно Claude Code и Codex.**
   Переносим установку целиком. Оба клиента запускают один и тот же код MCP
   отдельными stdio-процессами и используют общую локальную SQLite-доску
   `~/.claude-local-delegate/coordination.sqlite3` под одним пользователем ОС.
   Сетевой координатор не нужен. Старую машину после переноса не используем
   для параллельного изменения того же проекта.
2. **Не переносить на другую машину:**
   - активный `coordination.sqlite3` с живыми резервированиями (task keys, блокировки);
   - PID'ы daemon'ов/сессий (`claude --bg` агентов, MCP-процессы). PID'ы привязаны к
     конкретному ОС/хосту и на новой машине бессмысленны и опасны (коллизируют).
   Если новая машина — **чистое** окружение, стартовать с **пустым** state-каталогом
   (MCP создаст свежий `coordination.sqlite3` сам).
3. **Ссылка-симлинк пересоздаётся, а не копируется.** `~/.codex/local-delegate-agents`
   — это *ссылка* на `~/.claude/agents`. Если её скопировать как обычный файл/папку,
   она «замерзнет» (stale link) и не обновится. Инсталлятор (`contrib/install_codex.py`)
   сам создаёт симлинк; ручное копирование ссылки — ошибка.
4. **User-scope MCP** хранится в `~/.claude.json` → `mcpServers` (не в `settings.json`),
   потому что фоновые `claude --bg` агенты читают MCP только из user-scope и `.mcp.json`.

### A1. Требования на новой машине

- **Python 3.11+** (`server.py` — stdlib-only, `pip install` не нужен).
- **Claude CLI с поддержкой `--bg`** (нативные background-агенты). Проверить:
  `claude --help | grep -- --bg`.
- **Codex**, залогиненный **по подписке** (не через API-ключ) — для основной сессии.
- **Основная сессия** (Claude и Codex) остаётся на **подписке** (`claude`, `codex`);
  локальный vLLM-профиль используется **только** для дочерних `--bg` агентов.
- node/npx (если нужны сторонние MCP типа github/context7) — опционально.
- Доступ в сеть к **удалённому vLLM/LiteLLM** (см. A4).

### A2. Копирование репозитория

```bash
# Вариант 1 (если репо в git):
git clone <repo-url> /home/$USER/Projects/claude-code-mcp/claude-local-delegate
cd /home/$USER/Projects/claude-code-mcp/claude-local-delegate && git pull

# Вариант 2 (локально, без сети):
rsync -a --delete /home/svyatoslav/Projects/claude-code-mcp/claude-local-delegate/ \
  /home/$USER/Projects/claude-code-mcp/claude-local-delegate/
```
Зафиксировать абсолютный путь — он попадёт в обе регистрации MCP (A5, A6).

### A3. Общие персоны и slim-профиль vLLM (без раскрытия ключей)

Переносятся **файлами**, **не вставляются в этот документ**:

| Файл | Что это | Как перенести (секретно) |
|---|---|---|
| `~/.claude/agents/local-worker.md` | персона делегата (worker) | `scp`/`tar` с сохранением прав `600` |
| `~/.claude/agents/local-checker.md` | персона локального чекера | то же |
| `~/.claude/vllm.delegate.settings.json` | **slim-профиль** для `--bg` (vLLM + env) | то же, **права `600`**, не раскрывать в общем месте |

Правильный способ перенести конфиг с уже встроенными секретами — **зашифрованным
архивом** (age/gpg) или ограниченным каналом:

```bash
tar -cf - ~/.claude/vllm.delegate.settings.json ~/.claude/agents/ \
  | age -r recipient  > migrate.age
# на новой:
age -d migrate.age > - | tar -xf - -C /
chmod 600 ~/.claude/vllm.delegate.settings.json
```

Внутри `vllm.delegate.settings.json` **единственная машинно-специфичная** величина —
`env.ANTHROPIC_BASE_URL` (адрес vLLM/LiteLLM) и `env.ANTHROPIC_API_KEY` (master-key
врат). Остальные ключи (`PARALLEL_API_KEY`, `GITHUB_TOKEN`) **переносимые** (облачные
сервисы) и работают с любой сети — их менять не нужно. Если новая машина ходит в
**другой** vLLM/LiteLLM — поправьте **только** `ANTHROPIC_BASE_URL` и `ANTHROPIC_API_KEY`
в этом файле (см. A4). **Никогда не печатайте эти значения в этом документе.**

> `vllm.delegate.settings.json` — это *slim* профиль (без интерактивных плагинов),
> предназначенный именно для `--bg`-агентов, работающих без присмотра. Основная интерактивная
> сессия остаётся на подписке и не использует этот профиль.

### A4. Удалённый vLLM / LiteLLM: адрес и достижимость

Делегаты ходят в `ANTHROPIC_BASE_URL`. Требуется один **управляющий/удалённый** сервис
(один vLLM+LiteLLM-шлюз), достижимый с новой машины:

```
vLLM (:8000)  +  LiteLLM-шлюз (Anthropic->vLLM) (:4000)
```

- Проверьте достижимость **с новой** машины:
  ```bash
  curl -s -m 5 http://<HOST>:4000/health -H "Authorization: Bearer <master-key>"
  ```
  (пустой/200-ответ — шлюз на месте; таймаут — сеть/firewall/не тот host).
- Впишите в `vllm.delegate.settings.json` → `env`:
  - `ANTHROPIC_BASE_URL=http://<HOST>:4000`
  - `ANTHROPIC_API_KEY=<master-key>` (равен `master_key`/`API_KEY` в `.env` LiteLLM).
- Если vLLM **на самой** новой машине — поднять свой стек (docker compose) и
  использовать `127.0.0.1:4000`. Если **удалённый** хост — оставить его адрес и
  проверить порт/файрвол.
- Конкурентность: GPU-потолок задаёт `--max-num-seqs` у vLLM, а пул одновременно
  запущенных **локальных** делегатов учитывает и этот MCP
  (`CLAUDE_LOCAL_DELEGATE_MAX_CONCURRENCY`, по умолчанию 16; см. B5).

### A5. Регистрация MCP в Claude (user-scope)

User-scope запись — в `~/.claude.json` → `mcpServers` (проверить, а не дописывать в
`settings.json`). Команда (надёжнее, сама впишет с абсолютным путём):

```bash
claude mcp add --scope user claude-local-delegate -- \
  python3 $REPO/server.py
# (установка задаст CLAUDE_LOCAL_DELEGATE_SETTINGS на путь slim-профиля; см. ниже)
```

Требуемый env-блок в записи (с **абсолютным** путём к server.py и к slim-профилю):
```json
{
  "claude-local-delegate": {
    "type": "stdio",
    "command": "python3",
    "args": ["$REPO/server.py"],
    "env": { "CLAUDE_LOCAL_DELEGATE_SETTINGS": "/home/$USER/.claude/vllm.delegate.settings.json" }
  }
}
```
Проверка: `claude mcp list` → `claude-local-delegate` = **Connected**.

### A6. Инсталлятор Codex (создаст симлинк, не копирует stale link)

```bash
# сначала dry-run (только покажет план, ничего не меняет):
python3 $REPO/contrib/install_codex.py
# затем применить:
python3 $REPO/contrib/install_codex.py --apply
```
Что делает инсталлятор:
- добавляет **один** stdio-сервер `claude-local-delegate` в `~/.codex/config.toml`
  (с абсолютным `$REPO/server.py` и `CLAUDE_LOCAL_DELEGATE_SETTINGS`);
- дописывает ссылку на `COORDINATION.md`/`TASK_DESIGN.md` в `~/.codex/AGENTS.md`
  и `~/.claude/CLAUDE.md`;
- **создаёт** симлинк `~/.codex/local-delegate-agents -> ~/.claude/agents`
  (не копирует — см. A0.3);
- **пишет** блоки `[mcp_servers.claude-local-delegate.tools.<tool>] output_token_limit`
  в `~/.codex/config.toml` — у Codex они ограничивают вывод каждого инструмента
  (в текущий конфиг такие блоки уже добавлены);
- **делает бэкап** каждого изменяемого файла в `*.before-local-delegate-<stamp>`
  **до** записи (основание для отката, B6);
- **откажет** (SystemExit), если запись MCP уже указывает в другое место или
  slim-профиль/персоны отсутствуют.

### A7. Переподключение обоих клиентов

- **Claude:** закрыть и открыть сессию (MCP грузится на старте) → `claude mcp list`.
- **Codex:** перезапустить (`codex` заново), чтобы подхватить `~/.codex/config.toml`.
- **Важно:** обновив server.py — перезапустить **оба** клиента; уже запущенные
  старые MCP-процессы в новых защитах не участвуют.
- При наличии нескольких машин: оба клиента должны указывать на **один**
  coordinator (A0.1) и, при раздельном env, одинаковое
  `CLAUDE_LOCAL_DELEGATE_STATE_DIR`.

### A8. Smoke-тесты (новая машина, ~5 мин)

```bash
# 1) vLLM/LiteLLM отвечает:
curl -s -m 5 http://<HOST>:4000/health -H "Authorization: Bearer <master-key>"
# 2) MCP подключён (оба клиента):
claude mcp list          # claude-local-delegate → Connected
# 3) минимальная делегация (с task_key, read/write scope):
#    в Claude-сессии:
#      project_sync(root="$REPO")
#      task_claim(key="smoke-hello", mode="write", paths=["smoke.txt"])
#      delegate_to_local(task="Создай smoke.txt с текстом 'ok'",
#                        allowed_tools="Read,Write", task_id=<id>)
#      check_delegate_status(<run_id>)   # working → done
#      get_delegate_result(<run_id>)     # 'ok'
#      task_update(status="done", note="smoke.txt=ok")
```
Критерий успеха: агент **виден** в `claude agents`, `get_delegate_result` вернул
финальный ответ, а `smoke.txt` создан с нужным содержимым.

### A9. Бэкапы и откат (rollback)

- **До** применения: инсталлятор сам создаёт `*.before-local-delegate-<stamp>`
  для `~/.codex/config.toml`, `~/.codex/AGENTS.md`, `~/.claude/CLAUDE.md`.
- **Откат:** вернуть бэкап:
  ```bash
  cp ~/.codex/config.toml.before-local-delegate-<stamp> ~/.codex/config.toml
  rm ~/.codex/local-delegate-agents        # удалить симлинк
  claude mcp remove claude-local-delegate  # убрать MCP-запись
  ```
- **State:** `~/.claude-local-delegate/coordination.sqlite3` — при откате можно
  удалить (создастся заново); но **не копировать** его между машинами (A0.1).

---

## Часть B. Повседневное использование

Общий цикл: **архитектура, границы задач, критерии приёмки и финальный ревью — на
платной (основной) модели; рутинная реализация, извлечение, поиск, проверки — на
локальной** через `claude-local-delegate` (нативные `claude --bg` агенты). Локальные
токены бесплатны; платные тратятся только на осмысленную работу.

### B0. Экономия токенов (принципы)

- **Короткие доказательства (evidence):** worker при завершении даёт *компактный*
  результат + *исполнимую* проверку, а не дамп кода/вывода. Родитель просматривает
  diff и релевантные проверки, а не весь лог.
- **Polling контрольных точек 30–60 c:** `check_delegate_status` / `check_verified_status`
  с паузой 30–60 секунд, а не спам-опрос раз в секунду (экономит и платные, и
  локальные токены, не упирается в лимиты).
- **Стоп-бесполезное:** если агент ушёл не в ту сторону — `stop_delegate` и
  **перелегировать** с более точной задачей, а не «рулить вживую» (`SendMessage`
  работает надёжно только к агенту в состоянии `blocked`). Не допускать зацикливание
  paid-модели на механике.
- **Платная модель — только архитектура/ревью:** не запускать подписочную модель
  на механической работе, которую берёт `--bg` локальный агент.

### B1. Пример дня: рефакторинг в Claude (основная сессия на подписке)

Контекст: рефакторинг модуля в репо; одна сессия Claude; делегаты — локальные.

```
1) project_sync(root="/home/$USER/Projects/<refo>")
   # посмотреть существующие task/notes; вернуть session_id и next_event cursor

2) task_claim(key="refactor-parse-split", mode="write",
              paths=["src/parse.py"], depends_on=[])
   # scope = конкретные файлы; write; зависимости, если есть, = task IDs

3) delegate_verified(
      task_id=<id>,
      task="Разбей src/parse.py: вынеси _tokenize в parse_tokens.py;
            поведение не менять; прогони test_parse.py",
      allowed_tools="Read,Edit,Write,Bash",
      scope="write")
   # цикл work -> local-checker -> revise; checker подтверждает

4) [poll 30-60s] check_verified_status(<id>)  # до 'done'

5) [основная платная модель] — читать diff, ревью, принять/отказать
   get_verified_result(<id>)  # компактный финал

6) task_update(status="done", note="parse split, tests green, diff reviewed")
```
**Заметки по scope/ключам/зависимостям:**
- `task_key` — **стабильный, описательный** (`refactor-parse-split`), переиспользовать
  при ретраях; **не** выдумывать новый ключ, чтобы обойти дедупликацию.
- `paths` — **буквальные** относительные пути; каталог резервирует **поддерево**.
- `mode="write"` — конфликтует с `write`/`read` другого владельца; **никогда не
  перекрывать** записи между агентами. Раздельные запись-агенты → раздельные
  резервирования. Read-only fan-out безопасен; write fan-out под одним
  резервированием **не изолирует** братьев.
- `depends_on` — **task IDs**, и они должны быть `done`, прежде зависимая задача
  станет активной.
- Перезарегистривать `project_sync` **до** каждого подзадания, до смены общего
  интерфейса, перед коммитом/интеграцией; хранить `next_event` и передавать
  `after_event`.

### B2. Пример дня: исследование в Codex (основная сессия на подписке)

Контекст: сбор фактов/версий/диффов; Codex не имеет нативного `SendMessage`.

```
1) project_sync(root="/home/$USER/Projects/<research>")

2) task_claim(key="research-vllm-versions", mode="read",
              paths=["docs/", "notes/"], depends_on=[])
   # research — read-only fan-out допустим

3) fan_out_to_local(task_id=<id>,
      tasks=[
        "Проверь версию vLLM по api.github.com (curl, не печатай секреты)",
        "Собери diff-заметки: что изменилось в MCP v0.6->v0.7 (по репо)",
        "Найди ссылки на LiteLLM anthropic-gateway (web search)"],
      allowed_tools="Read,Write,Bash")
   # read-only fan-out под одним резервированием — корректен

4) [poll 30-60s] check_fanout_status(<id>)  # до 'done'

5) get_fanout_result(<id>)  # компактные выводы по каждому

6) [основная paid-модель] — синтез/ревью; task_update(status="done", note="...")
```
**Разговор с остановившимся worker в Codex (нет нативного SendMessage):**
- `stop_delegate(run_id)` → дождаться `settled` (poll) →
  `continue_delegate(run_id, message, allowed_tools, task_id)` — это **явный**
  restart/fork (fork native-разговора, transcript сохраняется в новой
  background-сессии); старая сессия остаётся читаемой. Это **не** live-инъекция.
- В Claude: нативный `SendMessage` остаётся доступен для `blocked`-агентов.

### B3. Честные границы: «5 часов использования не гарантируются»

- **Не гарантируется** непрерывная/фиксированная длительность (например, «работать
  ровно 5 часов») — это не функция планировщика. MCP — **не** постоянный
  авто-расписатель; общий пул нативного ростера ограничивает количество
  одновременных spawn (избыток отклоняется на ретрай).
- **Проверяйте фактическое `/status`** (агент-роster `claude agents --json`;
  состояние `working/blocked/completed/failed/stopped`) — не предположение.
  Статичный счётчик токенов ≠ stall; `blocked` — сигнал «нужен вход», а не «завис».
- Для длительных задач: контрольные точки каждые 30–60 c + **явный**
  `task_update`/`stop_delegate`, а не ожидание таймера.

### B4. Кооперативные резервирования ≠ файловая изоляция

- **Резервирования кооперативные:** проверяются **до** spawn'а и вставляются в
  prompt worker'а; они **не являются** OS/файловой блокировкой. Прямые правки,
  shell-команды и инструменты **вне** этого MCP их обходят.
- `allowedTools` — **список разрешений**, а не файловый sandbox.
- **Для сильной изоляции** используйте **отдельные git worktrees** и **одного
  владельца** интеграции (в этой версии worktrees не сменяются и весь клиент не
  останавливается).
- `task_update(status="paused")` сохраняет paths, но **не** останавливает
  дочерние процессы; `waiting` (сброс резервирования) — только когда **все**
  дети `settled`. Никогда не сбрасывать чужой stale-резерв: отключённый supervisor
  может иметь живых worker'ов.

### B5. Новые возможности 0.8.0 (сжатие, провенанс, пул, read-only-режим, блокировки, верификатор)

- **Сжатие результатов.** `get_delegate_result` по умолчанию возвращает первые 4 +
  последние 40 строк финального ответа и sha256 полного текста (вместо всего
  текста); параметры `full=true` (всё) и `max_lines`. `get_fanout_result` сжимает
  каждый item до последних 15 строк, те же параметры. Лимиты:
  `CLAUDE_LOCAL_DELEGATE_RESULT_LINES` (40), `CLAUDE_LOCAL_DELEGATE_FANOUT_LINES` (15).
  На диске ничего не обрезается — транскрипт цел, `full=true` отдаёт всё.
- **Дешевле опрос статуса.** JSONL-транскрипт разбирается один раз за опрос (не
  дважды), сводка кешируется по (путь, размер, mtime) — опрос завершённого или
  простаивающего агента почти бесплатен.
- **Провенанс маршрутизации.** Каждый спавн пишется в
  `~/.claude-local-delegate/runs.json` (модель, base URL, sha файла настроек,
  permission mode, allowed tools, cwd, имя, время). `check_delegate_status`
  печатает строку `backend:` из этой записи и прямо сообщает, если сессия НЕ
  запущена этим сервером (платная сессия-супервизор теперь видна как «не наша»).
- **Правильный учёт пула.** Лимит локальной параллельности
  (`CLAUDE_LOCAL_DELEGATE_MAX_CONCURRENCY`, по умолчанию 16) теперь считает только
  background-сессии, записанные как локальные делегаты, плюс спавны «в полёте».
  Раньше ЛЮБАЯ сессия из `claude agents` — включая платную сессию-супервизора —
  занимала слот локальной vLLM, которой она не касалась.
- **Настоящий least privilege для read-only.** Делегация, у которой `allowed_tools`
  только read-only (Read, Grep, Glob, NotebookRead, TodoWrite), запускается с
  `--permission-mode dontAsk`, а не `bypassPermissions`: bypass ИГНОРИРУЕТ
  `--allowedTools` (anthropics/claude-code#12232) — теперь allow-список реально
  применяется, а не только декларируется. Делегации, которые пишут, по-прежнему
  по умолчанию идут в `bypassPermissions` (неприсмотренный агент, который
  спрашивает разрешение, просто зависает). Переопределение:
  `CLAUDE_LOCAL_DELEGATE_READONLY_PERMISSION_MODE`.
- **Блокировки не замораживают другой супервизор.** Read-only вызовы MCP
  (project_sync, local_backend_info, check_delegate_status, watch_delegate,
  get_delegate_result, check_fanout_status, get_fanout_result) больше не берут
  межпроцессный flock; delegate_to_local / fan_out_to_local / continue_delegate
  отпускают flock на время запуска подпроцесса `claude --bg` (до 120 с), удерживая
  свой слот «билетом» в `~/.claude-local-delegate/inflight.json`. Раньше один
  спавн замораживал чтения и спавны другого супервизора до двух минут.
- **Условный верификатор в `delegate_verified`.** Если у запуска НЕТ
  `acceptance_criteria` И делегация read-only либо `git status --porcelain` в её
  cwd чист — checker не запускается вовсе, результат возвращается как
  **UNVERIFIED PASS** с объяснением причины. `always_verify=true` заставляет
  проверку выполниться в любом случае. Когда checker запускается, в его промпт
  идут `git status --porcelain` и `git diff --stat HEAD` (реальные улики) плюс
  только последние 30 строк самоотчёта worker'а — контекст checker'а больше не
  растёт вместе с объёмом работы worker'а.
- **Бенчмарк.** `contrib/bench_concurrency.py` — автономный драйвер: общается с
  сервером по stdio и измеряет масштабирование fan-out на уровнях 1/2/4/8
  (пишет CSV).
- **Ограничение вывода в Codex.** Клиенты Codex могут ограничивать вывод каждого
  инструмента блоком `[mcp_servers.claude-local-delegate.tools.<tool>]
  output_token_limit = N` в `~/.codex/config.toml`; `contrib/install_codex.py`
  пишет эти блоки при новой установке (в текущий конфиг уже добавлены).
- **Видимый запас пула.** `project_sync` возвращает поле `pool`
  (`max` / `local_running` / `spawning` / `headroom`) по тому же учёту, что и
  допуск к спавну, и список `startable_waiting_tasks` — задачи, у которых
  блокировки уже сняты. Автозапуска по-прежнему нет: задачу переводит в `active`
  супервизор.
- **Нужен перезапуск.** Уже запущенные процессы MCP работают на старом коде.
  Всё перечисленное включается только после перезапуска Claude и Codex.

### B6. Итоговый чек-лист (кратко)

- [ ] Python 3.11+, `claude --bg`, Codex по подписке — есть.
- [ ] `$REPO` скопирован; пути абсолютные.
- [ ] Персоны + slim-профиль перенесены (права `600`, без печати ключей в доке).
- [ ] `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` поправлены под удалённый vLLM/LiteLLM; `curl /health` OK.
- [ ] MCP зарегистрирован в Claude (user-scope) и в Codex (`install_codex.py --apply`).
- [ ] Симлинк `~/.codex/local-delegate-agents` **создан** (не скопирован).
- [ ] Оба клиента перезапущены; `mcp list` = Connected.
- [ ] Smoke-тест (A8) прошёл.
- [ ] Бэкапы `*.before-local-delegate-<stamp>` на месте; откат (A9) описан.
- [ ] Active SQLite/PID **не** перенесены между машинами; один coordinator (A0.1).