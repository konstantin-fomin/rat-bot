import html
import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from telegram import BotCommand, Message, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


BASE_DIR = Path("/app")
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
LOG_DIR = Path(os.getenv("LOG_DIR", BASE_DIR / "logs"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", BASE_DIR / "config"))
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "messages.sqlite3"))
LOG_PATH = Path(os.getenv("LOG_PATH", LOG_DIR / "bot.log"))
NAMES_PATH = Path(os.getenv("NAMES_PATH", CONFIG_DIR / "names.txt"))
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

BOT_COMMANDS = [
    ("digest", "Собрать сводку дня (спойлер: будет больно)"),
]

DIGEST_PROMPT = """Ты — саркастичный, острый на язык летописец дружеского чата.
На основе сообщений за день собери саркастичную сводку.
Сарказм — да, но по-доброму, без оскорблений, без перехода на личности, без токсичности по внешности, здоровью, национальности и подобным болезненным темам.

Верни ТОЛЬКО валидный JSON, без markdown-обёртки (без ```), без каких-либо пояснений или текста до и после JSON, строго такой структуры:
{
  "intro": "саркастичный пересказ дня, 2-4 предложения о том, что обсуждали",
  "quote_text": "самая абсурдная или смешная РЕАЛЬНАЯ фраза из чата дословно",
  "quote_author": "имя автора цитаты",
  "nominations": [
    {"title": "Название номинации, например Душнила дня", "name": "Имя", "reason": "короткое обоснование"}
  ]
}

В "nominations" верни 2-3 объекта. Обращайся к людям по именам. Пиши на русском. Не выдумывай сообщений, которых не было.
"""

RAT_MENTION_PATTERN = re.compile(r"\bкрыс[а-яё]*\b", re.IGNORECASE)

RAT_REPLY_PROMPT_TEMPLATE = (
    "Ты — саркастичный, но добрый бот по имени RAT в дружеском Telegram-чате. "
    "Кто-то в чате только что упомянул слово 'крыса' (в каком-то виде) в сообщении: '{text}'. "
    "Придумай короткий остроумный ответ на 1-2 предложения, обыгрывающий это упоминание "
    "в шуточной саркастичной манере. Без оскорблений, без перехода на личности. "
    "Ответь только текстом реплики, без кавычек и пояснений."
)


logger = logging.getLogger(__name__)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
        ],
    )


def get_allowed_chat_id() -> int | None:
    chat_id = os.getenv("CHAT_ID", "").strip()
    if not chat_id:
        return None

    try:
        return int(chat_id)
    except ValueError as exc:
        raise RuntimeError("CHAT_ID must be an integer or empty") from exc


def get_digest_time(tz: ZoneInfo) -> time | None:
    raw = os.getenv("DIGEST_TIME", "").strip()
    if not raw:
        return None

    try:
        hour_str, minute_str = raw.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise RuntimeError(f"DIGEST_TIME must be in HH:MM format, got {raw!r}") from exc

    return time(hour=hour, minute=minute, tzinfo=tz)


def get_rat_reply_cooldown() -> timedelta:
    raw = os.getenv("RAT_REPLY_COOLDOWN_MINUTES", "7").strip()
    try:
        minutes = float(raw)
    except ValueError:
        minutes = 7.0

    return timedelta(minutes=minutes)


def get_app_timezone() -> ZoneInfo:
    timezone_name = os.getenv("TZ", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning("unknown TZ=%s; falling back to UTC", timezone_name)
        return ZoneInfo("UTC")


def load_name_map() -> dict[str, str]:
    if not NAMES_PATH.exists():
        logger.info("names file does not exist: %s", NAMES_PATH)
        return {}

    names: dict[str, str] = {}
    for line_number, line in enumerate(NAMES_PATH.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if "=" not in stripped:
            logger.warning("skip invalid names line %s: %r", line_number, line)
            continue

        username, name = (part.strip() for part in stripped.split("=", 1))
        username = username.removeprefix("@").strip().lower()
        if username and name:
            names[username] = name

    logger.info("loaded %s names from %s", len(names), NAMES_PATH)
    return names


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                display_name TEXT,
                text TEXT NOT NULL,
                message_datetime TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, chat_id)
            )
            """
        )
        connection.commit()


def get_today_bounds_utc(tz: ZoneInfo) -> tuple[str, str]:
    now_local = datetime.now(tz)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    end_local = datetime.combine(now_local.date(), time.max, tzinfo=tz)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def fetch_today_messages(chat_id: int, tz: ZoneInfo) -> list[sqlite3.Row]:
    start_utc, end_utc = get_today_bounds_utc(tz)

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT user_id, username, display_name, text, message_datetime
            FROM messages
            WHERE chat_id = ?
              AND message_datetime >= ?
              AND message_datetime <= ?
            ORDER BY message_datetime ASC, message_id ASC
            """,
            (chat_id, start_utc, end_utc),
        ).fetchall()


def get_author_name(row: sqlite3.Row, name_map: dict[str, str]) -> str:
    username = row["username"]
    if username:
        mapped_name = name_map.get(username.lower())
        if mapped_name:
            return mapped_name

    return row["display_name"] or username or f"user_{row['user_id']}"


def build_digest_request(messages: list[sqlite3.Row], name_map: dict[str, str]) -> str:
    lines = [
        f"{get_author_name(row, name_map)}: {row['text'].strip()}"
        for row in messages
        if row["text"] and row["text"].strip()
    ]

    return f"{DIGEST_PROMPT}\n\nСообщения за день:\n" + "\n".join(lines)


async def generate_gemini_text(prompt: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required")

    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
    url = GEMINI_ENDPOINT.format(model=model)
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt,
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.8,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(url, headers=headers, json=payload)

    if response.status_code >= 400:
        raise RuntimeError(f"Gemini API error {response.status_code}: {response.text[:500]}")

    data = response.json()
    candidates = data.get("candidates") or []
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    text = "\n".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise RuntimeError(f"Gemini API returned empty text: {data}")

    return text


def parse_digest_json(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*\n", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()

    return json.loads(cleaned)


def escape_html(text: str) -> str:
    return html.escape(text, quote=False)


def format_digest_html(data: dict) -> str:
    intro = escape_html(str(data.get("intro", "")).strip())
    quote_text = escape_html(str(data.get("quote_text", "")).strip())
    quote_author = escape_html(str(data.get("quote_author", "")).strip())
    nominations = data.get("nominations") or []

    lines = [
        "🐀 <b>Сводка дня</b>",
        "",
        intro,
        "",
        "<b>Цитата дня:</b>",
        f"<blockquote>{quote_text}</blockquote>",
        f"<i>— {quote_author}</i>",
        "",
        "<b>Номинации дня:</b>",
    ]

    for nomination in nominations:
        title = escape_html(str(nomination.get("title", "")).strip())
        name = escape_html(str(nomination.get("name", "")).strip())
        reason = escape_html(str(nomination.get("reason", "")).strip())
        lines.append(f"<b>{title}</b> — {name} — {reason}")

    return "\n".join(lines)


async def maybe_reply_to_rat_mention(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    text = message.text
    if not text or not RAT_MENTION_PATTERN.search(text):
        return

    author = message.from_user
    if author is not None and author.is_bot:
        return

    last_triggered_at: dict[int, datetime] = context.application.bot_data.setdefault(
        "rat_reply_last_at", {}
    )
    chat_id = message.chat_id
    now = datetime.now(timezone.utc)
    cooldown = get_rat_reply_cooldown()
    previous = last_triggered_at.get(chat_id)
    if previous is not None and now - previous < cooldown:
        return

    prompt = RAT_REPLY_PROMPT_TEMPLATE.format(text=text)
    try:
        reply = await generate_gemini_text(prompt)
    except Exception:
        logger.exception("rat mention reply: Gemini error")
        return

    last_triggered_at[chat_id] = now
    await message.reply_text(reply)
    logger.info("rat mention triggered text=%r reply=%r", text[:120], reply[:120])


def save_message(
    *,
    message_id: int,
    chat_id: int,
    user_id: int | None,
    username: str | None,
    display_name: str | None,
    text: str,
    message_datetime: str,
) -> None:
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO messages (
                message_id,
                chat_id,
                user_id,
                username,
                display_name,
                text,
                message_datetime
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                chat_id,
                user_id,
                username,
                display_name,
                text,
                message_datetime,
            ),
        )
        connection.commit()


async def handle_text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    allowed_chat_id = get_allowed_chat_id()

    if message is None or chat is None or message.text is None:
        return

    if allowed_chat_id is not None and chat.id != allowed_chat_id:
        return

    message_datetime = message.date.astimezone(timezone.utc).isoformat()
    display_name = user.full_name if user else None

    save_message(
        message_id=message.message_id,
        chat_id=chat.id,
        user_id=user.id if user else None,
        username=user.username if user else None,
        display_name=display_name,
        text=message.text,
        message_datetime=message_datetime,
    )

    logger.info(
        "saved message chat_id=%s message_id=%s user_id=%s text=%r",
        chat.id,
        message.message_id,
        user.id if user else None,
        message.text[:120],
    )

    await maybe_reply_to_rat_mention(message, context)


async def handle_digest_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("/digest ignored because CHAT_ID is empty")
        await message.reply_text("CHAT_ID не настроен, сводку собрать не получится")
        return

    if chat.id != allowed_chat_id:
        logger.info("/digest ignored from chat_id=%s", chat.id)
        return

    tz = get_app_timezone()
    name_map = context.application.bot_data.get("name_map", {})
    rows = fetch_today_messages(allowed_chat_id, tz)
    logger.info("/digest called chat_id=%s messages_count=%s", allowed_chat_id, len(rows))

    if not rows:
        await message.reply_text("За сегодня сообщений нет. Даже сарказму не из чего вырасти.")
        return

    prompt = build_digest_request(rows, name_map)

    try:
        raw_digest = await generate_gemini_text(prompt)
        digest_data = parse_digest_json(raw_digest)
    except Exception:
        logger.exception("failed to generate digest with Gemini")
        await message.reply_text("Не смог собрать сводку, попробуйте позже")
        return

    logger.info("/digest generated successfully chat_id=%s", allowed_chat_id)
    await message.reply_text(format_digest_html(digest_data), parse_mode="HTML")


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("автосводка пропущена: CHAT_ID не настроен")
        return

    tz = get_app_timezone()
    name_map = context.application.bot_data.get("name_map", {})
    rows = fetch_today_messages(allowed_chat_id, tz)

    if not rows:
        logger.info("автосводка пропущена: нет сообщений за день")
        return

    prompt = build_digest_request(rows, name_map)

    try:
        raw_digest = await generate_gemini_text(prompt)
        digest_data = parse_digest_json(raw_digest)
    except Exception:
        logger.exception("автосводка: ошибка при обращении к Gemini")
        return

    await context.bot.send_message(
        chat_id=allowed_chat_id,
        text=format_digest_html(digest_data),
        parse_mode="HTML",
    )
    logger.info(
        "автосводка отправлена chat_id=%s messages_count=%s",
        allowed_chat_id,
        len(rows),
    )


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [BotCommand(name, description) for name, description in BOT_COMMANDS]
    )
    logger.info("bot commands registered: %s", [name for name, _ in BOT_COMMANDS])


def main() -> None:
    setup_logging()
    init_db()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("CHAT_ID is empty; bot will save text messages from all chats")
    else:
        logger.info("bot will save text messages from chat_id=%s", allowed_chat_id)

    application = Application.builder().token(token).post_init(post_init).build()
    application.bot_data["name_map"] = load_name_map()
    application.add_handler(CommandHandler("digest", handle_digest_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    tz = get_app_timezone()
    digest_time = get_digest_time(tz)
    if allowed_chat_id is not None and digest_time is not None:
        application.job_queue.run_daily(send_daily_digest, time=digest_time, name="daily_digest")
        logger.info("автосводка запланирована на %s (%s)", digest_time, tz)
    else:
        logger.warning(
            "автосводка не запланирована: CHAT_ID=%s DIGEST_TIME=%s",
            allowed_chat_id,
            os.getenv("DIGEST_TIME", ""),
        )

    logger.info("bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
