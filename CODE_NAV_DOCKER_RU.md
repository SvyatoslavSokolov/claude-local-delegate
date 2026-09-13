# Быстрая навигация по коду на хосте и в Docker

`code-nav` — отдельный stdio MCP. Он не запрещает Bash и не фиксирует список
рабочих каталогов. Каждый вызов принимает один `root`, сначала читает карту
этой репы, а затем выполняет ровно один точный поиск по выбранным маршрутам.

## Что установить

- Хост: обязательный `ripgrep`; рекомендуемый `universal-ctags`.
- Python/ROS 2 контейнер: Node.js и `pyright` (`npm install -g pyright`).
- C++/ROS 2 контейнер: `clangd` и корректный `compile_commands.json`.

SCIP не обязателен для интерактивного поиска. `scip-python`/`scip-clang`
имеют смысл для сохраняемого precise-index в CI большого монорепозитория;
для этой схемы LSP + ctags проще и быстрее.

```dockerfile
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
      ripgrep universal-ctags nodejs npm clangd \
 && npm install -g pyright \
 && rm -rf /var/lib/apt/lists/*
```

Либо выполните `scripts/install-code-nav-deps.sh`. `clangd` в скрипт не
включён: он нужен только образам, где редактируется C++.

## ROS 2 workspace

Перед запуском LSP/MCP в контейнере:

```bash
source /opt/ros/$ROS_DISTRO/setup.bash
test ! -f install/setup.bash || source install/setup.bash
colcon build --cmake-args -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

Для `clangd` создайте один корневой `compile_commands.json` либо запускайте
его с `--compile-commands-dir=<build/package>`. Colcon часто создаёт отдельную
базу в `build/<package>`.

Для Pyright добавьте фактические пути активированного ROS/workspace окружения
из `$PYTHONPATH` в `pyrightconfig.json`, например:

```json
{
  "extraPaths": [
    "/opt/ros/<distro>/lib/python3.x/site-packages",
    "install/<package>/lib/python3.x/site-packages"
  ],
  "exclude": ["build", "log"]
}
```

`install/` исключается из лексического поиска, но остаётся доступным Pyright.

## Порядок агента

1. `repository_route(root, task)` — карта и конкретные `route_paths`.
2. Прямой Read или LSP для известного файла/символа.
3. `symbol_index(root, route_paths)` для outline.
4. `search_literal(root, exact_text, route_paths)` — один literal `rg`.
5. Расширение маршрута только когда предыдущий результат доказал связь.

`exhaustive=true` доступен для полного аудита. `max_results` лишь пагинирует
ответ модели и не запрещает доступ к коду.
