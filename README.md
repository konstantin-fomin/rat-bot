# Telegram message collector bot

Минимальный Telegram-бот на Python: подключается к Telegram, принимает текстовые сообщения и сохраняет их в SQLite.

## Настройка

1. Скопируйте пример окружения:

```bash
cp .env.example .env
```

2. Заполните `.env`:

```env
TELEGRAM_BOT_TOKEN=telegram_bot_token
GEMINI_API_KEY=
CHAT_ID=chat_id
DIGEST_TIME=21:00
TZ=Europe/Moscow
```

`CHAT_ID` можно оставить пустым для сохранения текстовых сообщений из всех чатов, куда добавлен бот.

## Запуск

```bash
docker compose build
docker compose up -d
```

SQLite-база будет храниться в `./data/messages.sqlite3`, логи - в `./logs/bot.log`.

## Логи

```bash
docker compose logs -f bot
tail -f logs/bot.log
```
