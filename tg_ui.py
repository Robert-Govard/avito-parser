"""Bot screens: rich HTML, coloured buttons with premium icons (Bot API 10.3).

The same building blocks as the owner's other bot: a Screen is rich HTML plus
rows of Buttons; Telegram shows it as a rich message, and if it refuses one,
the text goes out plain (``to_plain``). Icons are premium emoji from the
tgmacicons pack (https://t.me/addemoji/tgmacicons): the bot may show them
because its owner has Telegram Premium; each has an ordinary emoji for plain text.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

PRIMARY, SUCCESS, DANGER = "primary", "success", "danger"

_LINE_BREAK = re.compile(r"<(br|hr)\s*/?>", re.IGNORECASE)
_CELL_GAP = re.compile(r"</t[dh]>\s*(?=<t[dh][\s>])", re.IGNORECASE)
_BLOCK_END = re.compile(r"</(p|h[1-6]|tr|li|summary|footer|blockquote|aside|caption|pre)>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Icon:
    id: str
    alt: str


SEARCHES = Icon("5257969839313526622", "📂")
STATS = Icon("5258330865674494479", "📊")
WATCH = Icon("5258165702707125574", "🎯")
RUNNING = Icon("5260416304224936047", "▶️")
IDLE = Icon("5260249440450520061", "⏸")
PRICE = Icon("5258204546391351475", "💰")
SELLER = Icon("5258260149037965799", "💼")
DATE = Icon("5258105663359294787", "🗓")
STOP = Icon("5260342697075416641", "🔴")
SHIELD = Icon("5275969776668134187", "🛡")
SETTINGS = Icon("5258096772776991776", "⚙️")
LINK = Icon("5258476306152038031", "🔑")
ROUTE = Icon("5260687681733533075", "🔁")
REFRESH = Icon("5260687119092817530", "🔄")
HOME = Icon("5257963315258204021", "⬅️")


def ce(icon: Icon) -> str:
    """A premium emoji for rich HTML; plain text keeps ``icon.alt``."""
    return f'<tg-emoji emoji-id="{icon.id}">{icon.alt}</tg-emoji>'


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def paragraph(text: str) -> str:
    """Plain text → a rich-HTML paragraph: escaped, lines joined with <br>."""
    return "<p>" + "<br>".join(esc(line) for line in text.split("\n")) + "</p>"


def plural(n: int, one: str, few: str, many: str) -> str:
    """The Russian form for a number: 1 поиск, 2 поиска, 5 поисков (11–14 — always «many»)."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def to_plain(markup: str) -> str:
    """Rich HTML → plain text, for the fallback message and for tests."""
    text = _LINE_BREAK.sub("\n", markup)
    text = _CELL_GAP.sub(" · ", text)
    text = _BLOCK_END.sub("\n", text)
    text = html.unescape(_TAG.sub("", text))
    lines: list[str] = []
    for line in (raw.strip() for raw in text.split("\n")):
        if line == "" and (not lines or lines[-1] == ""):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Plain text in pieces Telegram accepts (4096 characters each)."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
        else:
            parts.append(current)
            current = line
    if current:
        parts.append(current)
    return parts or [""]


@dataclass(frozen=True)
class Button:
    text: str
    data: str = ""
    style: str | None = None
    disabled: bool = False
    icon: Icon | None = None
    url: str | None = None

    def to_api(self) -> dict[str, Any]:
        button: dict[str, Any] = {"text": self.text}
        if self.style:
            button["style"] = self.style
        if self.icon:
            button["icon_custom_emoji_id"] = self.icon.id
        if self.disabled:
            button["disabled"] = {}
        elif self.url:
            button["url"] = self.url
        else:
            button["callback_data"] = self.data
        return button


Rows = tuple[tuple[Button, ...], ...]


@dataclass(frozen=True)
class Screen:
    html: str
    rows: Rows = ()

    @classmethod
    def plain(cls, text: str, rows: Iterable[Iterable[Button]] = ()) -> "Screen":
        return cls(paragraph(text), tuple(tuple(row) for row in rows))

    @property
    def text(self) -> str:
        return to_plain(self.html)

    @property
    def buttons(self) -> list[list[str]]:
        """Button texts by row, for tests."""
        return [[button.text for button in row] for row in self.rows]

    def markup(self) -> dict[str, Any] | None:
        if not self.rows:
            return None
        return {"inline_keyboard": [[button.to_api() for button in row] for row in self.rows]}

    def with_notice(self, notice_html: str) -> "Screen":
        """The same screen with a line on top, e.g. what the last button did."""
        return replace(self, html=notice_html + self.html)
