"""Предпросмотр нового поведения бота на реальной базе — ничего не отправляет в Telegram.

Показывает в терминале: объявление, примеры /мудрость и /стих, утреннее слово,
вечернюю сводку за выбранный день и (по желанию) дружеский подкол участника.
Сводка и подкол генерируются через Gemini, как в боте.

Запуск на сервере — см. DEPLOY.md, раздел «Предпросмотр перед деплоем».
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import random
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from src import bot, scripture

TAG_RE = re.compile(r"</?(?:b|i|blockquote)>")


def as_text(message_html: str) -> str:
    return html.unescape(TAG_RE.sub("", message_html))


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def fake_context() -> SimpleNamespace:
    return SimpleNamespace(application=SimpleNamespace(bot_data={}))


async def preview_digest(chat_id: int, day: date) -> None:
    tz = bot.get_app_timezone()
    rows = bot.fetch_messages_for_day(chat_id, tz, day)
    section(f"ВЕЧЕРНЯЯ СВОДКА за {day.isoformat()} (сообщений: {len(rows)})")
    if not rows:
        print("За этот день сообщений нет — бот сводку бы не отправил.")
        return

    prompt = bot.build_digest_request(rows, bot.load_name_map(), bot.build_character_intro(fake_context(), chat_id))
    raw = await bot.generate_gemini_text_for_task(
        prompt,
        model_env="GEMINI_DAILY_DIGEST_MODEL",
        model_default=bot.DEFAULT_GEMINI_DAILY_DIGEST_MODEL,
    )
    print(as_text(bot.format_digest_html(bot.parse_digest_json(raw))))


async def preview_roast(chat_id: int, username: str) -> None:
    username = username.removeprefix("@")
    section(f"/roast @{username} (дружеский подкол)")
    user_id = bot.resolve_user_id_by_username(chat_id, username)
    since = (datetime.now(timezone.utc) - timedelta(days=bot.get_roast_lookback_days())).isoformat()
    rows = bot.fetch_user_messages(chat_id, user_id=user_id, username=username, since_utc=since) or bot.fetch_user_messages(
        chat_id, user_id=user_id, username=username, since_utc=None
    )
    if not rows:
        print("Сообщений этого человека в базе нет.")
        return

    name_map = bot.load_name_map()
    name = name_map.get(username.lower()) or rows[-1]["display_name"] or username
    lines = bot.select_roast_source_lines(rows)
    styles = random.sample(bot.ROAST_STYLE_INSTRUCTIONS, k=3)
    prompt = bot.ROAST_PROMPT_TEMPLATE.format(
        character_intro=bot.build_character_intro(fake_context(), chat_id),
        name=name,
        messages="\n".join(f"- {line}" for line in lines),
        candidate_styles="\n".join(f"{i}. Кандидат {i}: {style}" for i, style in enumerate(styles, start=1)),
    )
    raw = await bot.generate_gemini_text_for_task(
        prompt,
        model_env="GEMINI_ROAST_MODEL",
        model_default=bot.DEFAULT_GEMINI_ROAST_MODEL,
        max_attempts=bot.GEMINI_ROAST_MAX_ATTEMPTS,
        timeout=bot.GEMINI_ROAST_TIMEOUT,
    )
    best, choice = bot.parse_roast_choice_json(raw)
    print(best)
    print("\n(другие варианты, которые бот отбросил:)")
    for index, candidate in enumerate(choice["candidates"]):
        if index != choice["best_index"]:
            print(f"  • {candidate}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="день сводки ГГГГ-ММ-ДД (по умолчанию вчера)")
    parser.add_argument("--roast", action="append", default=[], help="username для подкола, можно несколько раз")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    chat_id = bot.get_allowed_chat_id()
    if chat_id is None:
        raise SystemExit("CHAT_ID не задан")
    tz = bot.get_app_timezone()
    day = date.fromisoformat(args.date) if args.date else datetime.now(tz).date() - timedelta(days=1)

    section("ОБЪЯВЛЕНИЕ (уйдёт один раз после деплоя)")
    print(as_text(bot.REPENTANCE_ANNOUNCEMENT_TEXT))

    for _ in range(2):
        section("/мудрость")
        print(as_text(scripture.format_entry_html(scripture.pick_entry(scripture.WISDOM), header="🦉 <b>Мудрость</b>")))
    for _ in range(2):
        section("/стих")
        print(as_text(scripture.format_entry_html(scripture.pick_entry(scripture.VERSE), header="📖 <b>Стих</b>")))
    section("УТРЕННЕЕ СЛОВО (воскресенье)")
    print(as_text(scripture.format_entry_html(
        scripture.pick_entry(scripture.VERSE), header="☀️ <b>Доброе утро! Слово на неделю</b>"
    )))

    await preview_digest(chat_id, day)
    for username in args.roast:
        await preview_roast(chat_id, username)


if __name__ == "__main__":
    asyncio.run(main())
