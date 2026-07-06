# Rat Bot

Telegram-бот на Python, который сохраняет сообщения и медиа из Telegram-чата в SQLite, умеет делать сводки через Gemini и показывает быструю локальную статистику чата без обращения к внешним API. Проект запускается в Docker, а рабочие данные хранятся снаружи контейнера, чтобы не теряться при пересборке образа или перезапуске контейнера.

## Возможности

- подключение к Telegram через `python-telegram-bot`;
- прием текстовых сообщений;
- сохранение фото и стикеров в таблицу `media`;
- опциональная фильтрация по `CHAT_ID`;
- сохранение сообщений в SQLite через стандартный модуль `sqlite3`;
- автоматическое создание таблиц `messages`, `media` и `daily_digests` при старте;
- запись короткой строки в лог для каждого сохраненного сообщения;
- команды `/digest`, `/roast`, `/votekick`, `/horoscope`, `/stats`;
- команда `/stats` для мгновенной статистики по сохраненным данным без Gemini;
- реакция на упоминание слова "крыса" через общий образ персонажа и сленг чата;
- in-memory память последних сгенерированных ответов для реакции на "крысу" и `/roast`, чтобы снижать повторы;
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
templates/                # скачанные шаблоны мемов Imgflip и index.json
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
GEMINI_MODEL=gemini-3.5-flash
GEMINI_FAST_MODEL=gemini-2.5-flash-lite
CHAT_ID=
DIGEST_TIME=21:00
TZ=Europe/Moscow
ROAST_COOLDOWN_MINUTES=10
ROAST_LOOKBACK_DAYS=14
VOTEKICK_COOLDOWN_MINUTES=15
VOTEKICK_DURATION_MINUTES=5
NONSENSE_CHAIN_REFRESH_MINUTES=60
NONSENSE_REACTION_CHANCE=0.03
NONSENSE_COOLDOWN_MINUTES=30
NONSENSE_MIN_MESSAGES=300
MEME_RENDER_CHANCE=0.4
SLANG_REFRESH_HOURS=24
BACKUP_CHAT_ID=
BACKUP_TIME=04:00
WEEKLY_DIGEST_TIME=21:30
MORNING_GREETING_TIME=07:53
```

Описание:

- `TELEGRAM_BOT_TOKEN` - токен Telegram-бота от BotFather.
- `GEMINI_API_KEY` - ключ Gemini для генеративных команд и реакций.
- `GEMINI_MODEL` - основная модель Gemini для генеративных команд.
- `GEMINI_FAST_MODEL` - быстрая модель Gemini только для короткой реакции на слово "крыса".
- `CHAT_ID` - ID чата, из которого сохранять сообщения. Если пустой, бот сохраняет текстовые сообщения из всех доступных ему чатов.
- `DIGEST_TIME` - время ежедневной сводки.
- `TZ` - часовой пояс контейнера.
- `ROAST_COOLDOWN_MINUTES` - кулдаун для `/roast`.
- `ROAST_LOOKBACK_DAYS` - сколько дней истории брать для `/roast`.
- `VOTEKICK_COOLDOWN_MINUTES` - кулдаун шуточного голосования для одного участника.
- `VOTEKICK_DURATION_MINUTES` - длительность голосования `/votekick`.
- `NONSENSE_*` - настройки случайных марковских реакций по истории чата.
- `MEME_RENDER_CHANCE` - шанс отправить случайную фразу как мем на сохраненном фото.
- `SLANG_REFRESH_HOURS` - период обновления словаря сленга чата.
- `BACKUP_CHAT_ID` и `BACKUP_TIME` - чат и время для автобэкапа базы.
- `WEEKLY_DIGEST_TIME` - время пятничного дайджеста недели.
- `MORNING_GREETING_TIME` - время утреннего приветствия.

`/stats` использует только SQLite и не обращается к Gemini, поэтому работает без расхода внешних API.

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
./templates -> /app/templates
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

Основные таблицы создаются автоматически при старте бота.

### `messages`

Поля:

- `message_id` - ID сообщения в Telegram;
- `chat_id` - ID чата;
- `user_id` - ID пользователя;
- `username` - username пользователя;
- `display_name` - отображаемое имя пользователя;
- `text` - текст сообщения;
- `message_datetime` - дата и время сообщения в UTC;
- `is_bot` - признак сообщения от бота;
- `created_at` - время записи в базу.

Первичный ключ:

```text
(message_id, chat_id)
```

### `media`

Поля:

- `chat_id` - ID чата;
- `message_id` - ID сообщения в Telegram;
- `user_id` - ID пользователя;
- `media_type` - тип медиа (`photo` или `sticker`);
- `file_id` и `file_unique_id` - идентификаторы файла Telegram;
- `date` - дата сообщения в UTC;
- `created_at` - время записи в базу.

### `daily_digests`

Таблица хранит дневные сводки, которые затем используются для недельного дайджеста.

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
git check-ignore -v .env data/messages.sqlite3 logs/bot.log src/__pycache__
```

Ожидается, что `.env`, `data/`, `logs/` и `__pycache__/` игнорируются.

## Текущий статус

Проверено:

- бот подключается к Telegram;
- сообщения сохраняются в SQLite;
- база лежит на хосте в `./data`;
- данные переживают `docker compose down` и повторный `docker compose up -d`.

Важно: если во время теста перезапуска в чат приходят новые сообщения, число записей после перезапуска может стать больше, чем до него. Это не ошибка персистентности.
