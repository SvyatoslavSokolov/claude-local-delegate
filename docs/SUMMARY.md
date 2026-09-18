# Навигатор по документации (Documentation Summary)

Данный каталог содержит всю нормативную, архитектурную и аналитическую документацию проекта `claude-local-delegate`.

---

## 🏛️ Архитектура и Проектирование

1. [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md)
   - **Описание:** Полная архитектура двухзвенного распределённого харнесса: **Tier 1 (Гипервизор и Архитектор)** (Claude Code, Antigravity, Codex) $\to$ **Tier 0 (Автономный кластер 4x RTX 3090 с vLLM / LiteLLM)**.
   - **Ключевые темы:** Маршрутизация моделей, изоляция рабочей директории (`cwd`), защита от рекурсии, мониторинг иерархии в реальном времени.

2. [ARCHITECTURE.md](ARCHITECTURE.md)
   - **Описание:** Системная архитектура MCP-сервера, контракты инструментов, интеграция с Serena и code-nav.

3. [REQUIREMENTS.md](REQUIREMENTS.md)
   - **Описание:** Системные требования к хосту, кластеру GPU, зависимостям Python и версиям Claude Code / Antigravity.

---

## 📋 Координация и Управление задачами

4. [ARCHITECT_BRIEF.md](ARCHITECT_BRIEF.md)
   - **Описание:** Экспресс-памятка для Архитектора (Low-Context Path). Рекомендуется для загрузки в контекст супервизора перед началом работы, чтобы не перегружать контекстное окно.

5. [COORDINATION.md](COORDINATION.md)
   - **Описание:** Координационная доска задач (`coordination.sqlite3`), жизненный цикл задач (`active`, `done`, `cancelled`), шина событий (`events`) и протоколы синхронизации проектов (`project_sync`).

6. [TASK_DESIGN.md](TASK_DESIGN.md)
   - **Описание:** Руководство по атомарной декомпозиции задач: принцип «один файл — один воркер», формулирование контрактов, измеримые критерии приёмки (`acceptance_criteria`).

7. [SUPERVISION.md](SUPERVISION.md)
   - **Описание:** Протокол супервизии: преамбула задачи, нарратив планов воркерами, неблокирующее ожидание (`get_delegate_result` с сервером 900s) без расхода платных токенов на поллинг.

---

## 🛠️ Установка и Руководства пользователя

8. [README_INSTALL_RU.md](README_INSTALL_RU.md)
   - **Описание:** Пошаговая инструкция по установке, настройке окружения (`settings.json`, `~/.claude.json`, `~/.gemini/config/mcp_config.json`) и подключению к vLLM кластеру.

9. [MIGRATION_AND_USAGE_RU.md](MIGRATION_AND_USAGE_RU.md)
   - **Описание:** Полное руководство на русском языке: переход на vLLM, настройка профилей `default_model` / `local-fast`, примеры вызовов `delegate_to_local`, `fan_out_to_local`, `delegate_verified`.

---

## 📊 Аналитика и Планы развития

10. [ANALYTICS.md](ANALYTICS.md)
    - **Описание:** Сводная аналитика по выполнению задач: потребление контекста, медианное время ответа (719s), утилизация GPU и эффективность промптов.

11. [QUALITY_LOOP_PLAN_RU.md](QUALITY_LOOP_PLAN_RU.md)
    - **Описание:** Архитектура замкнутого цикла качества (Worker $\to$ Checker $\to$ Oracle Verdict) в `delegate_verified`.

12. [GUI_DASHBOARD_PLAN_RU.md](GUI_DASHBOARD_PLAN_RU.md)
    - **Описание:** Концепция и план реализации веб-дашборда для визуализации дерева задач, нагрузки GPU и логов воркеров.

13. [PIPELINE_REVIEW_RU.md](PIPELINE_REVIEW_RU.md)
    - **Описание:** Ретроспектива и ревизия старых версий пайплайна, выявленные узкие места и решения.
