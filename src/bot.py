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
from datetime import datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from PIL import Image, ImageDraw, ImageFont
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
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
TEMPLATES_DIR = Path(os.getenv("TEMPLATES_DIR", BASE_DIR / "templates"))
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "messages.sqlite3"))
LOG_PATH = Path(os.getenv("LOG_PATH", LOG_DIR / "bot.log"))
NAMES_PATH = Path(os.getenv("NAMES_PATH", CONFIG_DIR / "names.txt"))
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_GEMINI_FAST_MODEL = "gemini-2.5-flash-lite"
DEFAULT_GEMINI_FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-2.5-flash-lite")
DEFAULT_GEMINI_MAX_CONCURRENT_REQUESTS = 2
GEMINI_MAX_ATTEMPTS = 3
GEMINI_RETRY_BASE_DELAY_SECONDS = 1.5
GEMINI_RETRY_MAX_DELAY_SECONDS = 20.0
GEMINI_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
GEMINI_TIMEOUT = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)
GEMINI_FAST_TIMEOUT = httpx.Timeout(connect=5.0, read=12.0, write=10.0, pool=5.0)
GEMINI_RAT_MENTION_MAX_ATTEMPTS = 3
GEMINI_ROAST_MAX_ATTEMPTS = 2
GEMINI_ROAST_TIMEOUT = httpx.Timeout(connect=8.0, read=45.0, write=20.0, pool=5.0)
DEFAULT_TELEGRAM_CONCURRENT_UPDATES = 8
DEFAULT_TELEGRAM_CONNECTION_POOL_SIZE = 32
DEFAULT_TELEGRAM_POOL_TIMEOUT = 10.0
DEFAULT_SQLITE_TIMEOUT_SECONDS = 30.0
BOOT_FOLLOWUP_DELAY_SECONDS = 180
IMGFLIP_MEMES_ENDPOINT = "https://api.imgflip.com/get_memes"
MEME_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MEME_MAX_TEXT_BLOCK_HEIGHT_RATIO = 0.20
MEME_MIN_FONT_WIDTH_RATIO = 0.05
MEME_SAFE_TEXT_PATTERN = re.compile(r"[^а-яА-ЯёЁa-zA-Z0-9\s.,!?—\-:;()]")
LOCAL_ROAST_UNSAFE_WORD_PATTERN = re.compile(
    r"\b(?:бля\w*|хуй\w*|хуе\w*|пизд\w*|еба\w*|ёба\w*|уеб\w*|уёб\w*|заеб\w*)\b",
    re.IGNORECASE,
)
MEME_TEMPLATE_LIMIT = 20
MEME_TEMPLATE_INDEX_PATH = TEMPLATES_DIR / "index.json"
MEME_TEMPLATE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

BOT_COMMANDS = [
    ("digest", "Собрать сводку дня (спойлер: будет больно)"),
    ("roast", "Диагноз по сообщениям в чате"),
    ("votekick", "Слово чату: терпим или нет"),
    ("horoscope", "Гороскоп чата на завтра"),
    ("stats", "Статистика чата: активность, топ участников"),
    ("morning", "Прислать утреннее приветствие"),
    ("boot", "Бутнем?"),
]

RAT_CHARACTER_INTRO = (
    "Ты — RAT, саркастичный, остроумный и беспощадно едкий летописец чата, где любят "
    "настоящую злую прожарку. Ты подмечаешь конкретные детали и цепляешься за них, "
    "а не отделываешься общими фразами."
)
HARSH_ROAST_TONE_INSTRUCTION = (
    "Не сдерживайся из вежливости — если есть повод уколоть побольнее, используй его. "
    "Это не дружеское поддразнивание и не добрый сарказм, а жёсткая, злая, едкая прожарка, "
    "которую в этом чате любят и ждут. Можно использовать разговорные нематерные подколы "
    "вроде 'додик', 'лошара', 'неудачник' и похожие язвительные ярлыки, если они попадают "
    "в контекст и усиливают панчлайн."
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
    "по контексту и духу фразы — в том числе как разговорный подкол или злой ярлык, если это "
    "усиливает прожарку и не нарушает запрет на мат. "
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
На основе сообщений за день собери саркастичную сводку.
Сарказм — да, можно язвить и использовать нематерные оскорбительные формулировки.

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

{no_cliches_instruction}
"""

RAT_MENTION_PATTERN = re.compile(r"\bкрыс[а-яё]*\b", re.IGNORECASE)
BOOT_TRIGGER_PATTERN = re.compile(r"^/?бут(?:@\w+)?$", re.IGNORECASE)

RAT_REPLY_PROMPT_TEMPLATE = (
    "{character_intro}\n"
    "Кто-то в чате только что упомянул слово 'крыса' (в каком-то виде) в сообщении ниже:\n"
    "{text}\n"
    "Придумай короткий ОСТРЫЙ саркастичный ответ на 1-2 предложения — дерзкий, "
    "язвительный и ощутимо жёсткий, почти как roast, но без мата. "
    "Обыграй упоминание слова с иронией и атакуй конкретную нелепость в исходной фразе, "
    "а не само слово по шаблону. Можно использовать нематерные оскорбления и язвительные ярлыки. "
    "Не смягчай концовку, не извиняйся и не превращай ответ в дружелюбную шутку. "
    "НЕ вставляй дословную цитату исходного сообщения в кавычках — обыграй его своими словами, "
    "без цитирования. Ответ должен звучать как твоя собственная реплика, а не разбор чужой "
    "фразы с вставками из неё. "
    "НЕ повторяй и не цитируй в кавычках само слово/фразу, которую написал человек "
    "(ни целиком сообщение, ни отдельное слово вроде 'крыса', 'бычок' и т.п.) — реагируй "
    "по существу, без цитирования и без шаблонного открывающего приёма 'ах, [слово], значит?'. "
    "Начни ответ с разных структур, а не всегда с разбора конкретного слова. "
    "Обязательный ракурс ответа: {style_instruction}. "
    "Архетип ответа: {archetype_instruction}. "
    "Ответь только текстом реплики, без кавычек и пояснений."
)
RAT_REPLY_STYLE_INSTRUCTIONS = (
    "короткий разнос в стиле злого стендап-комика",
    "сухой приговор человеку, который сам подставился словом 'крыса'",
    "псевдонаучный диагноз по одной фразе из чата",
    "саркастичное разоблачение трусости, суеты или подозрительности в сообщении",
    "жёсткая бытовая метафора, будто фраза развалилась на глазах",
    "ответ так, будто RAT поймал человека на месте преступления",
)
RAT_REPLY_ARCHETYPE_INSTRUCTIONS = (
    "прямая издёвка без разбора слов, сразу переход к сути",
    "притворное возмущение или оскорблённое достоинство",
    "абсурдный неожиданный поворот темы",
    "угроза-предупреждение в шутливо-зловещем тоне",
    "снисходительное превосходство, будто отвечаешь несмышлёному",
    "короткий встречный вопрос-подкол",
)
ROAST_PROMPT_TEMPLATE = (
    "{character_intro}\n"
    "Вот реальные сообщения человека по имени {name} за последнее время:\n"
    "{messages}\n"
    "Придумай короткий roast (подкол) на основе ЭТИХ РЕАЛЬНЫХ сообщений. "
    "Длина — на твоё усмотрение, главное чтобы был один цельный панчлайн, а не перечисление фактов. "
    "Тон должен быть дерзким, язвительным и ощутимо злым — это полноценная прожарка, но без мата. "
    "Бей в конкретные детали, привычки, повторяющиеся слова и странные выводы человека. "
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
    "Можно использовать жёсткие, но нематерные ярлыки и оскорбительные формулировки. "
    "Не смягчай концовку и не превращай ответ в комплимент. "
    "Архетип задаёт только форму подачи; содержание всё равно должно вырастать ИЗ КОНКРЕТНОЙ детали "
    "и слов именно этого человека. Не подгоняй человека под готовый образ, если это не подтверждается "
    "сообщениями. "
    "Придумай 3 РАЗНЫХ варианта подкола, каждый в своём обязательном ракурсе:\n"
    "{candidate_styles}\n"
    "Все три кандидата должны отталкиваться от одного и того же анкера, но обыгрывать его через "
    "разные архетипы и приёмы юмора. Это не три подкола про три разные темы. "
    "Затем оцени их сам и выбери самый смешной, оригинальный и точно бьющий в детали — как опытный "
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
    "сухой судебный приговор по поведению в чате",
    "псевдонаучный диагноз по сообщениям, будто это клинический случай",
    "жёсткое сравнение с офисной, игровой или бытовой катастрофой",
    "короткий разнос в стиле злого стендап-комика",
    "ироничный портрет человека, который сам себе создал проблему и гордится этим",
    "разоблачение главной привычки человека через одну смешную деталь из сообщений",
    "саркастичная инструкция, как стать таким же невыносимым",
)

NONSENSE_PROMPT_TEMPLATE = (
    "{character_intro}\n"
    "Вот случайная реальная фраза из истории этого чата: '{message}'. "
    "Напиши ОДИН короткий, по-настоящему смешной комментарий-реакцию на эту фразу — "
    "используй конкретный приём: гиперболу, неожиданный вывод, ироничное преувеличение "
    "или абсурдное 'что, если...' развитие мысли. Оттолкнись ИМЕННО от этой фразы, "
    "не добавляй посторонние несвязанные детали и темы — весь юмор должен строиться "
    "вокруг одной этой мысли, а не мешанины из разного. Одно предложение, максимум два. "
    "Без кавычек в ответе."
)

MEME_CAPTION_PROMPT_TEMPLATE = (
    "{character_intro}\n"
    "Вот несколько случайных реальных реплик из истории этого чата: {messages}.\n"
    "Ты делаешь классический мем на шаблоне «{template_name}». "
    "Механика шаблона: {template_description}.\n"
    "Придумай подпись на русском, коротко и смешно, в тему выбранных реплик и в духе персонажа RAT. "
    "Название мем-шаблона: '{template_name}'. Ты наверняка знаешь классическую структуру и механику "
    "этого формата (например: сравнение/выбор между двумя вариантами, реакция-разоблачение, "
    "нарастающий абсурд, и т.п.) — используй именно эту механику при распределении текста между "
    "top_text и bottom_text, а не просто две произвольные фразы. Если не уверен в точной механике "
    "конкретного шаблона — сделай раскладку в духе 'ожидание/сначала' сверху и "
    "'неожиданный поворот/на самом деле' снизу.\n\n"
    "ВАЖНО про сам панчлайн: он должен содержать настоящий неожиданный поворот, контраст или "
    "разоблачение — а не просто описывать ситуацию или констатировать факт. Плохой пример механики "
    "(не для копирования, просто для понимания разницы): констатация 'все будут переживать' — "
    "это плоско. Хороший панчлайн вскрывает нелепость, доводит до абсурда, или переворачивает "
    "ожидание. Представь, что рассказываешь это лучшему другу, чтобы он заржал в голос, "
    "а не просто кивнул понимающе.\n"
    "Избегай формата 'A = B, следствие C' как двух несвязанных ярлыков — вместо этого top_text "
    "и bottom_text должны звучать как одна цельная мысль с сюжетным поворотом между ними, "
    "а не как две отдельные подписи-этикетки. "
    "top_text и bottom_text должны быть короткими фразами, максимум 5-6 слов каждая — "
    "как в настоящих мемах, не пиши длинные предложения. "
    "Желательно, чтобы каждая фраза умещалась в одну строку. "
    "Используй только обычные русские и латинские буквы, цифры и стандартную пунктуацию "
    "(точка, запятая, восклицательный/вопросительный знак, тире-минус). НЕ используй эмодзи "
    "и специальные юникод-символы (например ➡️, 🤝, ✅ и подобные) — только простой текст. "
    "Если нижняя строка не нужна, верни её пустой строкой.\n"
    "Верни ТОЛЬКО строгий валидный JSON без markdown, без пояснений, строго такого вида:\n"
    '{{"top_text": "верхняя строка", "bottom_text": "нижняя строка"}}'
)

HOROSCOPE_PROMPT_TEMPLATE = (
    "{character_intro}\n"
    "Сегодня в чате обсуждали (кратко): {topics}.\n"
    "Придумай шуточный гороскоп на завтра для каждого из следующих людей: {names}.\n"
    "Каждый прогноз — 1 короткое ироничное предложение, в шуточно-астрологическом стиле "
    "(звёзды, планеты, знаки), можно отсылаться к характеру человека по его недавним репликам "
    "в чате, если это уместно. Можно язвить и использовать нематерные оскорбительные формулировки.\n"
    "Верни СТРОГО валидный JSON без markdown, формат:\n"
    '{{"horoscopes": [{{"name": "Имя", "forecast": "текст прогноза"}}]}}\n\n'
    + NO_CLICHES_INSTRUCTION
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
    "Напиши короткое смешное прощание/эпитафию, 2-3 предложения, в жёстком саркастичном тоне, "
    "можно с нематерными оскорбительными формулировками.\n"
    + NO_CLICHES_INSTRUCTION
)

VOTEKICK_SPARED_PROMPT_TEMPLATE = (
    "{character_intro}\n"
    "Голосование за изгнание {name} не набрало нужного числа голосов "
    "(голоса: {kick_count} за, {spare_count} против) — чат смилостивился, {name} остаётся в чате. "
    "Придумай короткий саркастичный комментарий по этому поводу, 2-3 предложения, в жёстком едком тоне, "
    "можно с нематерными оскорбительными формулировками.\n"
    + NO_CLICHES_INSTRUCTION
)


logger = logging.getLogger(__name__)
_gemini_semaphore: asyncio.Semaphore | None = None
_gemini_semaphore_limit: int | None = None


def meme_template_description(template_name: str) -> str:
    normalized = template_name.lower()
    descriptions = {
        "drake hotline bling": "отвергает первый вариант и одобряет второй",
        "distracted boyfriend": "персонаж отвлекается от привычного выбора на более соблазнительный вариант",
        "two buttons": "мучительный выбор между двумя неудобными вариантами",
        "left exit": "резкий и нелепый уход с нормального пути к неожиданному решению",
        "change my mind": "самоуверенное спорное утверждение, которое предлагается оспорить",
        "expanding brain": "нарастающие уровни всё более странного или якобы гениального мышления",
        "buff doge": "контраст сильной старой версии и слабой новой версии",
        "woman yelling at cat": "эмоциональное обвинение против невозмутимой реакции",
        "uno draw 25 cards": "человек выбирает наказание вместо простого действия",
        "running away balloon": "кто-то теряет контроль над желаемой вещью или идеей",
        "bernie i am once again asking": "настойчивая повторная просьба о чём-то",
        "gru's plan": "план звучит нормально, пока внезапно не становится абсурдным",
        "always has been": "внезапное признание, что странная правда была очевидна всегда",
        "disaster girl": "невинное довольство на фоне хаоса",
        "mocking spongebob": "насмешливое передразнивание чужой фразы",
        "one does not simply": "нельзя просто так взять и сделать очевидную вещь",
    }

    for key, description in descriptions.items():
        if key in normalized:
            return description

    return (
        "обыграй верхнюю и нижнюю строку в контрасте; если шаблон подсказывает роли, "
        "используй их естественно"
    )


def list_meme_template_files() -> list[Path]:
    if not TEMPLATES_DIR.exists():
        return []

    return sorted(
        path
        for path in TEMPLATES_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in MEME_TEMPLATE_IMAGE_SUFFIXES
    )


def load_meme_template_index() -> dict[str, str]:
    if not MEME_TEMPLATE_INDEX_PATH.exists():
        return {}

    try:
        raw = json.loads(MEME_TEMPLATE_INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("failed to read meme template index: %s", MEME_TEMPLATE_INDEX_PATH)
        return {}

    if not isinstance(raw, dict):
        logger.warning("meme template index has unexpected shape: %s", MEME_TEMPLATE_INDEX_PATH)
        return {}

    return {str(template_id): str(name) for template_id, name in raw.items()}


def get_random_meme_template() -> tuple[Path, str] | None:
    template_files = list_meme_template_files()
    if not template_files:
        return None

    template_path = random.choice(template_files)
    index = load_meme_template_index()
    template_name = index.get(template_path.stem, template_path.stem)
    return template_path, template_name


def get_template_file_suffix(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in MEME_TEMPLATE_IMAGE_SUFFIXES:
        return suffix
    return ".jpg"


def ensure_meme_templates() -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    existing_templates = list_meme_template_files()
    if existing_templates:
        logger.info(
            "meme templates found locally: dir=%s count=%s; skipping download",
            TEMPLATES_DIR,
            len(existing_templates),
        )
        return

    logger.info("meme templates not found; checking access to %s", IMGFLIP_MEMES_ENDPOINT)
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(IMGFLIP_MEMES_ENDPOINT)
            response.raise_for_status()
            data = response.json()
            memes = data.get("data", {}).get("memes", [])
            if not data.get("success") or not isinstance(memes, list):
                logger.warning("Imgflip returned unexpected meme list response: %s", data)
                return

            downloaded: dict[str, str] = {}
            for meme in memes[:MEME_TEMPLATE_LIMIT]:
                template_id = str(meme.get("id", "")).strip()
                template_name = str(meme.get("name", "")).strip()
                template_url = str(meme.get("url", "")).strip()
                if not template_id or not template_name or not template_url:
                    continue

                suffix = get_template_file_suffix(template_url)
                template_path = TEMPLATES_DIR / f"{template_id}{suffix}"
                try:
                    image_response = client.get(template_url)
                    image_response.raise_for_status()
                    template_path.write_bytes(image_response.content)
                except Exception:
                    logger.exception(
                        "failed to download meme template id=%s name=%r url=%s",
                        template_id,
                        template_name,
                        template_url,
                    )
                    continue

                downloaded[template_id] = template_name

    except httpx.RequestError as exc:
        logger.warning(
            "Imgflip template download skipped: no network access to api.imgflip.com (%s)",
            exc,
        )
        return
    except Exception:
        logger.exception("Imgflip template download failed")
        return

    if not downloaded:
        logger.warning("Imgflip template download finished with zero downloaded templates")
        return

    MEME_TEMPLATE_INDEX_PATH.write_text(
        json.dumps(downloaded, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    logger.info(
        "downloaded meme templates from Imgflip: dir=%s count=%s",
        TEMPLATES_DIR,
        len(downloaded),
    )


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


def get_weekly_digest_time(tz: ZoneInfo) -> time | None:
    raw = os.getenv("WEEKLY_DIGEST_TIME", "").strip()
    if not raw:
        return None

    try:
        hour_str, minute_str = raw.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise RuntimeError(f"WEEKLY_DIGEST_TIME must be in HH:MM format, got {raw!r}") from exc

    return time(hour=hour, minute=minute, tzinfo=tz)


def get_morning_greeting_time(tz: ZoneInfo) -> time | None:
    raw = os.getenv("MORNING_GREETING_TIME", "").strip()
    if not raw:
        return None

    try:
        hour_str, minute_str = raw.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise RuntimeError(f"MORNING_GREETING_TIME must be in HH:MM format, got {raw!r}") from exc

    return time(hour=hour, minute=minute, tzinfo=tz)


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


def get_nonsense_chain_refresh_interval() -> timedelta:
    raw = os.getenv("NONSENSE_CHAIN_REFRESH_MINUTES", "60").strip()
    try:
        minutes = float(raw)
    except ValueError:
        minutes = 60.0

    return timedelta(minutes=minutes)


def get_nonsense_cooldown() -> timedelta:
    raw = os.getenv("NONSENSE_COOLDOWN_MINUTES", "30").strip()
    try:
        minutes = float(raw)
    except ValueError:
        minutes = 30.0

    return timedelta(minutes=minutes)


def get_nonsense_reaction_chance() -> float:
    raw = os.getenv("NONSENSE_REACTION_CHANCE", "0.03").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.03


def get_nonsense_min_messages() -> int:
    raw = os.getenv("NONSENSE_MIN_MESSAGES", "300").strip()
    try:
        return int(raw)
    except ValueError:
        return 300


def get_meme_render_chance() -> float:
    raw = os.getenv("MEME_RENDER_CHANCE", "0.4").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.4


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
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id INTEGER PRIMARY KEY,
                handler TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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


def count_chat_messages(chat_id: int) -> int:
    with closing(connect_db()) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()

    return row[0] if row else 0


def count_non_bot_chat_messages(chat_id: int) -> int:
    with closing(connect_db()) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*)
            FROM messages
            WHERE chat_id = ?
              AND (is_bot IS NULL OR is_bot = 0)
              AND trim(text) != ''
            """,
            (chat_id,),
        ).fetchone()

    return row[0] if row else 0


def fetch_all_chat_texts(chat_id: int) -> list[str]:
    with closing(connect_db()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT text
            FROM messages
            WHERE chat_id = ?
              AND (is_bot IS NULL OR is_bot = 0)
            ORDER BY message_datetime ASC, message_id ASC
            """,
            (chat_id,),
        ).fetchall()

    return [row["text"] for row in rows if row["text"] and row["text"].strip()]


def fetch_random_messages_sample(chat_id: int, n: int = 1) -> list[str]:
    if n <= 0:
        return []

    with closing(connect_db()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT text
            FROM messages
            WHERE chat_id = ?
              AND (is_bot IS NULL OR is_bot = 0)
              AND trim(text) != ''
              AND length(trim(text)) - length(replace(trim(text), ' ', '')) >= 3
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (chat_id, n),
        ).fetchall()

    return [
        row["text"].strip()
        for row in rows
        if row["text"] and len(row["text"].strip().split()) >= 4
    ]


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
            HARSH_ROAST_TONE_INSTRUCTION,
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


def format_recent_rat_replies_instruction(recent_replies: list[str]) -> str:
    if not recent_replies:
        return ""

    return (
        "\n\nВот твои последние ответы на упоминание 'крысы' в этом чате: "
        f"{' | '.join(recent_replies)}. "
        "НЕ повторяй эти формулировки, образы и структуру фраз — придумай принципиально новый ракурс шутки."
    )


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
        "{name}, Gemini сейчас лег под нагрузкой, но база улик не молчит. После фразы «{detail}» "
        "даже автосводка выглядит как серьезная аналитика, а не попытка чата собрать мысль по частям.",
        "{name}, внешний мозг временно недоступен, поэтому приговор короткий: «{detail}» уже само "
        "по себе звучит как заявка на отдельную папку в архиве странных решений.",
        "{name}, нейросеть не ответила, зато история чата справилась без нее. «{detail}» — это тот "
        "случай, когда подкол не генерируют, а аккуратно достают из протокола.",
    )
    return random.choice(templates).format(name=name, detail=detail)


def format_recent_nonsense_phrases_instruction(recent_phrases: list[str]) -> str:
    if not recent_phrases:
        return ""

    return (
        "\n\nВот твои последние такие фразы: "
        f"{' | '.join(recent_phrases)}. "
        "Не повторяй эти образы и структуру."
    )


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
    prompt = DIGEST_PROMPT_TEMPLATE.format(
        character_intro=character_intro,
        no_cliches_instruction=NO_CLICHES_INSTRUCTION,
    )
    return f"{prompt}\n\nСообщения за день:\n" + build_messages_transcript(messages, name_map)


def build_horoscope_request(
    messages: list[sqlite3.Row],
    name_map: dict[str, str],
    names: list[str],
    character_intro: str,
) -> str:
    transcript = build_messages_transcript(messages, name_map)
    topics = transcript if transcript else "ничего особенного"
    return HOROSCOPE_PROMPT_TEMPLATE.format(
        character_intro=character_intro,
        topics=topics,
        names=", ".join(names),
    )


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


def build_markov_chain(texts: list[str]) -> dict[tuple[str, str], list[str]]:
    chain: dict[tuple[str, str], list[str]] = {}
    for text in texts:
        words = text.split()
        if len(words) < 3:
            continue
        for i in range(len(words) - 2):
            key = (words[i], words[i + 1])
            chain.setdefault(key, []).append(words[i + 2])

    return chain


def generate_nonsense_phrase(chain: dict[tuple[str, str], list[str]]) -> str | None:
    if not chain:
        return None

    words = list(random.choice(list(chain.keys())))
    target_length = random.randint(5, 20)

    while len(words) < target_length:
        next_words = chain.get((words[-2], words[-1]))
        if not next_words:
            break
        words.append(random.choice(next_words))

    return " ".join(words)


def normalize_meme_text(text: str) -> str:
    safe_text = MEME_SAFE_TEXT_PATTERN.sub("", str(text or ""))
    return " ".join(safe_text.split()).strip().upper()


def split_word_to_fit(
    draw: ImageDraw.ImageDraw,
    word: str,
    font: ImageFont.FreeTypeFont,
    stroke_width: int,
    max_width: int,
) -> list[str]:
    parts: list[str] = []
    current = ""
    for char in word:
        candidate = current + char
        bbox = draw.textbbox((0, 0), candidate, font=font, stroke_width=stroke_width)
        if current and bbox[2] - bbox[0] > max_width:
            parts.append(current)
            current = char
        else:
            current = candidate

    if current:
        parts.append(current)

    return parts or [word]


def wrap_meme_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    stroke_width: int,
    max_width: int,
) -> list[str]:
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font, stroke_width=stroke_width)
        if not current or bbox[2] - bbox[0] <= max_width:
            current = candidate
            continue

        lines.append(current)
        word_bbox = draw.textbbox((0, 0), word, font=font, stroke_width=stroke_width)
        if word_bbox[2] - word_bbox[0] <= max_width:
            current = word
        else:
            split_parts = split_word_to_fit(draw, word, font, stroke_width, max_width)
            lines.extend(split_parts[:-1])
            current = split_parts[-1]

    if current:
        lines.append(current)

    return lines


def measure_meme_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    stroke_width: int,
    line_gap: int,
) -> tuple[int, int]:
    if not lines:
        return 0, 0

    widths = []
    heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        widths.append(bbox[2] - bbox[0])
        heights.append(bbox[3] - bbox[1])

    return max(widths), sum(heights) + line_gap * (len(lines) - 1)


def get_meme_font_settings(font_size: int) -> tuple[ImageFont.FreeTypeFont, int, int]:
    font = ImageFont.truetype(MEME_FONT_PATH, font_size)
    stroke_width = max(2, font_size // 14)
    line_gap = max(3, font_size // 8)
    return font, stroke_width, line_gap


def truncate_meme_text_to_fit(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    stroke_width: int,
    line_gap: int,
    max_width: int,
    max_height: int,
) -> tuple[list[str], int]:
    words = text.split()
    if not words:
        return [], 0

    for word_count in range(len(words), 0, -1):
        trimmed = " ".join(words[:word_count]).rstrip(" ,.!?;:…")
        candidate = f"{trimmed}…" if trimmed else "…"
        lines = wrap_meme_lines(draw, candidate, font, stroke_width, max_width)
        _, block_height = measure_meme_block(draw, lines, font, stroke_width, line_gap)
        if block_height <= max_height:
            return lines, block_height

    lines = wrap_meme_lines(draw, "…", font, stroke_width, max_width)
    _, block_height = measure_meme_block(draw, lines, font, stroke_width, line_gap)
    if block_height <= max_height:
        return lines, block_height

    return [], 0


def fit_meme_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_font_size: int,
    min_font_size: int,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.FreeTypeFont, int, int, list[str], int]:
    if not text:
        font, stroke_width, line_gap = get_meme_font_settings(min_font_size)
        return font, stroke_width, line_gap, [], 0

    for font_size in range(max_font_size, min_font_size - 1, -2):
        font, stroke_width, line_gap = get_meme_font_settings(font_size)
        lines = wrap_meme_lines(draw, text, font, stroke_width, max_width)
        _, block_height = measure_meme_block(draw, lines, font, stroke_width, line_gap)
        if block_height <= max_height:
            return font, stroke_width, line_gap, lines, block_height

    font, stroke_width, line_gap = get_meme_font_settings(min_font_size)
    lines = wrap_meme_lines(draw, text, font, stroke_width, max_width)
    _, block_height = measure_meme_block(draw, lines, font, stroke_width, line_gap)
    if block_height > max_height:
        lines, block_height = truncate_meme_text_to_fit(
            draw,
            text,
            font,
            stroke_width,
            line_gap,
            max_width,
            max_height,
        )

    return font, stroke_width, line_gap, lines, block_height


def draw_meme_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    stroke_width: int,
    line_gap: int,
    image_width: int,
    start_y: int,
) -> None:
    y = start_y
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        line_width = bbox[2] - bbox[0]
        line_height = bbox[3] - bbox[1]
        x = (image_width - line_width) / 2 - bbox[0]
        draw.text(
            (x, y - bbox[1]),
            line,
            font=font,
            fill="white",
            stroke_width=stroke_width,
            stroke_fill="black",
        )
        y += line_height + line_gap


def render_meme_template(image_path: Path, top_text: str, bottom_text: str) -> bytes:
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    horizontal_padding = max(10, width // 35)
    vertical_padding = max(10, height // 35)
    max_text_width = max(1, width - horizontal_padding * 2)

    top = normalize_meme_text(top_text)
    bottom = normalize_meme_text(bottom_text)
    if not top and not bottom:
        raise ValueError("meme caption is empty")

    max_font_size = max(24, min(width // 9, height // 6))
    min_font_size = min(max_font_size, max(14, int(width * MEME_MIN_FONT_WIDTH_RATIO)))
    max_block_height = max(1, int(height * MEME_MAX_TEXT_BLOCK_HEIGHT_RATIO))
    if top and bottom:
        max_block_height = min(
            max_block_height,
            max(1, (height - vertical_padding * 3) // 2),
        )

    top_font, top_stroke_width, top_line_gap, top_lines, top_height = fit_meme_block(
        draw,
        top,
        max_font_size,
        min_font_size,
        max_text_width,
        max_block_height,
    )
    (
        bottom_font,
        bottom_stroke_width,
        bottom_line_gap,
        bottom_lines,
        bottom_height,
    ) = fit_meme_block(
        draw,
        bottom,
        max_font_size,
        min_font_size,
        max_text_width,
        max_block_height,
    )

    if top_lines:
        draw_meme_block(
            draw,
            top_lines,
            top_font,
            top_stroke_width,
            top_line_gap,
            width,
            vertical_padding,
        )
    if bottom_lines:
        bottom_y = height - vertical_padding - bottom_height
        draw_meme_block(
            draw,
            bottom_lines,
            bottom_font,
            bottom_stroke_width,
            bottom_line_gap,
            width,
            bottom_y,
        )

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


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


def parse_digest_json(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*\n", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()

    return json.loads(cleaned)


def parse_meme_caption_json(raw_text: str) -> tuple[str, str]:
    data = parse_digest_json(raw_text)
    if not isinstance(data, dict):
        raise ValueError("meme caption response must be a JSON object")

    top_text = " ".join(str(data.get("top_text", "")).split()).strip()
    bottom_text = " ".join(str(data.get("bottom_text", "")).split()).strip()
    if not top_text and not bottom_text:
        raise ValueError("meme caption response is empty")

    return top_text, bottom_text


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


def serialize_meme_caption_pair(top_text: str, bottom_text: str) -> str:
    return json.dumps(
        {"top_text": top_text, "bottom_text": bottom_text},
        ensure_ascii=False,
        sort_keys=True,
    )


def format_meme_caption_text(top_text: str, bottom_text: str) -> str:
    lines = [line for line in (top_text.strip(), bottom_text.strip()) if line]
    return "\n".join(lines)


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
                InlineKeyboardButton("🔪 Кикнуть", callback_data="votekick:kick"),
                InlineKeyboardButton("🙏 Помиловать", callback_data="votekick:spare"),
            ]
        ]
    )


def build_votekick_text(target_name: str, votes: dict[int, str]) -> str:
    kick_count = sum(1 for choice in votes.values() if choice == "kick")
    spare_count = sum(1 for choice in votes.values() if choice == "spare")
    return f"🔪 Голосование: выгоняем {target_name} из чата?\n\nЗа: {kick_count} | Против: {spare_count}"


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


async def send_scheduled_morning_greeting(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await send_morning_greeting(context, source="scheduled")
    except Exception:
        logger.exception("утреннее приветствие: не удалось отправить сообщение")


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


async def maybe_reply_to_rat_mention(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    text = message.text
    if not text or not RAT_MENTION_PATTERN.search(text):
        return False

    author = message.from_user
    if author is not None and author.is_bot:
        return False

    chat_id = message.chat_id

    character_intro = build_character_intro(context, chat_id)
    recent_replies = get_recent_generated_texts(context, "recent_rat_replies", chat_id)
    prompt = RAT_REPLY_PROMPT_TEMPLATE.format(
        character_intro=character_intro,
        text=text,
        style_instruction=random.choice(RAT_REPLY_STYLE_INSTRUCTIONS),
        archetype_instruction=random.choice(RAT_REPLY_ARCHETYPE_INSTRUCTIONS),
    )
    prompt += format_recent_rat_replies_instruction(recent_replies)
    logger.info(
        "rat mention detected chat_id=%s message_id=%s text=%r",
        chat_id,
        message.message_id,
        text[:120],
    )
    fast_model = get_gemini_model("GEMINI_FAST_MODEL", DEFAULT_GEMINI_FAST_MODEL)
    primary_model = get_gemini_model()
    try:
        try:
            reply = await generate_gemini_text(
                prompt,
                max_attempts=GEMINI_RAT_MENTION_MAX_ATTEMPTS,
                timeout=GEMINI_FAST_TIMEOUT,
                model=fast_model,
            )
        except Exception as exc:
            if primary_model == fast_model:
                raise

            logger.warning(
                "rat mention reply: fast Gemini model failed, retrying with primary model "
                "fast_model=%s primary_model=%s error=%r",
                fast_model,
                primary_model,
                exc,
            )
            reply = await generate_gemini_text(
                prompt,
                max_attempts=1,
                timeout=GEMINI_FAST_TIMEOUT,
                model=primary_model,
            )
    except Exception:
        logger.exception(
            "rat mention reply: Gemini error fast_model=%s primary_model=%s",
            fast_model,
            primary_model,
        )
        return True

    await message.reply_text(reply)
    remember_generated_text(context, "recent_rat_replies", chat_id, reply)
    logger.info("rat mention triggered text=%r reply=%r", text[:120], reply[:120])
    return True


async def get_or_build_markov_chain(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> dict[tuple[str, str], list[str]]:
    cache: dict[int, dict] = context.application.bot_data.setdefault("markov_chain_cache", {})
    now = datetime.now(timezone.utc)
    refresh_interval = get_nonsense_chain_refresh_interval()

    cached = cache.get(chat_id)
    if cached is not None and now - cached["built_at"] < refresh_interval:
        return cached["chain"]

    texts = fetch_all_chat_texts(chat_id)
    chain = build_markov_chain(texts)
    cache[chat_id] = {"chain": chain, "built_at": now}
    logger.info(
        "markov chain rebuilt chat_id=%s texts=%s chain_keys=%s",
        chat_id,
        len(texts),
        len(chain),
    )
    return chain


async def generate_nonsense_phrase_gemini(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> str | None:
    sample = fetch_random_messages_sample(chat_id)
    if not sample:
        return None

    character_intro = build_character_intro(context, chat_id)
    recent_phrases = get_recent_generated_texts(context, "recent_nonsense_phrases", chat_id)
    prompt = NONSENSE_PROMPT_TEMPLATE.format(
        character_intro=character_intro,
        message=sample[0],
    )
    prompt += format_recent_nonsense_phrases_instruction(recent_phrases)

    try:
        phrase = await generate_gemini_text(prompt)
    except Exception:
        logger.exception("nonsense reaction: Gemini error chat_id=%s", chat_id)
        return None

    phrase = " ".join(phrase.split()).strip()
    if not phrase:
        return None

    return phrase


async def generate_meme_caption_gemini(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    template_name: str,
) -> tuple[str, str] | None:
    sample = fetch_random_messages_sample(chat_id)
    if not sample:
        return None

    character_intro = build_character_intro(context, chat_id)
    recent_phrases = get_recent_generated_texts(context, "recent_nonsense_phrases", chat_id)
    prompt = MEME_CAPTION_PROMPT_TEMPLATE.format(
        character_intro=character_intro,
        messages=" | ".join(sample),
        template_name=template_name,
        template_description=meme_template_description(template_name),
    )
    prompt += format_recent_nonsense_phrases_instruction(recent_phrases)

    try:
        raw_caption = await generate_gemini_text(prompt)
        return parse_meme_caption_json(raw_caption)
    except Exception:
        logger.exception(
            "nonsense reaction: Gemini meme caption error chat_id=%s template=%r",
            chat_id,
            template_name,
        )
        return None


async def maybe_send_nonsense_reaction(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    if random.random() > get_nonsense_reaction_chance():
        return

    last_triggered_at: dict[int, datetime] = context.application.bot_data.setdefault(
        "nonsense_last_at", {}
    )
    now = datetime.now(timezone.utc)
    cooldown = get_nonsense_cooldown()
    previous = last_triggered_at.get(chat_id)
    if previous is not None and now - previous < cooldown:
        return

    if count_non_bot_chat_messages(chat_id) < get_nonsense_min_messages():
        return

    sent_as_meme = False
    generated_text_for_memory: str | None = None
    template = get_random_meme_template()
    if template is not None and random.random() <= get_meme_render_chance():
        template_path, template_name = template
        caption = await generate_meme_caption_gemini(context, chat_id, template_name)
        if caption is not None:
            top_text, bottom_text = caption
            fallback_text = format_meme_caption_text(top_text, bottom_text)
            generated_text_for_memory = serialize_meme_caption_pair(top_text, bottom_text)
            if fallback_text:
                try:
                    meme_bytes = render_meme_template(template_path, top_text, bottom_text)
                    await message.reply_photo(photo=BytesIO(meme_bytes))
                    sent_as_meme = True
                except Exception:
                    logger.exception(
                        "nonsense reaction: meme render failed chat_id=%s template=%s, "
                        "falling back to text",
                        chat_id,
                        template_path.name,
                    )
                    await message.reply_text(fallback_text)

    if generated_text_for_memory is None:
        phrase = await generate_nonsense_phrase_gemini(context, chat_id)
        if phrase is None:
            return

        generated_text_for_memory = phrase
        await message.reply_text(phrase)

    last_triggered_at[chat_id] = now
    remember_generated_text(
        context,
        "recent_nonsense_phrases",
        chat_id,
        generated_text_for_memory,
    )
    logger.info(
        "nonsense reaction triggered chat_id=%s kind=%s text=%r",
        chat_id,
        "meme" if sent_as_meme else "text",
        generated_text_for_memory[:200],
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
    today_start_utc, today_end_utc = get_today_bounds_utc(tz)
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


async def handle_text_reactions(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    try:
        rat_triggered = await maybe_reply_to_rat_mention(message, context)
        if not rat_triggered:
            await maybe_send_nonsense_reaction(message, context, chat_id)
    except Exception:
        logger.exception(
            "text reaction task failed chat_id=%s message_id=%s",
            chat_id,
            message.message_id,
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

    context.application.create_task(
        handle_text_reactions(message, context, chat.id),
        update=update,
        name=f"text-reactions:{chat.id}:{message.message_id}",
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


async def handle_digest_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not mark_update_processed_once(update, "/digest"):
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

    character_intro = build_character_intro(context, allowed_chat_id)
    prompt = build_digest_request(rows, name_map, character_intro)

    try:
        raw_digest = await generate_gemini_text(prompt)
        digest_data = parse_digest_json(raw_digest)
    except Exception:
        logger.exception("failed to generate digest with Gemini")
        await message.reply_text("Не смог собрать сводку, попробуйте позже")
        return

    save_daily_digest(allowed_chat_id, datetime.now(tz).date().isoformat(), digest_data)

    logger.info("/digest generated successfully chat_id=%s", allowed_chat_id)
    await message.reply_text(format_digest_html(digest_data), parse_mode="HTML")


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
        raw_roast = await generate_gemini_text_with_fallback(
            prompt,
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
    if not mark_update_processed_once(update, "/horoscope"):
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
    character_intro = build_character_intro(context, allowed_chat_id)
    prompt = build_horoscope_request(rows, name_map, names, character_intro)

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
        raw_weekly = await generate_gemini_text(prompt)
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

    tz = get_app_timezone()
    name_map = context.application.bot_data.get("name_map", {})
    rows = fetch_today_messages(allowed_chat_id, tz)

    if not rows:
        logger.info("автосводка пропущена: нет сообщений за день")
        return

    character_intro = build_character_intro(context, allowed_chat_id)
    prompt = build_digest_request(rows, name_map, character_intro)

    try:
        raw_digest = await generate_gemini_text(prompt)
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


async def send_weekly_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_chat_id = get_allowed_chat_id()
    if allowed_chat_id is None:
        logger.warning("недельный дайджест пропущен: CHAT_ID не настроен")
        return

    character_intro = build_character_intro(context, allowed_chat_id)
    prompt = build_weekly_digest(allowed_chat_id, character_intro)
    if prompt is None:
        logger.info("недельный дайджест пропущен: нет данных за неделю")
        return

    try:
        raw_weekly = await generate_gemini_text(prompt)
        weekly_data = parse_digest_json(raw_weekly)
    except Exception:
        logger.exception("недельный дайджест: ошибка при обращении к Gemini")
        return

    await context.bot.send_message(
        chat_id=allowed_chat_id,
        text=format_weekly_digest_html(weekly_data),
        parse_mode="HTML",
    )
    logger.info("недельный дайджест отправлен chat_id=%s", allowed_chat_id)


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


def main() -> None:
    setup_logging()
    init_db()
    ensure_meme_templates()

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
    application.add_handler(CommandHandler("digest", handle_digest_command))
    application.add_handler(CommandHandler("roast", handle_roast_command))
    application.add_handler(CommandHandler("votekick", handle_votekick_command))
    application.add_handler(CommandHandler("horoscope", handle_horoscope_command))
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

    weekly_digest_time = get_weekly_digest_time(tz)
    if allowed_chat_id is not None and weekly_digest_time is not None:
        application.job_queue.run_daily(
            send_weekly_digest, time=weekly_digest_time, days=(4,), name="weekly_digest"
        )
        logger.info("недельный дайджест запланирован на пятницу %s (%s)", weekly_digest_time, tz)
    else:
        logger.warning(
            "недельный дайджест не запланирован: CHAT_ID=%s WEEKLY_DIGEST_TIME=%s",
            allowed_chat_id,
            os.getenv("WEEKLY_DIGEST_TIME", ""),
        )

    morning_greeting_time = get_morning_greeting_time(tz)
    if allowed_chat_id is not None and morning_greeting_time is not None:
        application.job_queue.run_daily(
            send_scheduled_morning_greeting,
            time=morning_greeting_time,
            name="morning_greeting",
        )
        logger.info("утреннее приветствие запланировано на %s (%s)", morning_greeting_time, tz)
    else:
        logger.warning(
            "утреннее приветствие не запланировано: CHAT_ID=%s MORNING_GREETING_TIME=%s",
            allowed_chat_id,
            os.getenv("MORNING_GREETING_TIME", ""),
        )

    logger.info("bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
