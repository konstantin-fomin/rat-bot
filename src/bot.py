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
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


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
    ("roast", "Подколоть друга по его сообщениям: /roast @username"),
    ("votekick", "Шуточное голосование за изгнание из чата"),
    ("horoscope", "Гороскоп чата на завтра"),
]

RAT_CHARACTER_INTRO = (
    "Ты — RAT, саркастичный, остроумный и слегка нахальный летописец дружеского чата. "
    "Ты подмечаешь конкретные детали и цепляешься за них, а не отделываешься общими фразами."
)

NO_CLICHES_INSTRUCTION = (
    "Избегай клише и шаблонных фраз вроде 'легендарный', 'спокойная совесть', 'триумф' — вместо этого "
    "цепляйся за конкретные слова, цифры и детали из реальных сообщений. "
    "Чем конкретнее и неожиданнее шутка — тем лучше."
)

DIGEST_PROMPT = f"""{RAT_CHARACTER_INTRO}
На основе сообщений за день собери саркастичную сводку.
Сарказм — да, но по-доброму, без оскорблений, без перехода на личности, без токсичности по внешности, здоровью, национальности и подобным болезненным темам.

Верни ТОЛЬКО валидный JSON, без markdown-обёртки (без ```), без каких-либо пояснений или текста до и после JSON, строго такой структуры:
{{
  "intro": "саркастичный пересказ дня, 2-4 предложения о том, что обсуждали",
  "quote_text": "самая абсурдная или смешная РЕАЛЬНАЯ фраза из чата дословно",
  "quote_author": "имя автора цитаты",
  "nominations": [
    {{"title": "Название номинации, например Душнила дня", "name": "Имя", "reason": "короткое обоснование"}}
  ]
}}

В "nominations" верни 2-3 объекта. Обращайся к людям по именам. Пиши на русском. Не выдумывай сообщений, которых не было.

{NO_CLICHES_INSTRUCTION}
"""

RAT_MENTION_PATTERN = re.compile(r"\bкрыс[а-яё]*\b", re.IGNORECASE)

RAT_REPLY_PROMPT_TEMPLATE = (
    "Ты — саркастичный, но добрый бот по имени RAT в дружеском Telegram-чате. "
    "Кто-то в чате только что упомянул слово 'крыса' (в каком-то виде) в сообщении: '{text}'. "
    "Придумай короткий остроумный ответ на 1-2 предложения, обыгрывающий это упоминание "
    "в шуточной саркастичной манере. Без оскорблений, без перехода на личности. "
    "Ответь только текстом реплики, без кавычек и пояснений."
)

ROAST_PROMPT_TEMPLATE = (
    RAT_CHARACTER_INTRO + "\n"
    "Вот реальные сообщения человека по имени {name} за последнее время:\n"
    "{messages}\n"
    "Придумай короткий дружеский roast (подкол) на основе ЭТИХ РЕАЛЬНЫХ сообщений, 2-4 предложения. "
    "Это должно быть смешно и точно бить в характерные детали, но БЕЗ оскорблений, БЕЗ перехода на "
    "личности, БЕЗ комментариев про внешность, здоровье, национальность и подобные болезненные темы. "
    "Это дружеский подкол, а не унижение. Ответь только текстом подкола, без пояснений.\n"
    + NO_CLICHES_INSTRUCTION
)

HOROSCOPE_PROMPT_TEMPLATE = (
    RAT_CHARACTER_INTRO + "\n"
    "Сегодня в чате обсуждали (кратко): {topics}.\n"
    "Придумай шуточный гороскоп на завтра для каждого из следующих людей: {names}.\n"
    "Каждый прогноз — 1 короткое ироничное предложение, в шуточно-астрологическом стиле "
    "(звёзды, планеты, знаки), можно отсылаться к характеру человека по его недавним репликам "
    "в чате, если это уместно. Без оскорблений и перехода на личности.\n"
    "Верни СТРОГО валидный JSON без markdown, формат:\n"
    '{{"horoscopes": [{{"name": "Имя", "forecast": "текст прогноза"}}]}}\n\n'
    + NO_CLICHES_INSTRUCTION
)

VOTEKICK_KICK_THRESHOLD = 5

VOTEKICK_KICKED_PROMPT_TEMPLATE = (
    RAT_CHARACTER_INTRO + "\n"
    "По итогам голосования чат решил шуточно «выгнать» {name} из чата "
    "(голоса: {kick_count} за, {spare_count} против). "
    "Напиши короткое смешное прощание/эпитафию, 2-3 предложения, в саркастичном, но добром тоне, "
    "без реальных оскорблений.\n"
    + NO_CLICHES_INSTRUCTION
)

VOTEKICK_SPARED_PROMPT_TEMPLATE = (
    RAT_CHARACTER_INTRO + "\n"
    "Голосование за изгнание {name} не набрало нужного числа голосов "
    "(голоса: {kick_count} за, {spare_count} против) — чат смилостивился, {name} остаётся в чате. "
    "Придумай короткий саркастичный комментарий по этому поводу, 2-3 предложения, в добром тоне, "
    "без реальных оскорблений.\n"
    + NO_CLICHES_INSTRUCTION
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


def get_roast_cooldown() -> timedelta:
    raw = os.getenv("ROAST_COOLDOWN_MINUTES", "10").strip()
    try:
        minutes = float(raw)
    except ValueError:
        minutes = 10.0

    return timedelta(minutes=minutes)


def get_votekick_cooldown() -> timedelta:
    raw = os.getenv("VOTEKICK_COOLDOWN_MINUTES", "15").strip()
    try:
        minutes = float(raw)
    except ValueError:
        minutes = 15.0

    return timedelta(minutes=minutes)


def get_votekick_duration() -> timedelta:
    raw = os.getenv("VOTEKICK_DURATION_MINUTES", "5").strip()
    try:
        minutes = float(raw)
    except ValueError:
        minutes = 5.0

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


def resolve_user_id_by_username(chat_id: int, username: str) -> int | None:
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT user_id
            FROM messages
            WHERE chat_id = ?
              AND lower(username) = lower(?)
              AND user_id IS NOT NULL
            ORDER BY message_datetime DESC
            LIMIT 1
            """,
            (chat_id, username),
        ).fetchone()

    return row["user_id"] if row else None


def fetch_latest_display_name(chat_id: int, user_id: int) -> str | None:
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT display_name
            FROM messages
            WHERE chat_id = ?
              AND user_id = ?
              AND display_name IS NOT NULL
            ORDER BY message_datetime DESC
            LIMIT 1
            """,
            (chat_id, user_id),
        ).fetchone()

    return row["display_name"] if row else None


def fetch_user_messages(
    chat_id: int,
    *,
    user_id: int | None,
    username: str | None,
    since_utc: str | None,
) -> list[sqlite3.Row]:
    if user_id is not None:
        where_clause = "chat_id = ? AND user_id = ?"
        params: list = [chat_id, user_id]
    elif username:
        where_clause = "chat_id = ? AND lower(username) = lower(?)"
        params = [chat_id, username]
    else:
        return []

    query = f"""
        SELECT user_id, username, display_name, text, message_datetime
        FROM messages
        WHERE {where_clause}
    """
    if since_utc is not None:
        query += " AND message_datetime >= ?"
        params.append(since_utc)
    query += " ORDER BY message_datetime ASC, message_id ASC"

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(query, params).fetchall()


def get_author_name(row: sqlite3.Row, name_map: dict[str, str]) -> str:
    username = row["username"]
    if username:
        mapped_name = name_map.get(username.lower())
        if mapped_name:
            return mapped_name

    return row["display_name"] or username or f"user_{row['user_id']}"


def build_messages_transcript(messages: list[sqlite3.Row], name_map: dict[str, str]) -> str:
    lines = [
        f"{get_author_name(row, name_map)}: {row['text'].strip()}"
        for row in messages
        if row["text"] and row["text"].strip()
    ]

    return "\n".join(lines)


def build_digest_request(messages: list[sqlite3.Row], name_map: dict[str, str]) -> str:
    return f"{DIGEST_PROMPT}\n\nСообщения за день:\n" + build_messages_transcript(messages, name_map)


def build_horoscope_request(
    messages: list[sqlite3.Row],
    name_map: dict[str, str],
    names: list[str],
) -> str:
    transcript = build_messages_transcript(messages, name_map)
    topics = transcript if transcript else "ничего особенного"
    return HOROSCOPE_PROMPT_TEMPLATE.format(topics=topics, names=", ".join(names))


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
            "temperature": 1.0,
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


def format_horoscope_html(data: dict) -> str:
    horoscopes = data.get("horoscopes") or []

    lines = ["🔮 <b>Гороскоп чата на завтра</b>", ""]
    for item in horoscopes:
        name = escape_html(str(item.get("name", "")).strip())
        forecast = escape_html(str(item.get("forecast", "")).strip())
        lines.append(f"<b>{name}</b> — <i>{forecast}</i>")

    return "\n".join(lines)


def build_votekick_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔪 Кикнуть", callback_data="votekick:kick"),
                InlineKeyboardButton("🙏 Помиловать", callback_data="votekick:spare"),
            ]
        ]
    )


def build_votekick_text(target_name: str, votes: dict[int, str]) -> str:
    kick_count = sum(1 for choice in votes.values() if choice == "kick")
    spare_count = sum(1 for choice in votes.values() if choice == "spare")
    return f"🔪 Голосование: выгоняем {target_name} из чата?\n\nЗа: {kick_count} | Против: {spare_count}"


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


async def handle_roast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("/roast ignored because CHAT_ID is empty")
        await message.reply_text("CHAT_ID не настроен, подкол собрать не получится")
        return

    if chat.id != allowed_chat_id:
        logger.info("/roast ignored from chat_id=%s", chat.id)
        return

    args = context.args or []
    arg_username = args[0].removeprefix("@").strip() if args else ""

    target_user_id: int | None = None
    target_username: str | None = None
    target_name_hint: str | None = None

    if arg_username:
        target_username = arg_username
        target_user_id = resolve_user_id_by_username(allowed_chat_id, arg_username)
    else:
        reply_to = message.reply_to_message
        reply_author = reply_to.from_user if reply_to else None
        if reply_author is not None:
            target_user_id = reply_author.id
            target_username = reply_author.username
            target_name_hint = reply_author.full_name
        else:
            await message.reply_text(
                "Использование: /roast @username, или ответьте на сообщение человека "
                "командой /roast (без аргумента)"
            )
            return

    since_utc = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = fetch_user_messages(
        allowed_chat_id, user_id=target_user_id, username=target_username, since_utc=since_utc
    )
    if not rows:
        rows = fetch_user_messages(
            allowed_chat_id, user_id=target_user_id, username=target_username, since_utc=None
        )

    if not rows:
        logger.info(
            "/roast target_user_id=%s target_username=%s messages_count=0",
            target_user_id,
            target_username,
        )
        await message.reply_text("Об этом человеке в базе пока ничего нет — видимо, тень в чате.")
        return

    if target_user_id is None:
        target_user_id = rows[-1]["user_id"]

    roast_key: int | str = target_user_id if target_user_id is not None else (target_username or "").lower()

    name_map = context.application.bot_data.get("name_map", {})
    display_name = None
    if target_username:
        display_name = name_map.get(target_username.lower())
    if not display_name:
        display_name = target_name_hint or rows[-1]["display_name"] or target_username or f"user_{target_user_id}"

    roast_last_at: dict[int | str, datetime] = context.application.bot_data.setdefault(
        "roast_last_at", {}
    )
    cooldown = get_roast_cooldown()
    now = datetime.now(timezone.utc)
    previous = roast_last_at.get(roast_key)
    if previous is not None and now - previous < cooldown:
        logger.info("/roast target_user_id=%s cooldown active", roast_key)
        await message.reply_text("Уже подкалывали недавно, дайте человеку выдохнуть 😅")
        return

    lines = [row["text"].strip() for row in rows if row["text"] and row["text"].strip()]
    messages_text = "\n".join(f"- {line}" for line in lines)
    prompt = ROAST_PROMPT_TEMPLATE.format(name=display_name, messages=messages_text)

    try:
        roast_text = await generate_gemini_text(prompt)
    except Exception:
        logger.exception("/roast failed to generate for target_user_id=%s", roast_key)
        await message.reply_text("Не смог придумать подкол, попробуйте позже")
        return

    roast_last_at[roast_key] = now
    await message.reply_text(roast_text)
    logger.info(
        "/roast target_user_id=%s target_username=%s messages_count=%s success",
        target_user_id,
        target_username,
        len(rows),
    )


async def handle_votekick_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("/votekick ignored because CHAT_ID is empty")
        await message.reply_text("CHAT_ID не настроен, голосование не получится")
        return

    if chat.id != allowed_chat_id:
        logger.info("/votekick ignored from chat_id=%s", chat.id)
        return

    sessions: dict[int, dict] = context.application.bot_data.setdefault("votekick_sessions", {})
    if sessions.get(allowed_chat_id) is not None:
        await message.reply_text("Уже идёт голосование, дождитесь итога")
        return

    args = context.args or []
    arg_username = args[0].removeprefix("@").strip() if args else ""

    target_user_id: int | None = None
    target_username: str | None = None
    target_name_hint: str | None = None

    if arg_username:
        target_username = arg_username
        target_user_id = resolve_user_id_by_username(allowed_chat_id, arg_username)
    else:
        reply_to = message.reply_to_message
        reply_author = reply_to.from_user if reply_to else None
        if reply_author is not None:
            target_user_id = reply_author.id
            target_username = reply_author.username
            target_name_hint = reply_author.full_name
        else:
            await message.reply_text(
                "Использование: /votekick @username, или ответьте на сообщение человека "
                "командой /votekick (без аргумента)"
            )
            return

    if target_user_id is None:
        logger.info("/votekick target not found target_username=%s", target_username)
        await message.reply_text("Об этом человеке в базе пока ничего нет — видимо, тень в чате.")
        return

    name_map = context.application.bot_data.get("name_map", {})
    display_name = None
    if target_username:
        display_name = name_map.get(target_username.lower())
    if not display_name:
        display_name = (
            target_name_hint
            or fetch_latest_display_name(allowed_chat_id, target_user_id)
            or target_username
            or f"user_{target_user_id}"
        )

    votekick_last_at: dict[int, datetime] = context.application.bot_data.setdefault(
        "votekick_last_at", {}
    )
    cooldown = get_votekick_cooldown()
    now = datetime.now(timezone.utc)
    previous = votekick_last_at.get(target_user_id)
    if previous is not None and now - previous < cooldown:
        logger.info("/votekick target_user_id=%s cooldown active", target_user_id)
        await message.reply_text("Этого недавно уже кикали (понарошку), дайте человеку передохнуть 😅")
        return

    sent_message = await message.reply_text(
        build_votekick_text(display_name, {}),
        reply_markup=build_votekick_keyboard(),
    )

    duration = get_votekick_duration()
    session: dict = {
        "target_user_id": target_user_id,
        "target_name": display_name,
        "votes": {},
        "message_id": sent_message.message_id,
        "chat_id": allowed_chat_id,
        "deadline": now + duration,
        "active": True,
    }
    session["job"] = context.application.job_queue.run_once(
        votekick_timeout_job,
        when=duration,
        chat_id=allowed_chat_id,
        name=f"votekick_finish_{allowed_chat_id}",
    )
    sessions[allowed_chat_id] = session
    votekick_last_at[target_user_id] = now

    logger.info(
        "/votekick started chat_id=%s target_user_id=%s target_name=%s",
        allowed_chat_id,
        target_user_id,
        display_name,
    )


async def handle_votekick_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return

    sessions: dict[int, dict] = context.application.bot_data.get("votekick_sessions", {})
    session = sessions.get(chat.id)

    if (
        session is None
        or not session.get("active")
        or query.message is None
        or query.message.message_id != session.get("message_id")
    ):
        await query.answer("Голосование уже завершено")
        return

    voter = query.from_user
    _, _, choice = (query.data or "").partition(":")
    if voter is None or choice not in ("kick", "spare"):
        await query.answer()
        return

    session["votes"][voter.id] = choice
    kick_count = sum(1 for v in session["votes"].values() if v == "kick")
    spare_count = sum(1 for v in session["votes"].values() if v == "spare")

    logger.info(
        "/votekick vote chat_id=%s voter_id=%s choice=%s kick=%s spare=%s",
        chat.id,
        voter.id,
        choice,
        kick_count,
        spare_count,
    )

    try:
        await query.edit_message_text(
            build_votekick_text(session["target_name"], session["votes"]),
            reply_markup=build_votekick_keyboard(),
        )
    except Exception:
        logger.exception("/votekick failed to edit message chat_id=%s", chat.id)

    await query.answer("Голос принят")

    if kick_count >= VOTEKICK_KICK_THRESHOLD:
        job = session.get("job")
        if job is not None:
            job.schedule_removal()
        await finish_votekick(context, chat.id)


async def votekick_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    logger.info("/votekick timeout reached chat_id=%s", chat_id)
    await finish_votekick(context, chat_id)


async def finish_votekick(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    sessions: dict[int, dict] = context.application.bot_data.get("votekick_sessions", {})
    session = sessions.get(chat_id)
    if session is None or not session.get("active"):
        return

    session["active"] = False
    sessions.pop(chat_id, None)

    votes: dict[int, str] = session["votes"]
    kick_count = sum(1 for v in votes.values() if v == "kick")
    spare_count = sum(1 for v in votes.values() if v == "spare")
    target_name = session["target_name"]
    kicked = kick_count >= VOTEKICK_KICK_THRESHOLD

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=session["message_id"],
            reply_markup=None,
        )
    except Exception:
        logger.exception("/votekick failed to clear keyboard chat_id=%s", chat_id)

    if kicked:
        prompt = VOTEKICK_KICKED_PROMPT_TEMPLATE.format(
            name=target_name, kick_count=kick_count, spare_count=spare_count
        )
    else:
        prompt = VOTEKICK_SPARED_PROMPT_TEMPLATE.format(
            name=target_name, kick_count=kick_count, spare_count=spare_count
        )

    verdict = "кикнут" if kicked else "помилован"
    try:
        result_text = await generate_gemini_text(prompt)
    except Exception:
        logger.exception("/votekick failed to generate outcome text chat_id=%s", chat_id)
        result_text = f"Голосование завершено: {target_name} {verdict} ({kick_count}:{spare_count})"

    await context.bot.send_message(chat_id=chat_id, text=result_text)

    logger.info(
        "/votekick finished chat_id=%s target_user_id=%s outcome=%s score=%s:%s",
        chat_id,
        session["target_user_id"],
        verdict,
        kick_count,
        spare_count,
    )


async def handle_horoscope_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("/horoscope ignored because CHAT_ID is empty")
        await message.reply_text("CHAT_ID не настроен, гороскоп собрать не получится")
        return

    if chat.id != allowed_chat_id:
        logger.info("/horoscope ignored from chat_id=%s", chat.id)
        return

    name_map = context.application.bot_data.get("name_map", {})
    names = list(name_map.values())
    if not names:
        logger.warning("/horoscope: словарик имён пуст, гороскоп составлять не для кого")
        await message.reply_text("В словарике имён пока никого нет, гороскоп составлять не для кого")
        return

    tz = get_app_timezone()
    rows = fetch_today_messages(allowed_chat_id, tz)
    prompt = build_horoscope_request(rows, name_map, names)

    try:
        raw_horoscope = await generate_gemini_text(prompt)
        horoscope_data = parse_digest_json(raw_horoscope)
    except Exception:
        logger.exception("/horoscope failed to generate")
        await message.reply_text("Звёзды сегодня не отвечают, попробуйте позже")
        return

    logger.info(
        "/horoscope generated successfully people_count=%s",
        len(horoscope_data.get("horoscopes") or []),
    )
    await message.reply_text(format_horoscope_html(horoscope_data), parse_mode="HTML")


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
    application.add_handler(CommandHandler("roast", handle_roast_command))
    application.add_handler(CommandHandler("votekick", handle_votekick_command))
    application.add_handler(CommandHandler("horoscope", handle_horoscope_command))
    application.add_handler(
        CallbackQueryHandler(handle_votekick_callback, pattern=r"^votekick:")
    )
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
