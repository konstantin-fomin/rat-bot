"""Проверенные цитаты из Библии (Новый русский перевод) для команд /wisdom и /verse.

Тексты хранятся в scripture_nrt.json дословно и никогда не генерируются нейросетью:
Gemini может только выбрать id подходящей цитаты из этого списка.
"""

from __future__ import annotations

import html
import json
import random
from collections import deque
from functools import lru_cache
from pathlib import Path

SCRIPTURE_PATH = Path(__file__).with_name("scripture_nrt.json")
TRANSLATION_ABBR = "НРП"
RECENT_LIMIT = 25

WISDOM = "wisdom"  # Притчи и Екклесиаст
VERSE = "verse"  # остальные книги

_recent: dict[str, deque[str]] = {}


@lru_cache(maxsize=1)
def load_scripture() -> dict[str, list[dict]]:
    data = json.loads(SCRIPTURE_PATH.read_text(encoding="utf-8"))
    return {WISDOM: data[WISDOM], VERSE: data[VERSE]}


def get_entries(pool: str) -> list[dict]:
    return load_scripture()[pool]


def find_entry(entry_id: str) -> dict | None:
    for pool in (WISDOM, VERSE):
        for entry in get_entries(pool):
            if entry["id"] == entry_id:
                return entry
    return None


def pick_entry(pool: str, *, rng: random.Random | None = None) -> dict:
    """Случайная цитата из пула, без повторов среди последних RECENT_LIMIT выдач."""
    rng = rng or random
    entries = get_entries(pool)
    recent = _recent.setdefault(pool, deque(maxlen=min(RECENT_LIMIT, len(entries) - 1)))
    candidates = [entry for entry in entries if entry["id"] not in recent] or entries
    entry = rng.choice(candidates)
    recent.append(entry["id"])
    return entry


def sample_entries(pool: str, k: int, *, rng: random.Random | None = None) -> list[dict]:
    rng = rng or random
    entries = get_entries(pool)
    return rng.sample(entries, k=min(k, len(entries)))


def format_entry_html(entry: dict, header: str | None = None) -> str:
    lines = []
    if header:
        lines.extend([header, ""])
    lines.append(f"<i>{html.escape(entry['text'])}</i>")
    lines.append("")
    lines.append(f"— {html.escape(entry['ref'])} ({TRANSLATION_ABBR})")
    return "\n".join(lines)
