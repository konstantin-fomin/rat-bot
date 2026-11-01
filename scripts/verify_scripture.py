#!/usr/bin/env python3
"""Сверяет цитаты из src/scripture_nrt.json с текстом Нового русского перевода на BibleGateway.

Нужен только стандартный Python 3 и доступ к www.biblegateway.com по IPv4
(например, из Google Cloud Shell):

    python3 scripts/verify_scripture.py

Код возврата 0 — все цитаты совпадают дословно (с точностью до пробелов и переносов строк).
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "src" / "scripture_nrt.json"
BG_URL = "https://www.biblegateway.com/passage/?search={search}&version=NRT&interface=print"
BATCH_SIZE = 20  # BibleGateway показывает не больше 25 отрывков за запрос
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

VERSE_CLASS_RE = re.compile(r"\btext\s+([1-3]?[A-Za-z]+)-(\d+)-(\d+)\b")
OSIS_RE = re.compile(r"^([1-3]?[A-Za-z]+) (\d+):(\d+)(?:-(\d+))?$")
# Заголовки разделов и надписания псалмов помечены тем же классом стиха, но в текст стиха не входят.
SKIP_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "sup"}
SKIP_CLASSES = {"chapternum", "versenum", "footnote", "crossreference", "footnotes", "crossrefs"}


class PassageParser(HTMLParser):
    """Собирает текст каждого стиха: {("Prov", 3, 5): "Доверяй Господу ..."}."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.verses: dict[tuple[str, int, int], list[str]] = {}
        self._stack: list[tuple[str, tuple[str, int, int] | None, bool]] = []
        self._last_verse: tuple[str, int, int] | None = None

    def _line_break(self) -> None:
        # Строки стихов разделены <br>/<p>/<div>: между ними нужен пробел.
        if self._last_verse is not None:
            self.verses[self._last_verse].append(" ")

    def _current_verse(self) -> tuple[str, int, int] | None:
        for _, verse, _ in reversed(self._stack):
            if verse is not None:
                return verse
        return None

    def _skipping(self) -> bool:
        return any(skip for _, _, skip in self._stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div"}:
            self._line_break()
        if tag in {"br", "img", "hr", "input", "meta", "link"}:
            return
        classes = (dict(attrs).get("class") or "").split()
        skip = tag in SKIP_TAGS or bool(SKIP_CLASSES.intersection(classes))
        verse = None
        match = VERSE_CLASS_RE.search(" ".join(classes))
        if match:
            verse = (match.group(1), int(match.group(2)), int(match.group(3)))
        self._stack.append((tag, verse, skip))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div"}:
            self._line_break()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        verse = self._current_verse()
        if verse is None or self._skipping():
            return
        self.verses.setdefault(verse, []).append(data)
        self._last_verse = verse


def normalize(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def fetch(search: str) -> str:
    url = BG_URL.format(search=urllib.parse.quote(search, safe=":;-"))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def main() -> int:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    entries = data["wisdom"] + data["verse"]
    failures = 0

    for start in range(0, len(entries), BATCH_SIZE):
        batch = entries[start : start + BATCH_SIZE]
        parser = PassageParser()
        parser.feed(fetch(";".join(entry["osis"] for entry in batch)))
        found = {key: normalize("".join(parts)) for key, parts in parser.verses.items()}

        for entry in batch:
            match = OSIS_RE.match(entry["osis"])
            if not match:
                print(f"BAD OSIS  {entry['id']}: {entry['osis']}")
                failures += 1
                continue
            book, chapter, first = match.group(1), int(match.group(2)), int(match.group(3))
            last = int(match.group(4) or first)
            parts = [found.get((book, chapter, verse)) for verse in range(first, last + 1)]
            if any(part is None for part in parts):
                print(f"NOT FOUND {entry['ref']} ({entry['osis']})")
                failures += 1
                continue
            expected = normalize(" ".join(parts))
            actual = normalize(entry["text"])
            if expected == actual:
                print(f"OK        {entry['ref']}")
            else:
                failures += 1
                print(f"MISMATCH  {entry['ref']}\n  сайт: {expected}\n  бот:  {actual}")
        time.sleep(1)

    print(f"\nПроверено цитат: {len(entries)}, расхождений: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
