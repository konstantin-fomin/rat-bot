# Инструкции для ИИ-агентов

Этот файл описывает правила работы с проектом для Codex и других ИИ-агентов. Перед изменениями в коде прочитайте `README.md`, `docker-compose.yml`, `.gitignore` и `src/bot.py`.

## Назначение проекта

Проект `rat-bot` - Dockerized Telegram-бот на Python. Сейчас он делает только одно: принимает текстовые сообщения из Telegram и сохраняет их в SQLite.

Не добавляйте команды, сводки, планировщик или интеграцию с Gemini без явного запроса пользователя.

## Текущая архитектура

- Основной файл: `src/bot.py`.
- Telegram SDK: `python-telegram-bot`.
- База: SQLite через стандартный модуль `sqlite3`.
- Контейнер: `Dockerfile` на базе `python:3.13-slim`.
- Запуск: `docker-compose.yml`, сервис `bot`.
- База в контейнере: `/app/data/messages.sqlite3`.
- Логи в контейнере: `/app/logs/bot.log`.
- База на хосте: `./data/messages.sqlite3`.
- Логи на хосте: `./logs/bot.log`.

## Что нельзя коммитить

Никогда не добавляйте в git:

- `.env`;
- `data/`;
- `logs/`;
- любые SQLite-базы;
- реальные токены Telegram;
- реальные API-ключи Gemini;
- дампы сообщений и персональные данные пользователей.

Перед коммитом обязательно выполните:

```bash
git status --ignored -sb
git check-ignore -v .env data/messages.sqlite3 logs/bot.log
git diff --cached --name-only
```

В коммит должны попадать только исходники, конфигурация Docker без секретов, пример `.env.example` и документация.

## Правила изменений

- Не меняйте поведение бота без явного запроса.
- Не пересобирайте и не перезапускайте контейнер, если пользователь просит только посмотреть данные.
- Не удаляйте `./data` и `./logs`.
- Не используйте ORM: для базы применяется стандартный `sqlite3`.
- Не храните данные внутри образа. Все runtime-данные должны оставаться в примонтированных папках.
- Не добавляйте тяжелые зависимости без необходимости.
- Если меняется схема базы, сначала опишите миграционный план и риски для уже сохраненных данных.

## Проверки после изменения кода

Минимальная локальная проверка синтаксиса:

```bash
python3 -m py_compile src/bot.py
```

После изменений кода приложения обязательно пересоберите образ и перезапустите контейнер:

```bash
docker compose build
docker compose up -d
docker compose ps
```

Проверка количества сообщений:

```bash
docker compose exec bot python -c "import sqlite3; print(sqlite3.connect('/app/data/messages.sqlite3').execute('select count(*) from messages').fetchone()[0])"
```

## Проверка персистентности

Используйте эту последовательность только по запросу пользователя:

```bash
docker compose exec bot python -c "import sqlite3; print('BEFORE:', sqlite3.connect('/app/data/messages.sqlite3').execute('select count(*) from messages').fetchone()[0])"
ls -la ./data
docker compose down
docker compose up -d
sleep 5
docker compose exec bot python -c "import sqlite3; print('AFTER:', sqlite3.connect('/app/data/messages.sqlite3').execute('select count(*) from messages').fetchone()[0])"
```

Если `AFTER` меньше `BEFORE` или база пропала, остановитесь и сообщите пользователю. Ничего не удаляйте и не пересоздавайте.

## Работа с git

- Постоянная инструкция пользователя: после внесения изменений в проект коммитьте и пушьте результат без отдельного дополнительного запроса, если пользователь явно не попросил обратное.
- Перед `git add` проверьте `git status --ignored -sb`.
- Добавляйте файлы явно, а не через бездумный `git add -A`, если в рабочем дереве есть runtime-файлы.
- Перед коммитом выполните обязательные проверки из раздела "Что нельзя коммитить".
- Сообщения коммитов можно писать на русском, коротко и по делу.

## Полезные команды

Статус контейнера:

```bash
docker compose ps
```

Логи контейнера:

```bash
docker compose logs -f bot
```

Лог-файл на хосте:

```bash
tail -f logs/bot.log
```

Список уникальных участников:

```bash
docker compose exec bot python -c "import sqlite3; con=sqlite3.connect('/app/data/messages.sqlite3'); rows=con.execute('select distinct user_id, username, display_name from messages order by display_name').fetchall(); [print(f'{r[0]} | @{r[1]} | {r[2]}') for r in rows]"
```
