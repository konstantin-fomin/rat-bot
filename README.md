# Rat Bot

Telegram-бот на Python, который сохраняет сообщения и медиа из Telegram-чата в SQLite, умеет делать сводки через Gemini и показывает быструю локальную статистику чата без обращения к внешним API. Проект запускается в Docker, а рабочие данные хранятся снаружи контейнера, чтобы не теряться при пересборке образа или перезапуске контейнера.

Продакшен-деплой (бесплатная VM в Google Cloud, только IPv6) и порядок обновления описаны в [DEPLOY.md](DEPLOY.md).

## Возможности

- подключение к Telegram через `python-telegram-bot`;
- прием текстовых сообщений;
- сохранение фото и стикеров в таблицу `media`;
- опциональная фильтрация по `CHAT_ID`;
- сохранение сообщений в SQLite через стандартный модуль `sqlite3`;
- автоматическое создание таблиц `messages`, `media` и `daily_digests` при старте;
- запись короткой строки в лог для каждого сохраненного сообщения;
- команды `/wisdom` (`/мудрость`), `/verse` (`/стих`), `/silence` (`/тишина`), `/roast`, `/votekick`, `/stats`, `/weekly`;
- проверенные цитаты из Библии (Новый русский перевод): мудрость дня в вечерней сводке и утреннее слово по воскресеньям;
- автоматическая сводка дня в `DIGEST_TIME` (вручную командой не вызывается);
- команда `/stats` для мгновенной статистики по сохраненным данным без Gemini;
- in-memory память последних сгенерированных ответов для `/roast`, чтобы снижать повторы;
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
GEMINI_MODEL=gemini-3.6-flash
GEMINI_DAILY_DIGEST_MODEL=gemini-3.8-flash
GEMINI_WEEKLY_DIGEST_MODEL=gemini-3.8-flash
GEMINI_ROAST_MODEL=gemini-3.8-flash
GEMINI_FALLBACK_MODELS=gemini-3.6-flash,gemini-3.5-flash-lite
GEMINI_MAX_CONCURRENT_REQUESTS=2
TELEGRAM_CONCURRENT_UPDATES=8
TELEGRAM_CONNECTION_POOL_SIZE=32
TELEGRAM_POOL_TIMEOUT=10
SQLITE_TIMEOUT_SECONDS=30
CHAT_ID=
DIGEST_TIME=21:00
TZ=Europe/Moscow
ROAST_COOLDOWN_MINUTES=10
ROAST_LOOKBACK_DAYS=14
VOTEKICK_COOLDOWN_MINUTES=15
VOTEKICK_DURATION_MINUTES=5
SLANG_REFRESH_HOURS=24
BACKUP_CHAT_ID=
BACKUP_TIME=04:00
SCRIPTURE_MORNING_TIME=09:00
SCRIPTURE_MORNING_DAYS=sun
REPENTANCE_ANNOUNCEMENT=1
```

Описание:

- `TELEGRAM_BOT_TOKEN` - токен Telegram-бота от BotFather.
- `GEMINI_API_KEY` - ключ Gemini для генеративных команд и реакций.
- `GEMINI_MODEL` - основная модель Gemini для генеративных команд.
- `GEMINI_DAILY_DIGEST_MODEL` - первая модель для ежедневной сводки (отправляется автоматически в `DIGEST_TIME`); после неё бот пробует резервные модели.
- `GEMINI_WEEKLY_DIGEST_MODEL` - первая модель для недельного дайджеста (`/weekly`); после неё бот пробует резервные модели.
- `GEMINI_ROAST_MODEL` - первая модель для команды `/roast`; после неё бот пробует резервные модели.
- `GEMINI_FALLBACK_MODELS` - модели через запятую, которые пробуются только если основная модель не ответила.
- `GEMINI_MAX_CONCURRENT_REQUESTS` - общий лимит одновременных запросов к Gemini.
- `TELEGRAM_CONCURRENT_UPDATES` - сколько апдейтов Telegram бот может обрабатывать параллельно.
- `TELEGRAM_CONNECTION_POOL_SIZE` и `TELEGRAM_POOL_TIMEOUT` - размер и таймаут пула HTTP-соединений Telegram SDK.
- `SQLITE_TIMEOUT_SECONDS` - сколько секунд SQLite ждёт снятия краткой блокировки базы.
- `CHAT_ID` - ID чата, из которого сохранять сообщения. Если пустой, бот сохраняет текстовые сообщения из всех доступных ему чатов.
- `DIGEST_TIME` - время ежедневной сводки.
- `TZ` - часовой пояс контейнера.
- `ROAST_COOLDOWN_MINUTES` - кулдаун для `/roast`.
- `ROAST_LOOKBACK_DAYS` - сколько дней истории брать для `/roast`.
- `VOTEKICK_COOLDOWN_MINUTES` - кулдаун шуточного голосования для одного участника.
- `VOTEKICK_DURATION_MINUTES` - длительность голосования `/votekick`.
- `SLANG_REFRESH_HOURS` - период обновления словаря сленга чата.
- `BACKUP_CHAT_ID` и `BACKUP_TIME` - чат и время для автобэкапа базы.
- `SCRIPTURE_MORNING_TIME` - время утреннего слова (пусто - выключено), `SCRIPTURE_MORNING_DAYS` - дни через запятую из `sun,mon,tue,wed,thu,fri,sat`.
- `SILENCE_VOTE_THRESHOLD` (5), `SILENCE_DURATION_DAYS` (7), `SILENCE_VOTE_HOURS` (24) - голосование `/тишина`: сколько голосов «за» нужно, на сколько дней бот перестаёт сам писать в чат (вечерняя сводка и воскресное слово) и сколько часов открыто голосование. Продлить можно за день до конца, бот об этом напоминает. Команды во время тишины отвечают как обычно.
- `REPENTANCE_ANNOUNCEMENT` - `0`, чтобы не отправлять разовое объявление «RAT покаялся». Оно уходит один раз, факт отправки хранится в таблице `bot_state`.

`/stats` использует только SQLite и не обращается к Gemini, поэтому работает без расхода внешних API.

## Цитаты из Библии

Цитаты лежат в `src/scripture_nrt.json`: 81 мудрость из Притч и Екклесиаста (`/wisdom`) и 59 стихов из других книг (`/verse`), всего 170 стихов. Тексты дословно взяты из Нового русского перевода на [BibleGateway](https://www.biblegateway.com/versions/New-Russian-Translation-NRT-Bible/); нумерация псалмов синодальная, как в НРП.

Gemini никогда не пишет текст цитаты: в сводке он только выбирает `id` из списка, а текст бот берёт из файла.

Проверить, что все цитаты совпадают с сайтом дословно (нужен доступ по IPv4, удобно из Google Cloud Shell):

```bash
python3 scripts/verify_scripture.py
```

Священное Писание, Новый русский перевод. Copyright © 2006 by Biblica, Inc.® Используется с разрешения. Все права защищены по всему миру. Условия Biblica позволяют цитировать до 500 стихов без отдельного разрешения.

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
