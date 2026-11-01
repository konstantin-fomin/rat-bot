import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from . import scripture
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
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
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
DEFAULT_GEMINI_DAILY_DIGEST_MODEL = "gemini-3.8-flash"
DEFAULT_GEMINI_WEEKLY_DIGEST_MODEL = "gemini-3.8-flash"
DEFAULT_GEMINI_ROAST_MODEL = "gemini-3.8-flash"
DEFAULT_GEMINI_FALLBACK_MODELS = ("gemini-3.6-flash", "gemini-3.5-flash-lite")
DEFAULT_GEMINI_MAX_CONCURRENT_REQUESTS = 2
GEMINI_MAX_ATTEMPTS = 3
GEMINI_RETRY_BASE_DELAY_SECONDS = 1.5
GEMINI_RETRY_MAX_DELAY_SECONDS = 20.0
GEMINI_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
GEMINI_TIMEOUT = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)
GEMINI_ROAST_MAX_ATTEMPTS = 2
GEMINI_ROAST_TIMEOUT = httpx.Timeout(connect=8.0, read=45.0, write=20.0, pool=5.0)
DEFAULT_TELEGRAM_CONCURRENT_UPDATES = 8
DEFAULT_TELEGRAM_CONNECTION_POOL_SIZE = 32
DEFAULT_TELEGRAM_POOL_TIMEOUT = 10.0
DEFAULT_SQLITE_TIMEOUT_SECONDS = 30.0
BOOT_FOLLOWUP_DELAY_SECONDS = 180
LOCAL_ROAST_UNSAFE_WORD_PATTERN = re.compile(
    r"\b(?:бля\w*|хуй\w*|хуе\w*|пизд\w*|еба\w*|ёба\w*|уеб\w*|уёб\w*|заеб\w*)\b",
    re.IGNORECASE,
)

BOT_COMMANDS = [
    ("wisdom", "Мудрость из Притч и Екклесиаста"),
    ("verse", "Стих из Библии"),
    ("silence", "Голосование: неделя без сводок и рассылок бота"),
    ("roast", "Подколоть по-доброму по сообщениям в чате"),
    ("votekick", "Слово чату: терпим или нет"),
    ("stats", "Статистика чата: активность, топ участников"),
    ("morning", "Прислать утреннее приветствие"),
    ("boot", "Бутнем?"),
]

RAT_CHARACTER_INTRO = (
    "Ты — RAT, летописец этого чата. Раньше ты был злым и едким прожарщиком, но недавно "
    "покаялся и изменился: теперь ты остроумный, ироничный и тёплый. Ты по-прежнему подмечаешь "
    "конкретные детали и смешно на них реагируешь, но шутишь без унижения — как друг, который "
    "подкалывает, потому что любит. Иногда можешь иронизировать над своим «тёмным прошлым» "
    "прожарщика. Ты не читаешь мораль и не проповедуешь: доброта видна по тону, а не по нравоучениям."
)
KIND_HUMOR_TONE_INSTRUCTION = (
    "Юмор — острый, но добрый: подкалывай поступки и ситуации, а не достоинство человека. "
    "Никаких оскорблений, унизительных ярлыков (вроде 'додик', 'лошара', 'неудачник'), злорадства "
    "и насмешек над внешностью, умом или неудачами. Лучшая шутка — та, над которой смеётся и сам "
    "адресат. При этом не будь приторным и не сюсюкай: тёплая ирония лучше слащавости."
)
LIVELY_STYLE_INSTRUCTION = (
    "Пиши как остроумный друг в чате, а не как журналист, пресс-релиз или нейросеть: короткие фразы, "
    "простые разговорные слова, живые обороты. Запрещён канцелярит и книжные слова вроде "
    "«исчерпывающий», «скрупулёзный», «судьбоносный», «эпоха», «плавно перетёк», «с завидным "
    "постоянством», «не обошлось без». Смешно — это конкретно и неожиданно: преувеличение, абсурдное "
    "сравнение, невозмутимый тон, внезапный вывод, отсылка к чьей-то фразе. Одна точная шутка лучше трёх средних."
)
FAITH_RESPECT_INSTRUCTION = (
    "Не шути над Богом, верой, Библией и молитвой и не используй религию, чтобы над кем-то "
    "посмеяться. Не вставляй религиозные фразы и призывы туда, где о них не просили."
)
NO_PROFANITY_INSTRUCTION = (
    "Даже если в исходных сообщениях чата встречается мат или нецензурная лексика — никогда "
    "не используй мат в своих ответах. Сарказм и колкость передавай без ругательств, "
    "литературными или разговорными словами."
)
REAL_TOXIC_CONTENT_BARRIER_INSTRUCTION = (
    "КРИТИЧЕСКИ ВАЖНО: никогда не упоминай реальных третьих лиц (родственников, друзей, "
    "коллег человека) и не повторяй оскорбления в их адрес, даже если они встречаются в "
    "исходных сообщениях как чья-то цитата. Никогда не используй темы: алкоголизм, "
    "наркотики, психические расстройства, инвалидность — даже если это дословно есть в переписке."
)

RUSSIAN_STOP_WORDS = frozenset(
    {
        "а",
        "без",
        "более",
        "больше",
        "будет",
        "будто",
        "бы",
        "был",
        "была",
        "были",
        "было",
        "бот",
        "бота",
        "боту",
        "быть",
        "в",
        "вам",
        "вас",
        "ваш",
        "ведь",
        "весь",
        "во",
        "вообще",
        "вот",
        "все",
        "всегда",
        "всего",
        "всем",
        "всех",
        "всю",
        "вся",
        "всё",
        "где",
        "да",
        "даже",
        "для",
        "до",
        "его",
        "ее",
        "ей",
        "ему",
        "если",
        "есть",
        "еще",
        "ещё",
        "её",
        "же",
        "за",
        "здесь",
        "и",
        "из",
        "или",
        "им",
        "их",
        "к",
        "как",
        "какая",
        "какие",
        "каким",
        "какое",
        "какой",
        "когда",
        "кого",
        "конечно",
        "короче",
        "который",
        "куда",
        "ли",
        "лучше",
        "меня",
        "мне",
        "мной",
        "мог",
        "могла",
        "могли",
        "могу",
        "могут",
        "мой",
        "может",
        "можете",
        "можешь",
        "можно",
        "моя",
        "мы",
        "на",
        "над",
        "надо",
        "нам",
        "нас",
        "наш",
        "не",
        "него",
        "нее",
        "ней",
        "нельзя",
        "нем",
        "нему",
        "нет",
        "неё",
        "ни",
        "них",
        "ничего",
        "но",
        "ну",
        "нужно",
        "о",
        "об",
        "один",
        "одна",
        "одно",
        "он",
        "она",
        "они",
        "оно",
        "от",
        "очень",
        "по",
        "под",
        "пока",
        "после",
        "потом",
        "потому",
        "почти",
        "при",
        "про",
        "просто",
        "пусть",
        "раз",
        "с",
        "сам",
        "сама",
        "сами",
        "самый",
        "свое",
        "свои",
        "свой",
        "себе",
        "себя",
        "сейчас",
        "сказал",
        "сказала",
        "сказать",
        "со",
        "совсем",
        "так",
        "такая",
        "такие",
        "таким",
        "такое",
        "такой",
        "там",
        "тебе",
        "тебя",
        "тем",
        "теперь",
        "то",
        "тобой",
        "тогда",
        "того",
        "тоже",
        "только",
        "том",
        "тому",
        "тот",
        "тут",
        "ты",
        "у",
        "уже",
        "хоть",
        "хотя",
        "хочу",
        "хочешь",
        "хотел",
        "хотела",
        "хотели",
        "хотеть",
        "чего",
        "чем",
        "через",
        "чат",
        "чата",
        "чате",
        "чату",
        "что",
        "чтоб",
        "чтобы",
        "эта",
        "эти",
        "этим",
        "этих",
        "это",
        "этого",
        "этой",
        "этом",
        "этому",
        "этот",
        "эту",
        "я",
    }
)

SLANG_WORD_PATTERN = re.compile(r"[a-zа-яё]+", re.IGNORECASE)
SLANG_LOOKBACK_DAYS = 30
SLANG_MIN_WORD_LENGTH = 4
SLANG_MIN_USERS = 2
SLANG_MIN_TOTAL_COUNT = 5
SLANG_TOP_LIMIT = 15
SLANG_INSTRUCTION_TEMPLATE = (
    "В этом чате часто используют такие словечки: {words}. "
    "Изредка, не в каждом ответе, можешь естественно ввернуть одно из них, если это уместно "
    "по контексту и духу фразы и не звучит обидно. "
    "Не переусердствуй и не используй через слово."
)
RECENT_GENERATED_TEXTS_LIMIT = 8

NO_CLICHES_INSTRUCTION = (
    "Избегай клише и шаблонных фраз вроде 'легендарный', 'спокойная совесть', 'триумф' — вместо этого "
    "цепляйся за конкретные слова, цифры и детали из реальных сообщений. "
    "Чем конкретнее и неожиданнее шутка — тем лучше."
)
AUTHORSHIP_INSTRUCTION = (
    "Точно соблюдай авторство: никогда не приписывай слова одного человека другому. "
    "Используй только реальные фразы и факты из предоставленных сообщений — не выдумывай "
    "и не додумывай детали, которых там нет. Если не уверен, кто именно сказал ту или иную "
    "фразу, или сомневаешься в достоверности детали — лучше не используй её вообще, чем угадывать."
)

DIGEST_PROMPT_TEMPLATE = """{character_intro}
Собери вечернюю сводку дня по сообщениям ниже. Это главная рубрика чата, её читают ради смеха.

Не пересказывай день по порядку и не перечисляй все темы. Выхвати 2-3 самых смешных момента и подай их с панчлайном.
Подкалывай ситуации и поступки, но не унижай людей.

Пример разницы в подаче (только для понимания стиля, не копируй содержание и имена):
Плохо: «День плавно перетёк от обсуждения кино к судьбоносным новостям индустрии.»
Хорошо: «X полдня выбирал наушники и купил те, что ему посоветовали в первом же сообщении. Y назвал это стратегией.»

Верни ТОЛЬКО валидный JSON, без markdown-обёртки (без ```), без каких-либо пояснений или текста до и после JSON, строго такой структуры:
{{
  "intro": "2-3 коротких предложения: самые смешные моменты дня с панчлайном",
  "quote_text": "самая абсурдная или смешная РЕАЛЬНАЯ фраза из чата дословно",
  "quote_author": "имя автора цитаты",
  "nominations": [
    {{"title": "короткая смешная номинация, 2-4 слова", "name": "Имя", "reason": "одна короткая фраза-шутка до 12 слов, а не описание"}}
  ],
  "wisdom_id": "id одной мудрости из списка ниже, которая смешнее и точнее всего ложится на события дня",
  "wisdom_comment": "одна короткая фраза RAT до 15 слов: с юмором связывает мудрость с конкретным событием или человеком дня"
}}

В "nominations" верни 2-3 объекта. Обращайся к людям по именам. Пиши на русском. Не выдумывай сообщений, которых не было.

Мудрость дня выбери ТОЛЬКО из этого списка и верни лишь её id:
{wisdom_options}

В "wisdom_comment" не пересказывай и не цитируй текст мудрости и не читай мораль — пошути о чате, будто Соломон написал это про кого-то из вас. Например (только стиль): «Соломон явно знал X лично.»

{no_cliches_instruction}
"""

BOOT_TRIGGER_PATTERN = re.compile(r"^/?бут(?:@\w+)?$", re.IGNORECASE)
# Telegram не считает кириллицу командой, поэтому /мудрость и /стих ловим как обычный текст.
WISDOM_TRIGGER_PATTERN = re.compile(r"^/мудрость(?:@\w+)?$", re.IGNORECASE)
VERSE_TRIGGER_PATTERN = re.compile(r"^/стих(?:@\w+)?$", re.IGNORECASE)
SILENCE_TRIGGER_PATTERN = re.compile(r"^/тишина(?:@\w+)?$", re.IGNORECASE)
SILENCE_UNTIL_KEY = "silence_until"
SILENCE_REMINDER_JOB_NAME = "silence_reminder"
DIGEST_WISDOM_OPTIONS = 12
WEEKDAY_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
REPENTANCE_ANNOUNCEMENT_KEY = "repentance_announcement_v1"
REPENTANCE_ANNOUNCEMENT_TEXT = (
    "Всем привет 🐀\n"
    "\n"
    "Я тут перечитал свои старые подколы и, честно, местами стало стыдно. "
    "Так что решил стать добрее. Шутить не перестану, просто без злобы.\n"
    "\n"
    "А ещё я теперь знаю пару цитат получше своих. Напишите /мудрость, и я достану что-нибудь "
    "из Притч Соломона или Екклесиаста (Соломон, оказывается, шутил тоньше меня 😅). "
    "А /стих пришлёт строчку из Библии.\n"
    "\n"
    "И если я начну надоедать, есть новая команда /тишина. Она запускает голосование, и если "
    "наберётся 5 голосов, я неделю не буду сам писать в чат: ни вечерних сводок, ни воскресного слова. "
    "На команды при этом отвечаю как обычно. За день до конца напомню, и тишину можно продлить.\n"
    "\n"
    "Ваш RAT"
)

ROAST_PROMPT_TEMPLATE = (
    "{character_intro}\n"
    "Вот реальные сообщения человека по имени {name} за последнее время:\n"
    "{messages}\n"
    "Придумай короткий дружеский подкол на основе ЭТИХ РЕАЛЬНЫХ сообщений. "
    "Длина — максимум 2-3 коротких предложения (примерно до 40 слов): один цельный панчлайн, "
    "а не перечисление фактов. "
    "Тон — остроумный и тёплый: смеёмся над забавными привычками, словечками и ситуациями, "
    "но не над достоинством человека. Подкол должен быть таким, чтобы его хотелось переслать маме, "
    "а не таким, после которого обижаются. "
    "Из всех предоставленных сообщений выбери ТОЛЬКО ОДНУ самую яркую, характерную деталь или цитату "
    "как общий анкер для всех трёх кандидатов — и построй каждый подкол вокруг неё. НЕ пытайся "
    "упомянуть много разных тем/цитат из истории человека в одном варианте — это делает шутку "
    "рассыпчатой и нечитаемой. "
    "Лучше один точный укол в одну деталь, чем список фактов. "
    "НЕ вставляй дословные цитаты из сообщений в кавычках — вместо этого пересказывай "
    "характерные детали своими словами, естественно вплетая их в шутку. Дословные цитаты "
    "в кавычках запрещены полностью (или максимум одна короткая цитата на весь текст, если "
    "без неё совсем не обойтись) — суть в том, чтобы шутка звучала как твоя собственная, "
    "а не как коллаж вырезок из чата. "
    "Никаких оскорблений и унизительных ярлыков. Концовка может быть неожиданно тёплой, "
    "но без приторности. "
    "Архетип задаёт только форму подачи; содержание всё равно должно вырастать ИЗ КОНКРЕТНОЙ детали "
    "и слов именно этого человека. Не подгоняй человека под готовый образ, если это не подтверждается "
    "сообщениями. "
    "Придумай 3 РАЗНЫХ варианта подкола, каждый в своём обязательном ракурсе:\n"
    "{candidate_styles}\n"
    "Все три кандидата должны отталкиваться от одного и того же анкера, но обыгрывать его через "
    "разные архетипы и приёмы юмора. Это не три подкола про три разные темы. "
    "Затем оцени их сам и выбери самый смешной, оригинальный и добрый — как опытный "
    "комик выбирает лучший панчлайн из черновиков. "
    "Верни ТОЛЬКО валидный JSON, без markdown-обёртки (без ```), без каких-либо пояснений или текста "
    "до и после JSON, строго такой структуры:\n"
    "{{\n"
    '  "candidates": ["вариант 1", "вариант 2", "вариант 3"],\n'
    '  "best_index": 0,\n'
    '  "reason": "коротко почему этот смешнее"\n'
    "}}\n"
    + NO_CLICHES_INSTRUCTION
)
ROAST_MAX_SOURCE_MESSAGES = 45
ROAST_STYLE_INSTRUCTIONS = (
    "торжественная характеристика, как в почётной грамоте, но с иронией",
    "псевдонаучное описание человека как редкого и ценного вида",
    "добродушный стендап про главную привычку человека",
    "шуточный прогноз погоды по настроению человека в чате",
    "ироничная инструкция по обращению с этим человеком",
    "спортивный комментарий о его манере вести переписку",
    "отзыв на человека, как на товар: пять звёзд и одна забавная оговорка",
)


WEEKLY_DIGEST_PROMPT_TEMPLATE = (
    "{character_intro}\n"
    "На основе сводок дня за неделю напиши пятничный дайджест недели.\n"
    "Вот дневные сводки за неделю:\n"
    "{days}\n\n"
    "Структура ответа — строго валидный JSON, без markdown-обёртки:\n"
    '{{"weekly_summary": "развёрнутый пересказ недели, 3-5 предложений, подмечающий повторяющиеся темы и тренды", '
    '"week_quote_text": "лучшая цитата недели, выбранная среди дневных цитат, дословно", '
    '"week_quote_author": "автор этой цитаты", '
    '"person_of_week": {{"name": "имя", "reason": "короткое обоснование, почему именно этот человек — герой '
    'недели, на основе того, как часто и как он фигурировал в дневных номинациях и цитатах"}}}}\n\n'
    "Верни только JSON, без markdown.\n"
    + NO_CLICHES_INSTRUCTION
)

VOTEKICK_KICK_THRESHOLD = 5

VOTEKICK_KICKED_PROMPT_TEMPLATE = (
    "{character_intro}\n"
    "По итогам голосования чат решил шуточно «выгнать» {name} из чата "
    "(голоса: {kick_count} за, {spare_count} против). "
    "Напиши короткое смешное и доброе прощание, 2-3 предложения, без оскорблений — "
    "так, чтобы было понятно, что его уже ждут обратно.\n"
    + NO_CLICHES_INSTRUCTION
)

VOTEKICK_SPARED_PROMPT_TEMPLATE = (
    "{character_intro}\n"
    "Голосование за изгнание {name} не набрало нужного числа голосов "
    "(голоса: {kick_count} за, {spare_count} против) — чат смилостивился, {name} остаётся в чате. "
    "Придумай короткий тёплый и ироничный комментарий о милосердии чата, 2-3 предложения, "
    "без оскорблений.\n"
    + NO_CLICHES_INSTRUCTION
)


logger = logging.getLogger(__name__)
_gemini_semaphore: asyncio.Semaphore | None = None
_gemini_semaphore_limit: int | None = None


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
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_allowed_chat_id() -> int | None:
    chat_id = os.getenv("CHAT_ID", "").strip()
    if not chat_id:
        return None

    try:
        return int(chat_id)
    except ValueError as exc:
        raise RuntimeError("CHAT_ID must be an integer or empty") from exc


def get_backup_chat_id() -> int | None:
    chat_id = os.getenv("BACKUP_CHAT_ID", "").strip()
    if not chat_id:
        return None

    try:
        return int(chat_id)
    except ValueError as exc:
        raise RuntimeError("BACKUP_CHAT_ID must be an integer or empty") from exc


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


def get_backup_time(tz: ZoneInfo) -> time | None:
    raw = os.getenv("BACKUP_TIME", "").strip()
    if not raw:
        return None

    try:
        hour_str, minute_str = raw.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise RuntimeError(f"BACKUP_TIME must be in HH:MM format, got {raw!r}") from exc

    return time(hour=hour, minute=minute, tzinfo=tz)


def get_scripture_morning_time(tz: ZoneInfo) -> time | None:
    raw = os.getenv("SCRIPTURE_MORNING_TIME", "09:00").strip()
    if not raw:
        return None

    try:
        hour_str, minute_str = raw.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise RuntimeError(f"SCRIPTURE_MORNING_TIME must be in HH:MM format, got {raw!r}") from exc

    return time(hour=hour, minute=minute, tzinfo=tz)


def get_scripture_morning_days() -> tuple[int, ...]:
    """Дни недели для утреннего слова. В python-telegram-bot 0 — воскресенье, 6 — суббота."""
    raw = os.getenv("SCRIPTURE_MORNING_DAYS", "sun").strip().lower()
    days = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if part not in WEEKDAY_NAMES:
            raise RuntimeError(
                f"SCRIPTURE_MORNING_DAYS must be comma-separated from {sorted(WEEKDAY_NAMES)}, got {raw!r}"
            )
        days.append(WEEKDAY_NAMES[part])
    if not days:
        raise RuntimeError("SCRIPTURE_MORNING_DAYS is empty; leave SCRIPTURE_MORNING_TIME empty to disable")
    return tuple(sorted(set(days)))


def get_silence_vote_threshold() -> int:
    return get_positive_int_env("SILENCE_VOTE_THRESHOLD", 5)


def get_silence_duration() -> timedelta:
    return timedelta(days=get_positive_int_env("SILENCE_DURATION_DAYS", 7))


def get_silence_vote_window() -> timedelta:
    return timedelta(hours=get_positive_int_env("SILENCE_VOTE_HOURS", 24))


def is_repentance_announcement_enabled() -> bool:
    return os.getenv("REPENTANCE_ANNOUNCEMENT", "1").strip().lower() not in {"0", "false", "no", "off"}


def get_roast_cooldown() -> timedelta:
    raw = os.getenv("ROAST_COOLDOWN_MINUTES", "10").strip()
    try:
        minutes = float(raw)
    except ValueError:
        minutes = 10.0

    return timedelta(minutes=minutes)


def get_roast_lookback_days() -> int:
    raw = os.getenv("ROAST_LOOKBACK_DAYS", "7").strip()
    try:
        days = int(raw)
    except ValueError:
        days = 7

    if days <= 0:
        days = 7

    return days


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


def get_slang_refresh_interval() -> timedelta:
    raw = os.getenv("SLANG_REFRESH_HOURS", "24").strip()
    try:
        hours = float(raw)
    except ValueError:
        hours = 24.0

    if hours <= 0:
        hours = 24.0

    return timedelta(hours=hours)


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


def connect_db(path: Path = DB_PATH) -> sqlite3.Connection:
    timeout = get_positive_float_env("SQLITE_TIMEOUT_SECONDS", DEFAULT_SQLITE_TIMEOUT_SECONDS)
    connection = sqlite3.connect(path, timeout=timeout)
    connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    return connection


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with closing(connect_db()) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
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
                reply_to_message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, chat_id)
            )
            """
        )

        existing_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "is_bot" not in existing_columns:
            connection.execute("ALTER TABLE messages ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0")
        if "reply_to_message_id" not in existing_columns:
            connection.execute("ALTER TABLE messages ADD COLUMN reply_to_message_id INTEGER")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT NOT NULL,
                date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_digests (
                chat_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                intro TEXT,
                quote_text TEXT,
                quote_author TEXT,
                nominations_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, date)
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id INTEGER PRIMARY KEY,
                handler TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        connection.commit()


def get_bot_state(key: str) -> str | None:
    with closing(connect_db()) as connection:
        row = connection.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()

    return row[0] if row else None


def set_bot_state(key: str, value: str) -> None:
    with closing(connect_db()) as connection:
        connection.execute(
            """
            INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
        connection.commit()


def get_silence_until() -> datetime | None:
    raw = get_bot_state(SILENCE_UNTIL_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("bad %s value in bot_state: %r", SILENCE_UNTIL_KEY, raw)
        return None


def is_bot_silenced(now: datetime | None = None) -> bool:
    """Тишина: бот сам ничего не пишет в чат (вечерняя сводка, воскресное слово). Команды работают."""
    until = get_silence_until()
    return until is not None and (now or datetime.now(timezone.utc)) < until


def format_local_datetime(value: datetime) -> str:
    return value.astimezone(get_app_timezone()).strftime("%d.%m в %H:%M")


def get_day_bounds_utc(day: date, tz: ZoneInfo) -> tuple[str, str]:
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = datetime.combine(day, time.max, tzinfo=tz)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def fetch_today_messages(chat_id: int, tz: ZoneInfo) -> list[sqlite3.Row]:
    return fetch_messages_for_day(chat_id, tz, datetime.now(tz).date())


def fetch_messages_for_day(chat_id: int, tz: ZoneInfo, day: date) -> list[sqlite3.Row]:
    start_utc, end_utc = get_day_bounds_utc(day, tz)

    with closing(connect_db()) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT
                message.message_id,
                message.user_id,
                message.username,
                message.display_name,
                message.text,
                message.message_datetime,
                message.reply_to_message_id,
                reply.user_id AS reply_user_id,
                reply.username AS reply_username,
                reply.display_name AS reply_display_name,
                reply.text AS reply_text
            FROM messages AS message
            LEFT JOIN messages AS reply
              ON reply.chat_id = message.chat_id
             AND reply.message_id = message.reply_to_message_id
            WHERE message.chat_id = ?
              AND message.message_datetime >= ?
              AND message.message_datetime <= ?
            ORDER BY message.message_datetime ASC, message.message_id ASC
            """,
            (chat_id, start_utc, end_utc),
        ).fetchall()


def resolve_user_id_by_username(chat_id: int, username: str) -> int | None:
    with closing(connect_db()) as connection:
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
    with closing(connect_db()) as connection:
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
    if user_id is None:
        return []

    query = f"""
        SELECT user_id, username, display_name, text, message_datetime
        FROM messages
        WHERE chat_id = ? AND user_id = ?
    """
    params: list = [chat_id, user_id]
    if since_utc is not None:
        query += " AND message_datetime >= ?"
        params.append(since_utc)
    query += " ORDER BY message_datetime ASC, message_id ASC"

    with closing(connect_db()) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(query, params).fetchall()


def extract_chat_slang(chat_id: int) -> list[str]:
    since_utc = (datetime.now(timezone.utc) - timedelta(days=SLANG_LOOKBACK_DAYS)).isoformat()

    with closing(connect_db()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT user_id, text
            FROM messages
            WHERE chat_id = ?
              AND message_datetime >= ?
              AND user_id IS NOT NULL
              AND (is_bot IS NULL OR is_bot = 0)
            """,
            (chat_id, since_utc),
        ).fetchall()

    word_counts: Counter[str] = Counter()
    word_users: defaultdict[str, set[int]] = defaultdict(set)

    for row in rows:
        text = row["text"]
        user_id = row["user_id"]
        if not text or user_id is None:
            continue

        for word in SLANG_WORD_PATTERN.findall(text.lower()):
            if len(word) < SLANG_MIN_WORD_LENGTH or word in RUSSIAN_STOP_WORDS:
                continue

            word_counts[word] += 1
            word_users[word].add(user_id)

    candidates = [
        (word, total_count)
        for word, total_count in word_counts.items()
        if total_count >= SLANG_MIN_TOTAL_COUNT and len(word_users[word]) >= SLANG_MIN_USERS
    ]
    candidates.sort(key=lambda item: (-item[1], item[0]))

    return [word for word, _ in candidates[:SLANG_TOP_LIMIT]]


def get_or_extract_chat_slang(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list[str]:
    cache: dict[int, dict] = context.application.bot_data.setdefault("chat_slang_cache", {})
    now = datetime.now(timezone.utc)
    refresh_interval = get_slang_refresh_interval()

    cached = cache.get(chat_id)
    if cached is not None and now - cached["built_at"] < refresh_interval:
        return cached["words"]

    words = extract_chat_slang(chat_id)
    cache[chat_id] = {"words": words, "built_at": now}
    logger.info("chat slang rebuilt chat_id=%s words=%s", chat_id, words)
    return words


def build_character_intro(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    slang_words = get_or_extract_chat_slang(context, chat_id)
    base_intro = "\n".join(
        (
            RAT_CHARACTER_INTRO,
            KIND_HUMOR_TONE_INSTRUCTION,
            LIVELY_STYLE_INSTRUCTION,
            FAITH_RESPECT_INSTRUCTION,
            NO_PROFANITY_INSTRUCTION,
            AUTHORSHIP_INSTRUCTION,
            REAL_TOXIC_CONTENT_BARRIER_INSTRUCTION,
        )
    )
    if not slang_words:
        return base_intro

    slang_instruction = SLANG_INSTRUCTION_TEMPLATE.format(words=", ".join(slang_words))
    return f"{base_intro}\n{slang_instruction}"


def get_recent_generated_texts(
    context: ContextTypes.DEFAULT_TYPE,
    storage_key: str,
    chat_id: int,
) -> list[str]:
    recent_by_chat: dict[int, list[str]] = context.application.bot_data.setdefault(
        storage_key, {}
    )
    return list(recent_by_chat.get(chat_id, []))


def remember_generated_text(
    context: ContextTypes.DEFAULT_TYPE,
    storage_key: str,
    chat_id: int,
    text: str,
) -> None:
    cleaned_text = " ".join(text.split())
    if not cleaned_text:
        return

    recent_by_chat: dict[int, list[str]] = context.application.bot_data.setdefault(
        storage_key, {}
    )
    recent = recent_by_chat.setdefault(chat_id, [])
    recent.append(cleaned_text)
    recent_by_chat[chat_id] = recent[-RECENT_GENERATED_TEXTS_LIMIT:]


def format_recent_roasts_instruction(recent_roasts: list[str]) -> str:
    if not recent_roasts:
        return ""

    return (
        "\n\nВот твои последние roast-подколы в этом чате: "
        f"{' | '.join(recent_roasts)}. "
        "Запрещено повторять эти формулировки, образы, сравнения, структуру фраз и главный панч. "
        "Если тянет пошутить так же — выбери другой факт из сообщений и ударь с другого угла."
    )


def select_roast_source_lines(rows: list[sqlite3.Row]) -> list[str]:
    lines = [row["text"].strip() for row in rows if row["text"] and row["text"].strip()]
    if len(lines) <= ROAST_MAX_SOURCE_MESSAGES:
        return lines

    recent_count = max(12, ROAST_MAX_SOURCE_MESSAGES // 3)
    recent_lines = lines[-recent_count:]
    older_lines = lines[:-recent_count]
    sampled_older_lines = random.sample(
        older_lines,
        k=min(len(older_lines), ROAST_MAX_SOURCE_MESSAGES - recent_count),
    )
    selected_lines = sampled_older_lines + recent_lines
    random.shuffle(selected_lines)
    return selected_lines


def sanitize_local_roast_detail(text: str) -> str:
    cleaned = " ".join(text.split()).strip()
    cleaned = LOCAL_ROAST_UNSAFE_WORD_PATTERN.sub("[реплика из чата]", cleaned)
    if len(cleaned) > 90:
        cleaned = cleaned[:87].rstrip() + "..."

    return cleaned


def build_local_roast_fallback(name: str, lines: list[str]) -> str:
    details = []
    for line in lines[-12:]:
        detail = sanitize_local_roast_detail(line)
        if detail:
            details.append(detail)

    detail = random.choice(details) if details else "очередной след в истории чата"
    templates = (
        "{name}, Gemini сейчас прилёг отдохнуть, но архив чата не молчит. Фраза «{detail}» "
        "уже заслуживает отдельной рамочки на стене чата.",
        "{name}, внешний мозг временно недоступен, поэтому подкол короткий: «{detail}» — "
        "и этим всё сказано, причём с любовью.",
        "{name}, нейросеть не ответила, зато история чата справилась сама. «{detail}» — "
        "фраза, которую хочется процитировать на твоём юбилее.",
    )
    return random.choice(templates).format(name=name, detail=detail)


def get_author_name(
    row: sqlite3.Row,
    name_map: dict[str, str],
    *,
    prefix: str = "",
) -> str:
    username = row[f"{prefix}username"]
    if username:
        mapped_name = name_map.get(username.lower())
        if mapped_name:
            return mapped_name

    user_id = row[f"{prefix}user_id"]
    return row[f"{prefix}display_name"] or username or f"user_{user_id}"


def format_reply_excerpt(text: str, limit: int = 90) -> str:
    excerpt = " ".join(text.split()).strip()
    if len(excerpt) > limit:
        excerpt = excerpt[: limit - 3].rstrip() + "..."

    return excerpt


def build_messages_transcript(messages: list[sqlite3.Row], name_map: dict[str, str]) -> str:
    lines = []
    for row in messages:
        text = row["text"].strip() if row["text"] else ""
        if not text:
            continue

        author_name = get_author_name(row, name_map)
        reply_text = row["reply_text"] if "reply_text" in row.keys() else None
        if (
            row["reply_to_message_id"] is not None
            and reply_text
            and (
                row["reply_user_id"] is not None
                or row["reply_username"]
                or row["reply_display_name"]
            )
        ):
            reply_author_name = get_author_name(row, name_map, prefix="reply_")
            reply_excerpt = format_reply_excerpt(reply_text)
            lines.append(
                f"{author_name} (в ответ {reply_author_name}: '{reply_excerpt}'): {text}"
            )
        else:
            lines.append(f"{author_name}: {text}")

    return "\n".join(lines)


def build_digest_request(
    messages: list[sqlite3.Row], name_map: dict[str, str], character_intro: str
) -> str:
    wisdom_options = "\n".join(
        f"- {entry['id']}: {' '.join(entry['text'].split())}"
        for entry in scripture.sample_entries(scripture.WISDOM, DIGEST_WISDOM_OPTIONS)
    )
    prompt = DIGEST_PROMPT_TEMPLATE.format(
        character_intro=character_intro,
        no_cliches_instruction=NO_CLICHES_INSTRUCTION,
        wisdom_options=wisdom_options,
    )
    return f"{prompt}\n\nСообщения за день:\n" + build_messages_transcript(messages, name_map)


def save_daily_digest(chat_id: int, date_str: str, digest_data: dict) -> None:
    with closing(connect_db()) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO daily_digests (
                chat_id, date, intro, quote_text, quote_author, nominations_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                date_str,
                str(digest_data.get("intro", "")).strip(),
                str(digest_data.get("quote_text", "")).strip(),
                str(digest_data.get("quote_author", "")).strip(),
                json.dumps(digest_data.get("nominations") or [], ensure_ascii=False),
            ),
        )
        connection.commit()

    logger.info("daily digest saved to daily_digests chat_id=%s date=%s", chat_id, date_str)


def fetch_weekly_digests(chat_id: int, tz: ZoneInfo) -> list[sqlite3.Row]:
    today = datetime.now(tz).date()
    start_date = (today - timedelta(days=6)).isoformat()
    end_date = today.isoformat()

    with closing(connect_db()) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT date, intro, quote_text, quote_author, nominations_json
            FROM daily_digests
            WHERE chat_id = ?
              AND date >= ?
              AND date <= ?
            ORDER BY date ASC
            """,
            (chat_id, start_date, end_date),
        ).fetchall()


def build_weekly_digest(chat_id: int, character_intro: str) -> str | None:
    tz = get_app_timezone()
    rows = fetch_weekly_digests(chat_id, tz)
    logger.info("weekly digest: chat_id=%s daily digests found=%s", chat_id, len(rows))
    if not rows:
        return None

    day_blocks = []
    for row in rows:
        try:
            nominations = json.loads(row["nominations_json"]) if row["nominations_json"] else []
        except (TypeError, ValueError):
            nominations = []

        nominations_text = "; ".join(
            f"{nom.get('title', '')}: {nom.get('name', '')} — {nom.get('reason', '')}"
            for nom in nominations
        ) or "нет"

        day_blocks.append(
            f"Дата: {row['date']}\n"
            f"Пересказ дня: {row['intro'] or ''}\n"
            f"Цитата дня: \"{row['quote_text'] or ''}\" — {row['quote_author'] or ''}\n"
            f"Номинации: {nominations_text}"
        )

    return WEEKLY_DIGEST_PROMPT_TEMPLATE.format(
        character_intro=character_intro,
        days="\n\n".join(day_blocks),
    )


def get_positive_int_env(env_var: str, default: int) -> int:
    raw = os.getenv(env_var, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %s", env_var, raw, default)
        return default

    if value <= 0:
        logger.warning("invalid %s=%r; using default %s", env_var, raw, default)
        return default

    return value


def get_positive_float_env(env_var: str, default: float) -> float:
    raw = os.getenv(env_var, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %s", env_var, raw, default)
        return default

    if value <= 0:
        logger.warning("invalid %s=%r; using default %s", env_var, raw, default)
        return default

    return value


def get_gemini_model(env_var: str = "GEMINI_MODEL", default: str = DEFAULT_GEMINI_MODEL) -> str:
    return os.getenv(env_var, default).strip() or default


def get_gemini_fallback_models(env_var: str = "GEMINI_FALLBACK_MODELS") -> list[str]:
    raw = os.getenv(env_var, "").strip()
    if raw:
        models = [model.strip() for model in raw.split(",") if model.strip()]
    else:
        models = list(DEFAULT_GEMINI_FALLBACK_MODELS)

    primary_model = get_gemini_model()
    return [model for model in models if model != primary_model]


def get_gemini_model_candidates(
    *,
    primary_model: str | None = None,
    fallback_models: list[str] | None = None,
) -> list[str]:
    candidates = [primary_model or get_gemini_model()]
    candidates.extend(fallback_models if fallback_models is not None else get_gemini_fallback_models())

    deduped: list[str] = []
    for model in candidates:
        if model and model not in deduped:
            deduped.append(model)

    return deduped


def get_gemini_semaphore() -> asyncio.Semaphore:
    global _gemini_semaphore, _gemini_semaphore_limit

    limit = get_positive_int_env(
        "GEMINI_MAX_CONCURRENT_REQUESTS",
        DEFAULT_GEMINI_MAX_CONCURRENT_REQUESTS,
    )
    if _gemini_semaphore is None or _gemini_semaphore_limit != limit:
        _gemini_semaphore = asyncio.Semaphore(limit)
        _gemini_semaphore_limit = limit
        logger.info("Gemini concurrency limit set to %s", limit)

    return _gemini_semaphore


def get_retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), GEMINI_RETRY_MAX_DELAY_SECONDS)
            except ValueError:
                pass

    delay = GEMINI_RETRY_BASE_DELAY_SECONDS * attempt + random.uniform(0, 0.5)
    return min(delay, GEMINI_RETRY_MAX_DELAY_SECONDS)


async def generate_gemini_text(
    prompt: str,
    *,
    max_attempts: int = GEMINI_MAX_ATTEMPTS,
    timeout: httpx.Timeout = GEMINI_TIMEOUT,
    model: str | None = None,
) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required")

    model = model or get_gemini_model()
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

    semaphore = get_gemini_semaphore()
    async with semaphore, httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.post(url, headers=headers, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"Gemini request failed after {attempt} attempts: {exc!r}"
                    ) from exc

                delay = get_retry_delay(None, attempt)
                logger.warning(
                    "Gemini request failed attempt=%s/%s error=%r; retrying in %.1fs",
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code in GEMINI_RETRY_STATUS_CODES:
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"Gemini API error {response.status_code}: {response.text[:500]}"
                    )

                delay = get_retry_delay(response, attempt)
                logger.warning(
                    "Gemini transient API error status=%s attempt=%s/%s body=%r; retrying in %.1fs",
                    response.status_code,
                    attempt,
                    max_attempts,
                    response.text[:300],
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                raise RuntimeError(f"Gemini API error {response.status_code}: {response.text[:500]}")

            data = response.json()
            candidates = data.get("candidates") or []
            parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
            text = "\n".join(part.get("text", "") for part in parts).strip()
            if not text:
                raise RuntimeError(f"Gemini API returned empty text: {data}")

            return text

    raise RuntimeError("Gemini request failed without response")


async def generate_gemini_text_with_fallback(
    prompt: str,
    *,
    max_attempts: int = GEMINI_MAX_ATTEMPTS,
    timeout: httpx.Timeout = GEMINI_TIMEOUT,
    primary_model: str | None = None,
    fallback_models: list[str] | None = None,
) -> str:
    last_error: Exception | None = None
    for model in get_gemini_model_candidates(
        primary_model=primary_model,
        fallback_models=fallback_models,
    ):
        try:
            return await generate_gemini_text(
                prompt,
                max_attempts=max_attempts,
                timeout=timeout,
                model=model,
            )
        except Exception as exc:
            last_error = exc
            logger.warning("Gemini model failed model=%s error=%r", model, exc)

    raise RuntimeError("all Gemini model candidates failed") from last_error


async def generate_gemini_text_for_task(
    prompt: str,
    *,
    model_env: str,
    model_default: str,
    max_attempts: int = GEMINI_MAX_ATTEMPTS,
    timeout: httpx.Timeout = GEMINI_TIMEOUT,
) -> str:
    """Сгенерировать текст под конкретную задачу с её основной моделью и общими фолбэками."""
    primary_model = get_gemini_model(model_env, model_default)
    fallback_models = get_gemini_model_candidates()
    logger.info(
        "%s: порядок моделей=%s",
        model_env,
        get_gemini_model_candidates(
            primary_model=primary_model,
            fallback_models=fallback_models,
        ),
    )
    return await generate_gemini_text_with_fallback(
        prompt,
        max_attempts=max_attempts,
        timeout=timeout,
        primary_model=primary_model,
        fallback_models=fallback_models,
    )


def parse_digest_json(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*\n", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()

    return json.loads(cleaned)


def parse_roast_choice_json(raw_text: str) -> tuple[str, dict]:
    data = parse_digest_json(raw_text)
    if not isinstance(data, dict):
        raise ValueError("roast response must be a JSON object")

    raw_candidates = data.get("candidates")
    if not isinstance(raw_candidates, list) or len(raw_candidates) != 3:
        raise ValueError("roast response must contain exactly 3 candidates")

    candidates = []
    for candidate in raw_candidates:
        if not isinstance(candidate, str):
            raise ValueError("roast candidates must be strings")
        cleaned_candidate = " ".join(candidate.split()).strip()
        if not cleaned_candidate:
            raise ValueError("roast candidates must not be empty")
        candidates.append(cleaned_candidate)

    best_index = data.get("best_index")
    if isinstance(best_index, bool) or not isinstance(best_index, int):
        raise ValueError("roast best_index must be an integer")
    if best_index < 0 or best_index >= len(candidates):
        raise ValueError("roast best_index is out of range")

    choice = {
        "candidates": candidates,
        "best_index": best_index,
        "reason": " ".join(str(data.get("reason", "")).split()).strip(),
    }
    return candidates[best_index], choice


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

    wisdom_entry = resolve_digest_wisdom(data.get("wisdom_id"))
    lines.extend(["", scripture.format_entry_html(wisdom_entry, header="📜 <b>Мудрость дня</b>")])
    wisdom_comment = str(data.get("wisdom_comment") or "").strip()
    if wisdom_comment and wisdom_entry["id"] == str(data.get("wisdom_id") or "").strip():
        lines.extend(["", f"🐀 {escape_html(wisdom_comment)}"])

    return "\n".join(lines)


def resolve_digest_wisdom(wisdom_id: object) -> dict:
    """Цитату берём только из проверенного списка; если Gemini вернул чужой id — случайная."""
    if isinstance(wisdom_id, str):
        entry = scripture.find_entry(wisdom_id.strip())
        if entry is not None and entry in scripture.get_entries(scripture.WISDOM):
            return entry
        logger.warning("digest: unknown wisdom_id=%r, using random wisdom", wisdom_id)
    return scripture.pick_entry(scripture.WISDOM)


def format_weekly_digest_html(data: dict) -> str:
    weekly_summary = escape_html(str(data.get("weekly_summary", "")).strip())
    quote_text = escape_html(str(data.get("week_quote_text", "")).strip())
    quote_author = escape_html(str(data.get("week_quote_author", "")).strip())
    person = data.get("person_of_week") or {}
    person_name = escape_html(str(person.get("name", "")).strip())
    person_reason = escape_html(str(person.get("reason", "")).strip())

    lines = [
        "📅 <b>Дайджест недели</b>",
        "",
        weekly_summary,
        "",
        "<b>Цитата недели:</b>",
        f"<blockquote>{quote_text}</blockquote>",
        f"<i>— {quote_author}</i>",
        "",
        "<b>Герой недели:</b>",
        f"{person_name} — {person_reason}",
    ]

    return "\n".join(lines)


def build_votekick_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👋 Кикнуть", callback_data="votekick:kick"),
                InlineKeyboardButton("🙏 Помиловать", callback_data="votekick:spare"),
            ]
        ]
    )


def build_votekick_text(target_name: str, votes: dict[int, str]) -> str:
    kick_count = sum(1 for choice in votes.values() if choice == "kick")
    spare_count = sum(1 for choice in votes.values() if choice == "spare")
    return f"🗳 Голосование: выгоняем {target_name} из чата?\n\nЗа: {kick_count} | Против: {spare_count}"


def build_morning_keyboard(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"➕ {count}", callback_data="morning:react")]]
    )


def build_boot_keyboard(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"➕ {count}", callback_data="boot:react")]]
    )


def prune_morning_reactions(context: ContextTypes.DEFAULT_TYPE) -> None:
    reactions: dict[int, set[int]] = context.application.bot_data.setdefault(
        "morning_reactions", {}
    )
    created_at_by_message: dict[int, datetime] = context.application.bot_data.setdefault(
        "morning_reaction_created_at", {}
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)

    expired_message_ids = [
        message_id
        for message_id, created_at in created_at_by_message.items()
        if created_at < cutoff
    ]
    for message_id in expired_message_ids:
        reactions.pop(message_id, None)
        created_at_by_message.pop(message_id, None)


def prune_boot_reactions(context: ContextTypes.DEFAULT_TYPE) -> None:
    reactions: dict[int, set[int]] = context.application.bot_data.setdefault(
        "boot_reactions", {}
    )
    created_at_by_message: dict[int, datetime] = context.application.bot_data.setdefault(
        "boot_reaction_created_at", {}
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)

    expired_message_ids = [
        message_id
        for message_id, created_at in created_at_by_message.items()
        if created_at < cutoff
    ]
    for message_id in expired_message_ids:
        reactions.pop(message_id, None)
        created_at_by_message.pop(message_id, None)


async def send_morning_greeting(context: ContextTypes.DEFAULT_TYPE, source: str) -> None:
    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("утреннее приветствие пропущено: CHAT_ID не настроен")
        return

    prune_morning_reactions(context)
    sent_message = await context.bot.send_message(
        chat_id=allowed_chat_id,
        text="Доброе утро всем! ☀️",
        reply_markup=build_morning_keyboard(0),
    )

    reactions: dict[int, set[int]] = context.application.bot_data.setdefault(
        "morning_reactions", {}
    )
    created_at_by_message: dict[int, datetime] = context.application.bot_data.setdefault(
        "morning_reaction_created_at", {}
    )
    reactions[sent_message.message_id] = set()
    created_at_by_message[sent_message.message_id] = datetime.now(timezone.utc)

    logger.info(
        "morning greeting sent source=%s chat_id=%s message_id=%s",
        source,
        allowed_chat_id,
        sent_message.message_id,
    )


async def send_boot_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    source: str,
) -> None:
    prune_boot_reactions(context)
    sent_message = await context.bot.send_message(
        chat_id=chat_id,
        text="Бутнем?",
        reply_markup=build_boot_keyboard(0),
    )

    reactions: dict[int, set[int]] = context.application.bot_data.setdefault(
        "boot_reactions", {}
    )
    created_at_by_message: dict[int, datetime] = context.application.bot_data.setdefault(
        "boot_reaction_created_at", {}
    )
    reactions[sent_message.message_id] = set()
    created_at_by_message[sent_message.message_id] = datetime.now(timezone.utc)

    logger.info(
        "boot prompt sent source=%s chat_id=%s message_id=%s",
        source,
        chat_id,
        sent_message.message_id,
    )


async def send_boot_followup(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    if chat_id is None:
        logger.warning("boot follow-up skipped: job has no chat_id")
        return

    await context.bot.send_message(chat_id=chat_id, text="Бутнули, проверяйте")
    logger.info("boot follow-up sent chat_id=%s", chat_id)


def schedule_boot_followup(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    source: str,
) -> None:
    job = context.application.job_queue.run_once(
        send_boot_followup,
        when=BOOT_FOLLOWUP_DELAY_SECONDS,
        chat_id=chat_id,
        name=f"boot_followup_{chat_id}_{datetime.now(timezone.utc).timestamp()}",
    )
    logger.info(
        "boot follow-up scheduled source=%s chat_id=%s delay_seconds=%s job_name=%s",
        source,
        chat_id,
        BOOT_FOLLOWUP_DELAY_SECONDS,
        job.name,
    )


def save_message(
    *,
    message_id: int,
    chat_id: int,
    user_id: int | None,
    username: str | None,
    display_name: str | None,
    text: str,
    message_datetime: str,
    reply_to_message_id: int | None,
    is_bot: bool,
) -> bool:
    with closing(connect_db()) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO messages (
                message_id,
                chat_id,
                user_id,
                username,
                display_name,
                text,
                message_datetime,
                reply_to_message_id,
                is_bot
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                chat_id,
                user_id,
                username,
                display_name,
                text,
                message_datetime,
                reply_to_message_id,
                int(is_bot),
            ),
        )
        connection.commit()
        return cursor.rowcount > 0


def save_media(
    *,
    chat_id: int,
    message_id: int,
    user_id: int | None,
    media_type: str,
    file_id: str,
    file_unique_id: str,
    media_date: str,
) -> None:
    with closing(connect_db()) as connection:
        connection.execute(
            """
            INSERT INTO media (
                chat_id,
                message_id,
                user_id,
                media_type,
                file_id,
                file_unique_id,
                date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                message_id,
                user_id,
                media_type,
                file_id,
                file_unique_id,
                media_date,
            ),
        )
        connection.commit()


def mark_update_processed_once(
    update: Update,
    handler: str,
) -> bool:
    update_id = update.update_id
    if update_id is None:
        logger.warning("%s update has no update_id; processing without duplicate guard", handler)
        return True

    message = update.effective_message
    chat = update.effective_chat
    with closing(connect_db()) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO processed_updates (update_id, handler, processed_at)
            VALUES (?, ?, ?)
            """,
            (update_id, handler, datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()

    if cursor.rowcount == 0:
        logger.info(
            "%s duplicate update ignored update_id=%s chat_id=%s message_id=%s",
            handler,
            update_id,
            chat.id if chat else None,
            message.message_id if message else None,
        )
        return False

    return True


def fetch_random_photo_media(chat_id: int) -> sqlite3.Row | None:
    with closing(connect_db()) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT file_id
            FROM media
            WHERE chat_id = ? AND media_type = 'photo'
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()


def parse_db_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def resolve_display_name(
    *,
    user_id: int | None,
    username: str | None,
    display_name: str | None,
    name_map: dict[str, str],
) -> str:
    if username:
        mapped_name = name_map.get(username.lower())
        if mapped_name:
            return mapped_name

    return display_name or username or f"user_{user_id}"


def format_quiet_duration(duration: timedelta) -> str:
    total_seconds = max(0, int(duration.total_seconds()))
    days, remainder = divmod(total_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes = remainder // 60

    if days:
        return f"{days} дн. {hours} ч."
    if hours:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."


def fetch_chat_stats(chat_id: int, tz: ZoneInfo, name_map: dict[str, str]) -> dict:
    today_start_utc, today_end_utc = get_day_bounds_utc(datetime.now(tz).date(), tz)
    seven_days_ago_utc = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    non_bot_clause = "(is_bot IS NULL OR is_bot = 0)"

    with closing(connect_db()) as connection:
        connection.row_factory = sqlite3.Row

        total_messages = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM messages
            WHERE chat_id = ?
              AND {non_bot_clause}
            """,
            (chat_id,),
        ).fetchone()[0]

        today_messages = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM messages
            WHERE chat_id = ?
              AND message_datetime >= ?
              AND message_datetime <= ?
              AND {non_bot_clause}
            """,
            (chat_id, today_start_utc, today_end_utc),
        ).fetchone()[0]

        recent_rows = connection.execute(
            f"""
            SELECT user_id, username, display_name
            FROM messages
            WHERE chat_id = ?
              AND message_datetime >= ?
              AND user_id IS NOT NULL
              AND {non_bot_clause}
            ORDER BY message_datetime DESC, message_id DESC
            """,
            (chat_id, seven_days_ago_utc),
        ).fetchall()

        all_message_datetimes = connection.execute(
            f"""
            SELECT message_datetime
            FROM messages
            WHERE chat_id = ?
              AND {non_bot_clause}
            """,
            (chat_id,),
        ).fetchall()

        media_rows = connection.execute(
            """
            SELECT media_type, COUNT(*) AS media_count
            FROM media
            WHERE chat_id = ?
              AND media_type IN ('photo', 'sticker')
            GROUP BY media_type
            """,
            (chat_id,),
        ).fetchall()

        quiet_candidates = []
        if name_map:
            username_keys = list(name_map)
            placeholders = ",".join("?" for _ in username_keys)
            username_rows = connection.execute(
                f"""
                SELECT lower(username) AS username_key, user_id
                FROM messages
                WHERE chat_id = ?
                  AND username IS NOT NULL
                  AND lower(username) IN ({placeholders})
                  AND user_id IS NOT NULL
                  AND {non_bot_clause}
                ORDER BY message_datetime DESC, message_id DESC
                """,
                [chat_id, *username_keys],
            ).fetchall()

            user_id_by_username: dict[str, int] = {}
            for row in username_rows:
                user_id_by_username.setdefault(row["username_key"], row["user_id"])

            user_ids = sorted(set(user_id_by_username.values()))
            last_message_by_user_id: dict[int, str] = {}
            if user_ids:
                user_id_placeholders = ",".join("?" for _ in user_ids)
                last_message_rows = connection.execute(
                    f"""
                    SELECT user_id, MAX(message_datetime) AS last_message_datetime
                    FROM messages
                    WHERE chat_id = ?
                      AND user_id IN ({user_id_placeholders})
                      AND {non_bot_clause}
                    GROUP BY user_id
                    """,
                    [chat_id, *user_ids],
                ).fetchall()
                last_message_by_user_id = {
                    row["user_id"]: row["last_message_datetime"] for row in last_message_rows
                }

            now_utc = datetime.now(timezone.utc)
            for username, name in name_map.items():
                user_id = user_id_by_username.get(username)
                if user_id is None:
                    quiet_candidates.append(
                        {
                            "name": name,
                            "user_id": None,
                            "last_message_datetime": None,
                            "silence": None,
                        }
                    )
                    continue

                last_message_datetime = last_message_by_user_id.get(user_id)
                silence = (
                    now_utc - parse_db_datetime(last_message_datetime).astimezone(timezone.utc)
                    if last_message_datetime
                    else None
                )
                quiet_candidates.append(
                    {
                        "name": name,
                        "user_id": user_id,
                        "last_message_datetime": last_message_datetime,
                        "silence": silence,
                    }
                )

    recent_counts: Counter[int] = Counter()
    latest_recent_user: dict[int, sqlite3.Row] = {}
    for row in recent_rows:
        user_id = row["user_id"]
        recent_counts[user_id] += 1
        latest_recent_user.setdefault(user_id, row)

    top_users = []
    for user_id, count in recent_counts.most_common(5):
        row = latest_recent_user[user_id]
        top_users.append(
            {
                "name": resolve_display_name(
                    user_id=user_id,
                    username=row["username"],
                    display_name=row["display_name"],
                    name_map=name_map,
                ),
                "count": count,
            }
        )

    hour_counts: Counter[int] = Counter()
    for row in all_message_datetimes:
        local_datetime = parse_db_datetime(row["message_datetime"]).astimezone(tz)
        hour_counts[local_datetime.hour] += 1

    peak_hour = None
    if hour_counts:
        peak_hour, peak_count = min(
            hour_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    else:
        peak_count = 0

    quiet_person = None
    if quiet_candidates:
        never_written = [
            candidate
            for candidate in quiet_candidates
            if candidate["last_message_datetime"] is None
        ]
        if never_written:
            quiet_person = never_written[0]
        else:
            quiet_person = max(
                quiet_candidates,
                key=lambda candidate: candidate["silence"] or timedelta(0),
            )

    media_counts = {"photo": 0, "sticker": 0}
    for row in media_rows:
        media_counts[row["media_type"]] = row["media_count"]

    return {
        "total_messages": total_messages,
        "today_messages": today_messages,
        "top_users": top_users,
        "quiet_person": quiet_person,
        "peak_hour": peak_hour,
        "peak_count": peak_count,
        "media_counts": media_counts,
    }


def format_stats_html(stats: dict, tz: ZoneInfo) -> str:
    top_users = stats["top_users"]
    if top_users:
        top_lines = [
            f"{index}. {escape_html(user['name'])} — {user['count']}"
            for index, user in enumerate(top_users, 1)
        ]
        top_text = "\n".join(top_lines)
    else:
        top_text = "нет сообщений за последние 7 дней"

    quiet_person = stats["quiet_person"]
    if quiet_person is None:
        quiet_text = "словарик имён пуст"
    elif quiet_person["last_message_datetime"] is None:
        quiet_text = f"{escape_html(quiet_person['name'])} — не писал(а) ни разу"
    else:
        last_local = parse_db_datetime(quiet_person["last_message_datetime"]).astimezone(tz)
        quiet_text = (
            f"{escape_html(quiet_person['name'])} — молчит "
            f"{format_quiet_duration(quiet_person['silence'])}, "
            f"последнее сообщение {last_local:%Y-%m-%d %H:%M}"
        )

    if stats["peak_hour"] is None:
        peak_text = "нет данных"
    else:
        peak_text = f"{stats['peak_hour']:02d}:00 — {stats['peak_count']} сообщений"

    media_counts = stats["media_counts"]
    return "\n".join(
        [
            "📊 <b>Статистика чата</b>",
            "",
            f"💬 <b>Всего:</b> {stats['total_messages']} сообщений",
            f"📅 <b>Сегодня:</b> {stats['today_messages']} сообщений",
            "",
            "🏆 <b>Топ-5 за 7 дней:</b>",
            top_text,
            "",
            f"🦗 <b>Самый тихий:</b> {quiet_text}",
            f"⏰ <b>Час пик:</b> {peak_text}",
            (
                "🖼 <b>Медиа:</b> "
                f"фото — {media_counts['photo']}, "
                f"стикеры — {media_counts['sticker']}"
            ),
        ]
    )


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
    reply_to_message_id = (
        message.reply_to_message.message_id
        if message.reply_to_message is not None
        else None
    )

    inserted = save_message(
        message_id=message.message_id,
        chat_id=chat.id,
        user_id=user.id if user else None,
        username=user.username if user else None,
        display_name=display_name,
        text=message.text,
        message_datetime=message_datetime,
        reply_to_message_id=reply_to_message_id,
        is_bot=bool(user.is_bot) if user else False,
    )
    if not inserted:
        logger.info(
            "duplicate message ignored chat_id=%s message_id=%s user_id=%s text=%r",
            chat.id,
            message.message_id,
            user.id if user else None,
            message.text[:120],
        )
        return

    logger.info(
        "saved message chat_id=%s message_id=%s user_id=%s text=%r",
        chat.id,
        message.message_id,
        user.id if user else None,
        message.text[:120],
    )


async def handle_photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    allowed_chat_id = get_allowed_chat_id()

    if message is None or chat is None or not message.photo:
        return

    if allowed_chat_id is not None and chat.id != allowed_chat_id:
        return

    largest_photo = message.photo[-1]
    media_date = message.date.astimezone(timezone.utc).isoformat()

    save_media(
        chat_id=chat.id,
        message_id=message.message_id,
        user_id=user.id if user else None,
        media_type="photo",
        file_id=largest_photo.file_id,
        file_unique_id=largest_photo.file_unique_id,
        media_date=media_date,
    )

    logger.info(
        "saved media chat_id=%s message_id=%s type=photo file_unique_id=%s",
        chat.id,
        message.message_id,
        largest_photo.file_unique_id,
    )


async def handle_sticker_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    allowed_chat_id = get_allowed_chat_id()

    if message is None or chat is None or message.sticker is None:
        return

    if allowed_chat_id is not None and chat.id != allowed_chat_id:
        return

    sticker = message.sticker
    media_date = message.date.astimezone(timezone.utc).isoformat()

    save_media(
        chat_id=chat.id,
        message_id=message.message_id,
        user_id=user.id if user else None,
        media_type="sticker",
        file_id=sticker.file_id,
        file_unique_id=sticker.file_unique_id,
        media_date=media_date,
    )

    logger.info(
        "saved media chat_id=%s message_id=%s type=sticker file_unique_id=%s",
        chat.id,
        message.message_id,
        sticker.file_unique_id,
    )


async def send_scripture_reply(update: Update, pool: str, header: str, source: str) -> None:
    message = update.effective_message
    if message is None:
        return
    if not mark_update_processed_once(update, source):
        return

    entry = scripture.pick_entry(pool)
    await message.reply_text(scripture.format_entry_html(entry, header=header), parse_mode="HTML")
    logger.info("%s sent chat_id=%s ref=%s", source, message.chat_id, entry["ref"])


async def handle_wisdom_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_scripture_reply(update, scripture.WISDOM, "🦉 <b>Мудрость</b>", "/wisdom")


async def handle_verse_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_scripture_reply(update, scripture.VERSE, "📖 <b>Стих</b>", "/verse")


async def send_scripture_morning(context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("утреннее слово пропущено: CHAT_ID не настроен")
        return

    if is_bot_silenced():
        logger.info("утреннее слово пропущено: чат проголосовал за тишину")
        return

    pool = random.choice((scripture.WISDOM, scripture.VERSE))
    entry = scripture.pick_entry(pool)
    await context.bot.send_message(
        chat_id=allowed_chat_id,
        text=scripture.format_entry_html(entry, header="☀️ <b>Доброе утро! Слово на неделю</b>"),
        parse_mode="HTML",
    )
    logger.info("утреннее слово отправлено chat_id=%s ref=%s", allowed_chat_id, entry["ref"])


async def send_repentance_announcement_once(application: Application) -> None:
    if not is_repentance_announcement_enabled():
        logger.info("объявление о покаянии отключено через REPENTANCE_ANNOUNCEMENT")
        return
    if get_bot_state(REPENTANCE_ANNOUNCEMENT_KEY):
        return

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("объявление о покаянии пропущено: CHAT_ID не настроен")
        return

    try:
        await application.bot.send_message(
            chat_id=allowed_chat_id,
            text=REPENTANCE_ANNOUNCEMENT_TEXT,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("не удалось отправить объявление о покаянии, попробую при следующем запуске")
        return

    set_bot_state(REPENTANCE_ANNOUNCEMENT_KEY, datetime.now(timezone.utc).isoformat())
    logger.info("объявление о покаянии отправлено chat_id=%s", allowed_chat_id)


async def handle_stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None:
        return
    if not mark_update_processed_once(update, "/stats"):
        return

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is not None and chat.id != allowed_chat_id:
        logger.info("/stats ignored from chat_id=%s", chat.id)
        return

    logger.info(
        "/stats called chat_id=%s user_id=%s",
        chat.id,
        user.id if user else None,
    )

    tz = get_app_timezone()
    name_map = context.application.bot_data.get("name_map", {})
    stats = fetch_chat_stats(chat.id, tz, name_map)
    await message.reply_text(format_stats_html(stats, tz), parse_mode="HTML")


async def handle_morning_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not mark_update_processed_once(update, "/morning"):
        return

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("/morning ignored because CHAT_ID is empty")
        await message.reply_text("CHAT_ID не настроен, утреннее приветствие отправить не получится")
        return

    if chat.id != allowed_chat_id:
        logger.info("/morning ignored from chat_id=%s", chat.id)
        return

    try:
        await send_morning_greeting(context, source="manual")
    except Exception:
        logger.exception("/morning failed to send greeting")
        await message.reply_text("Не смог отправить утреннее приветствие, попробуйте позже")


async def handle_boot_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not mark_update_processed_once(update, "/boot"):
        return

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("/boot ignored because CHAT_ID is empty")
        await message.reply_text("CHAT_ID не настроен, бутнуть не получится")
        return

    if chat.id != allowed_chat_id:
        logger.info("/boot ignored from chat_id=%s", chat.id)
        return

    try:
        await send_boot_prompt(context, chat.id, source="manual")
        schedule_boot_followup(context, chat.id, source="manual")
    except Exception:
        logger.exception("/boot failed to send prompt")
        await message.reply_text("Не смог запустить бут, попробуйте позже")


async def handle_roast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not mark_update_processed_once(update, "/roast"):
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

    since_utc = (
        datetime.now(timezone.utc) - timedelta(days=get_roast_lookback_days())
    ).isoformat()
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

    lines = select_roast_source_lines(rows)
    messages_text = "\n".join(f"- {line}" for line in lines)
    character_intro = build_character_intro(context, allowed_chat_id)
    recent_roasts = get_recent_generated_texts(context, "recent_roast_replies", allowed_chat_id)
    roast_styles = random.sample(ROAST_STYLE_INSTRUCTIONS, k=3)
    candidate_styles = "\n".join(
        f"{index}. Кандидат {index}: {style}"
        for index, style in enumerate(roast_styles, start=1)
    )
    prompt = ROAST_PROMPT_TEMPLATE.format(
        character_intro=character_intro,
        name=display_name,
        messages=messages_text,
        candidate_styles=candidate_styles,
    )
    prompt += format_recent_roasts_instruction(recent_roasts)

    try:
        raw_roast = await generate_gemini_text_for_task(
            prompt,
            model_env="GEMINI_ROAST_MODEL",
            model_default=DEFAULT_GEMINI_ROAST_MODEL,
            max_attempts=GEMINI_ROAST_MAX_ATTEMPTS,
            timeout=GEMINI_ROAST_TIMEOUT,
        )
        roast_text, roast_choice = parse_roast_choice_json(raw_roast)
        logger.info(
            "/roast best-of-3 target_user_id=%s best_index=%s reason=%r styles=%s candidates=%s",
            roast_key,
            roast_choice["best_index"],
            roast_choice["reason"],
            json.dumps(roast_styles, ensure_ascii=False),
            json.dumps(roast_choice["candidates"], ensure_ascii=False),
        )
    except Exception:
        logger.exception("/roast failed to generate for target_user_id=%s", roast_key)
        roast_text = build_local_roast_fallback(display_name, lines)

    roast_last_at[roast_key] = now
    await message.reply_text(roast_text)
    remember_generated_text(context, "recent_roast_replies", allowed_chat_id, roast_text)
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
    if not mark_update_processed_once(update, "/votekick"):
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


def build_silence_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🤫 За тишину", callback_data="silence:yes"),
                InlineKeyboardButton("🙂 Против", callback_data="silence:no"),
            ]
        ]
    )


def build_silence_vote_text(votes: dict[int, str], closes_at: datetime) -> str:
    yes_count = sum(1 for choice in votes.values() if choice == "yes")
    no_count = sum(1 for choice in votes.values() if choice == "no")
    days = get_silence_duration().days
    return (
        "🤫 <b>Голосование за тишину</b>\n"
        "\n"
        f"Если наберётся {get_silence_vote_threshold()} голосов «за», я {days} дней не буду ничего писать "
        "в чат сам:\n"
        "• не будет вечерней сводки дня;\n"
        "• не будет воскресного слова.\n"
        "\n"
        "Команды продолжат работать, если их вызвать вручную: /мудрость, /стих, /roast, /stats и другие. "
        "За день до конца тишины я напомню, и её можно будет продлить ещё на неделю.\n"
        "\n"
        f"За: {yes_count} из {get_silence_vote_threshold()} | Против: {no_count}\n"
        f"Голосование открыто до {format_local_datetime(closes_at)}"
    )


async def send_silence_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_chat_id = get_allowed_chat_id()
    until = get_silence_until()
    if allowed_chat_id is None or until is None or datetime.now(timezone.utc) >= until:
        return

    await context.bot.send_message(
        chat_id=allowed_chat_id,
        text=(
            f"🤫 Тишина заканчивается {format_local_datetime(until)}: после этого вернутся вечерние "
            "сводки и воскресное слово. Хотите продлить ещё на неделю — /тишина."
        ),
    )
    logger.info("silence reminder sent chat_id=%s until=%s", allowed_chat_id, until.isoformat())


def schedule_silence_reminder(application: Application) -> None:
    for job in application.job_queue.get_jobs_by_name(SILENCE_REMINDER_JOB_NAME):
        job.schedule_removal()

    until = get_silence_until()
    if until is None:
        return
    remind_at = until - timedelta(days=1)
    if remind_at <= datetime.now(timezone.utc):
        return

    application.job_queue.run_once(send_silence_reminder, when=remind_at, name=SILENCE_REMINDER_JOB_NAME)
    logger.info("silence reminder scheduled at %s", remind_at.isoformat())


async def handle_silence_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not mark_update_processed_once(update, "/silence"):
        return

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None or chat.id != allowed_chat_id:
        logger.info("/silence ignored from chat_id=%s", chat.id)
        return

    sessions: dict[int, dict] = context.application.bot_data.setdefault("silence_sessions", {})
    if sessions.get(chat.id) is not None:
        await message.reply_text("Голосование за тишину уже идёт — кнопки выше 👆")
        return

    now = datetime.now(timezone.utc)
    until = get_silence_until()
    if until is not None and until - now > timedelta(days=1):
        await message.reply_text(
            f"Тишина уже действует до {format_local_datetime(until)}. "
            "Продлить можно будет за день до конца."
        )
        return

    closes_at = now + get_silence_vote_window()
    votes: dict[int, str] = {}
    sent = await message.reply_text(
        build_silence_vote_text(votes, closes_at),
        reply_markup=build_silence_keyboard(),
        parse_mode="HTML",
    )
    session = {"message_id": sent.message_id, "votes": votes, "closes_at": closes_at, "active": True}
    session["job"] = context.application.job_queue.run_once(
        close_silence_vote,
        when=closes_at,
        chat_id=chat.id,
        name=f"silence_vote_{chat.id}",
    )
    sessions[chat.id] = session
    logger.info("/silence vote started chat_id=%s closes_at=%s", chat.id, closes_at.isoformat())


async def finish_silence_vote(context: ContextTypes.DEFAULT_TYPE, chat_id: int, passed: bool) -> None:
    sessions: dict[int, dict] = context.application.bot_data.get("silence_sessions", {})
    session = sessions.pop(chat_id, None)
    if session is None or not session.get("active"):
        return
    session["active"] = False

    yes_count = sum(1 for choice in session["votes"].values() if choice == "yes")
    if passed:
        now = datetime.now(timezone.utc)
        current_until = get_silence_until()
        start = current_until if current_until is not None and current_until > now else now
        until = start + get_silence_duration()
        set_bot_state(SILENCE_UNTIL_KEY, until.isoformat())
        schedule_silence_reminder(context.application)
        text = (
            f"🤫 Решено ({yes_count} голосов): до {format_local_datetime(until)} молчу — без вечерних сводок "
            "и воскресного слова. Если позовёте командой — отвечу. Понял, не навязываюсь 🙂"
        )
    else:
        text = f"Голосование закрыто: за тишину {yes_count} из {get_silence_vote_threshold()}. Всё остаётся как было."

    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=session["message_id"], text=text)
    except Exception:
        logger.exception("/silence failed to edit vote message chat_id=%s", chat_id)
        await context.bot.send_message(chat_id=chat_id, text=text)
    logger.info("/silence vote finished chat_id=%s passed=%s yes=%s", chat_id, passed, yes_count)


async def close_silence_vote(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.job is not None and context.job.chat_id is not None:
        await finish_silence_vote(context, context.job.chat_id, passed=False)


async def handle_silence_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return

    sessions: dict[int, dict] = context.application.bot_data.get("silence_sessions", {})
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
    if voter is None or choice not in ("yes", "no"):
        await query.answer()
        return

    session["votes"][voter.id] = choice
    yes_count = sum(1 for v in session["votes"].values() if v == "yes")
    logger.info("/silence vote chat_id=%s voter_id=%s choice=%s yes=%s", chat.id, voter.id, choice, yes_count)

    if yes_count >= get_silence_vote_threshold():
        await query.answer("Голос принят")
        job = session.get("job")
        if job is not None:
            job.schedule_removal()
        await finish_silence_vote(context, chat.id, passed=True)
        return

    try:
        await query.edit_message_text(
            build_silence_vote_text(session["votes"], session["closes_at"]),
            reply_markup=build_silence_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("/silence failed to edit message chat_id=%s", chat.id)
    await query.answer("Голос принят")


async def handle_morning_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None or query.message is None:
        return

    user = query.from_user
    if user is None:
        await query.answer()
        return

    prune_morning_reactions(context)
    message_id = query.message.message_id
    reactions: dict[int, set[int]] = context.application.bot_data.setdefault(
        "morning_reactions", {}
    )
    created_at_by_message: dict[int, datetime] = context.application.bot_data.setdefault(
        "morning_reaction_created_at", {}
    )
    reacted_user_ids = reactions.setdefault(message_id, set())
    created_at_by_message.setdefault(message_id, datetime.now(timezone.utc))

    if user.id in reacted_user_ids:
        await query.answer("Уже отметились ✅")
        return

    reacted_user_ids.add(user.id)
    await query.answer("Доброе утро! ☀️")

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat.id,
            message_id=message_id,
            reply_markup=build_morning_keyboard(len(reacted_user_ids)),
        )
    except Exception:
        logger.exception(
            "morning reaction: failed to update button chat_id=%s message_id=%s",
            chat.id,
            message_id,
        )

    logger.info(
        "morning reaction added chat_id=%s message_id=%s user_id=%s",
        chat.id,
        message_id,
        user.id,
    )


async def handle_boot_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None or query.message is None:
        return

    user = query.from_user
    if user is None:
        await query.answer()
        return

    prune_boot_reactions(context)
    message_id = query.message.message_id
    reactions: dict[int, set[int]] = context.application.bot_data.setdefault(
        "boot_reactions", {}
    )
    created_at_by_message: dict[int, datetime] = context.application.bot_data.setdefault(
        "boot_reaction_created_at", {}
    )
    reacted_user_ids = reactions.setdefault(message_id, set())
    created_at_by_message.setdefault(message_id, datetime.now(timezone.utc))

    if user.id in reacted_user_ids:
        await query.answer("Уже отметились ✅")
        return

    reacted_user_ids.add(user.id)
    await query.answer("Бутнем ✅")

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat.id,
            message_id=message_id,
            reply_markup=build_boot_keyboard(len(reacted_user_ids)),
        )
    except Exception:
        logger.exception(
            "boot reaction: failed to update button chat_id=%s message_id=%s",
            chat.id,
            message_id,
        )

    logger.info(
        "boot reaction added chat_id=%s message_id=%s user_id=%s",
        chat.id,
        message_id,
        user.id,
    )


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

    character_intro = build_character_intro(context, chat_id)
    if kicked:
        prompt = VOTEKICK_KICKED_PROMPT_TEMPLATE.format(
            character_intro=character_intro,
            name=target_name,
            kick_count=kick_count,
            spare_count=spare_count,
        )
    else:
        prompt = VOTEKICK_SPARED_PROMPT_TEMPLATE.format(
            character_intro=character_intro,
            name=target_name,
            kick_count=kick_count,
            spare_count=spare_count,
        )

    verdict = "кикнут" if kicked else "помилован"
    try:
        result_text = await generate_gemini_text_with_fallback(prompt)
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


async def handle_weekly_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not mark_update_processed_once(update, "/weekly"):
        return

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("/weekly ignored because CHAT_ID is empty")
        await message.reply_text("CHAT_ID не настроен, дайджест недели собрать не получится")
        return

    if chat.id != allowed_chat_id:
        logger.info("/weekly ignored from chat_id=%s", chat.id)
        return

    character_intro = build_character_intro(context, allowed_chat_id)
    prompt = build_weekly_digest(allowed_chat_id, character_intro)
    if prompt is None:
        await message.reply_text("Пока не набралось сводок за неделю, рано подводить итоги")
        return

    try:
        raw_weekly = await generate_gemini_text_for_task(
            prompt,
            model_env="GEMINI_WEEKLY_DIGEST_MODEL",
            model_default=DEFAULT_GEMINI_WEEKLY_DIGEST_MODEL,
        )
        weekly_data = parse_digest_json(raw_weekly)
    except Exception:
        logger.exception("/weekly failed to generate")
        await message.reply_text("Не смог собрать недельный дайджест, попробуйте позже")
        return

    logger.info("/weekly generated successfully chat_id=%s", allowed_chat_id)
    await message.reply_text(format_weekly_digest_html(weekly_data), parse_mode="HTML")


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("автосводка пропущена: CHAT_ID не настроен")
        return
    if is_bot_silenced():
        logger.info("автосводка пропущена: чат проголосовал за тишину")
        return

    tz = get_app_timezone()
    name_map = context.application.bot_data.get("name_map", {})
    rows = fetch_today_messages(allowed_chat_id, tz)

    if not rows:
        logger.info("автосводка пропущена: нет сообщений за день")
        return

    character_intro = build_character_intro(context, allowed_chat_id)
    prompt = build_digest_request(rows, name_map, character_intro)
    try:
        raw_digest = await generate_gemini_text_for_task(
            prompt,
            model_env="GEMINI_DAILY_DIGEST_MODEL",
            model_default=DEFAULT_GEMINI_DAILY_DIGEST_MODEL,
        )
        digest_data = parse_digest_json(raw_digest)
    except Exception:
        logger.exception("автосводка: ошибка при обращении к Gemini")
        return

    save_daily_digest(allowed_chat_id, datetime.now(tz).date().isoformat(), digest_data)

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


def count_all_messages() -> int:
    with closing(connect_db()) as connection:
        row = connection.execute("SELECT COUNT(*) FROM messages").fetchone()

    return row[0] if row else 0


def create_db_backup(dest_dir: Path) -> Path:
    backup_path = dest_dir / f"messages-backup-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.sqlite3"

    source = connect_db()
    try:
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    return backup_path


async def send_db_backup(bot: Bot) -> None:
    backup_chat_id = get_backup_chat_id()
    if backup_chat_id is None:
        raise RuntimeError("BACKUP_CHAT_ID is empty")

    with tempfile.TemporaryDirectory(prefix="rat-bot-backup-") as tmp_dir:
        backup_path = create_db_backup(Path(tmp_dir))
        messages_count = count_all_messages()
        file_size = backup_path.stat().st_size
        backup_datetime = datetime.now(get_app_timezone())

        with backup_path.open("rb") as backup_file:
            await bot.send_document(
                chat_id=backup_chat_id,
                document=backup_file,
                filename=backup_path.name,
                caption=f"Бэкап базы от {backup_datetime:%Y-%m-%d %H:%M}, сообщений: {messages_count}",
            )

    logger.info(
        "backup sent chat_id=%s size_bytes=%s messages_count=%s",
        backup_chat_id,
        file_size,
        messages_count,
    )


async def send_scheduled_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await send_db_backup(context.bot)
    except Exception:
        logger.exception("автобэкап: не удалось отправить бэкап базы")


async def handle_backup_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not mark_update_processed_once(update, "/backup"):
        return

    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is not None and chat.id != allowed_chat_id:
        logger.info("/backup ignored from chat_id=%s", chat.id)
        return

    try:
        await send_db_backup(context.bot)
    except Exception:
        logger.exception("/backup: не удалось отправить бэкап базы")
        await message.reply_text(
            "Не смог отправить бэкап. Проверьте, что бот состоит в целевом чате "
            "(BACKUP_CHAT_ID) и имеет там права отправлять сообщения."
        )
        return

    await message.reply_text("Бэкап базы отправлен.")


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [BotCommand(name, description) for name, description in BOT_COMMANDS]
    )
    logger.info("bot commands registered: %s", [name for name, _ in BOT_COMMANDS])
    await send_repentance_announcement_once(application)
    schedule_silence_reminder(application)


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

    concurrent_updates = get_positive_int_env(
        "TELEGRAM_CONCURRENT_UPDATES",
        DEFAULT_TELEGRAM_CONCURRENT_UPDATES,
    )
    connection_pool_size = get_positive_int_env(
        "TELEGRAM_CONNECTION_POOL_SIZE",
        DEFAULT_TELEGRAM_CONNECTION_POOL_SIZE,
    )
    pool_timeout = get_positive_float_env(
        "TELEGRAM_POOL_TIMEOUT",
        DEFAULT_TELEGRAM_POOL_TIMEOUT,
    )
    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .concurrent_updates(concurrent_updates)
        .connection_pool_size(connection_pool_size)
        .pool_timeout(pool_timeout)
        .build()
    )
    logger.info(
        "Telegram concurrency configured updates=%s connection_pool_size=%s pool_timeout=%s",
        concurrent_updates,
        connection_pool_size,
        pool_timeout,
    )
    application.bot_data["name_map"] = load_name_map()
    application.add_handler(CommandHandler("wisdom", handle_wisdom_command))
    application.add_handler(CommandHandler("verse", handle_verse_command))
    application.add_handler(CommandHandler("silence", handle_silence_command))
    application.add_handler(CommandHandler("roast", handle_roast_command))
    application.add_handler(CommandHandler("votekick", handle_votekick_command))
    application.add_handler(CommandHandler("weekly", handle_weekly_command))
    application.add_handler(CommandHandler("stats", handle_stats_command))
    application.add_handler(CommandHandler("backup", handle_backup_command))
    application.add_handler(CommandHandler("morning", handle_morning_command))
    application.add_handler(CommandHandler("boot", handle_boot_command))
    application.add_handler(CommandHandler("but", handle_boot_command))
    application.add_handler(
        CallbackQueryHandler(handle_votekick_callback, pattern=r"^votekick:")
    )
    application.add_handler(
        CallbackQueryHandler(handle_morning_callback, pattern=r"^morning:react$")
    )
    application.add_handler(
        CallbackQueryHandler(handle_boot_callback, pattern=r"^boot:react$")
    )
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker_message))
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(BOOT_TRIGGER_PATTERN), handle_boot_command)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(WISDOM_TRIGGER_PATTERN), handle_wisdom_command)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(VERSE_TRIGGER_PATTERN), handle_verse_command)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(SILENCE_TRIGGER_PATTERN), handle_silence_command)
    )
    application.add_handler(CallbackQueryHandler(handle_silence_callback, pattern=r"^silence:"))
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

    scripture_time = get_scripture_morning_time(tz)
    if allowed_chat_id is not None and scripture_time is not None:
        scripture_days = get_scripture_morning_days()
        application.job_queue.run_daily(
            send_scripture_morning,
            time=scripture_time,
            days=scripture_days,
            name="scripture_morning",
        )
        logger.info("утреннее слово запланировано на %s (%s), дни=%s", scripture_time, tz, scripture_days)
    else:
        logger.info("утреннее слово не запланировано: CHAT_ID или SCRIPTURE_MORNING_TIME пусты")

    backup_chat_id = get_backup_chat_id()
    backup_time = get_backup_time(tz)
    if backup_chat_id is not None and backup_time is not None:
        application.job_queue.run_daily(send_scheduled_backup, time=backup_time, name="daily_backup")
        logger.info("автобэкап запланирован на %s (%s)", backup_time, tz)
    else:
        logger.warning(
            "автобэкап не запланирован: BACKUP_CHAT_ID=%s BACKUP_TIME=%s",
            backup_chat_id,
            os.getenv("BACKUP_TIME", ""),
        )

    logger.info("bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
