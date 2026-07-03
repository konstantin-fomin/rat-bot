# Rat Bot

Telegram-бот на Python, который принимает текстовые сообщения из Telegram-чата и сохраняет их в SQLite. Проект запускается в Docker, а рабочие данные хранятся снаружи контейнера, чтобы не теряться при пересборке образа или перезапуске контейнера.

Текущий этап: рабочий каркас для приема и сохранения сообщений. Команд, сводок и интеграции с нейросетью пока нет.

## Возможности

- подключение к Telegram через `python-telegram-bot`;
- прием текстовых сообщений;
- опциональная фильтрация по `CHAT_ID`;
- сохранение сообщений в SQLite через стандартный модуль `sqlite3`;
- автоматическое создание таблицы `messages` при старте;
- запись короткой строки в лог для каждого сохраненного сообщения;
- хранение базы и логов на хосте через Docker volume mounts.

## Структура проекта

```text
.
├── .env.example          # пример переменных окружения
├── .gitignore            # исключает секреты, базу и логи
├── AGENTS.md             # инструкции для ИИ-агентов
├── Dockerfile            # образ приложения
├── README.md             # документация проекта
├── docker-compose.yml    # запуск сервиса bot
├── requirements.txt      # Python-зависимости
└── src/
    ├── __init__.py
    └── bot.py            # основной код бота
```

Runtime-файлы создаются отдельно:

```text
data/messages.sqlite3     # SQLite-база на хосте
logs/bot.log              # лог-файл на хосте
.env                      # локальные секреты и настройки
```

Эти файлы не должны попадать в git.

## Переменные окружения

Скопируйте пример:

```bash
cp .env.example .env
```

Заполните `.env`:

```env
TELEGRAM_BOT_TOKEN=
GEMINI_API_KEY=
CHAT_ID=
DIGEST_TIME=21:00
TZ=Europe/Moscow
```

Описание:

- `TELEGRAM_BOT_TOKEN` - токен Telegram-бота от BotFather.
- `GEMINI_API_KEY` - ключ Gemini, зарезервирован для следующих этапов.
- `CHAT_ID` - ID чата, из которого сохранять сообщения. Если пустой, бот сохраняет текстовые сообщения из всех доступных ему чатов.
- `DIGEST_TIME` - время будущей вечерней сводки, пока не используется.
- `TZ` - часовой пояс контейнера.

## Запуск

Собрать образ:

```bash
docker compose build
```

Запустить контейнер в фоне:

```bash
docker compose up -d
```

Проверить состояние:

```bash
docker compose ps
```

Остановить и удалить контейнер без удаления данных:

```bash
docker compose down
```

## Данные и логи

SQLite-база хранится на хосте:

```text
./data/messages.sqlite3
```

Логи хранятся на хосте:

```text
./logs/bot.log
```

В `docker-compose.yml` эти пути примонтированы в контейнер:

```text
./data -> /app/data
./logs -> /app/logs
./.env -> /app/.env:ro
```

Это критично: данные не должны храниться только внутри образа или контейнера.

## Логи

Логи контейнера:

```bash
docker compose logs -f bot
```

Лог-файл на хосте:

```bash
tail -f logs/bot.log
```

## База данных

Таблица `messages` создается автоматически при старте бота.

Поля:

- `message_id` - ID сообщения в Telegram;
- `chat_id` - ID чата;
- `user_id` - ID пользователя;
- `username` - username пользователя;
- `display_name` - отображаемое имя пользователя;
- `text` - текст сообщения;
- `message_datetime` - дата и время сообщения в UTC;
- `created_at` - время записи в базу.

Первичный ключ:

```text
(message_id, chat_id)
```

## Полезные проверки

Количество сохраненных сообщений:

```bash
docker compose exec bot python -c "import sqlite3; print(sqlite3.connect('/app/data/messages.sqlite3').execute('select count(*) from messages').fetchone()[0])"
```

Список уникальных участников:

```bash
docker compose exec bot python -c "import sqlite3; con=sqlite3.connect('/app/data/messages.sqlite3'); rows=con.execute('select distinct user_id, username, display_name from messages order by display_name').fetchall(); [print(f'{r[0]} | @{r[1]} | {r[2]}') for r in rows]"
```

Проверка, что секреты и данные не попадут в git:

```bash
git status --ignored -sb
git check-ignore -v .env data/messages.sqlite3 logs/bot.log
```

Ожидается, что `.env`, `data/` и `logs/` игнорируются.

## Текущий статус

Проверено:

- бот подключается к Telegram;
- сообщения сохраняются в SQLite;
- база лежит на хосте в `./data`;
- данные переживают `docker compose down` и повторный `docker compose up -d`.

Важно: если во время теста перезапуска в чат приходят новые сообщения, число записей после перезапуска может стать больше, чем до него. Это не ошибка персистентности.
