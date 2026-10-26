"""Run Avito searches from Telegram: send a link or /scan, get the Excel table back.

    uv run python avito.py bot

The bot answers only the chat saved by ``avito.py telegram setup``; everyone
else is ignored. Jobs (add a link, collect, build a table) run one at a time
in a background thread, so the bot keeps answering /status and /stop while a
scan walks Avito at its calm pace. One status message is edited as pages come
in; the finished table arrives as an .xlsx document.

Watched searches ("👀 Следить") are checked between jobs, about once a minute:
the first page of each, new listings sent to the chat like ``avito.py watch``
does. Jobs come first; the watch list survives restarts.
"""

from __future__ import annotations

import json
import math
import queue
import random
import re
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import avito
import avito_client
import telegram_notify
from telegram_notify import TelegramError, TelegramNetworkError, esc

POLL_SECONDS = 50
NETWORK_PAUSE_SECONDS = 10
CONFLICT_PAUSE_SECONDS = 30
WATCH_INTERVAL_SECONDS = 60
WATCH_FAILURES_TO_REPORT = 3
WATCH_JOB = "проверка новых объявлений"
AVITO_LINK = re.compile(r"https?://(?:www\.|m\.)?avito\.ru/\S+")
# Client events worth showing under the progress line.
NOTEWORTHY = {"CAPTCHA", "CAPTCHA-SOLVED", "CAPTCHA-FAILED", "SESSION-RESET", "IP-BLOCK", "NETWORK-WAIT", "ERROR"}
COMMANDS = [
    ("list", "мои поиски"),
    ("scan", "собрать поиск: /scan имя или номер"),
    ("table", "прислать таблицу: /table имя или номер"),
    ("watch", "следить за новыми: /watch имя или номер"),
    ("unwatch", "перестать следить: /unwatch имя или номер"),
    ("watching", "за чем слежу"),
    ("delete", "удалить поиск: /delete имя или номер"),
    ("clear", "очистить весь список поисков"),
    ("status", "что сейчас делаю"),
    ("stop", "остановить сбор"),
    ("help", "как пользоваться"),
]
# The keyboard under the input field; a tap sends the button text.
MENU = [["📋 Мои поиски", "🔗 Новый поиск"], ["👀 Слежение", "ℹ️ Что делаю"], ["⏹ Остановить", "❓ Помощь"]]
BUTTONS = {
    "📋 Мои поиски": "list", "🔗 Новый поиск": "new", "👀 Слежение": "watching",
    "ℹ️ Что делаю": "status", "⏹ Остановить": "stop", "❓ Помощь": "help",
}
KEYBOARD = {"keyboard": [[{"text": text} for text in row] for row in MENU], "resize_keyboard": True,
            "is_persistent": True}
NEW_SEARCH = (
    "Откройте поиск на Авито в браузере или приложении, выставьте город, категорию и фильтры, "
    "скопируйте ссылку («Поделиться» → «Копировать») и пришлите её сюда."
)
HELP = (
    "Пришлите ссылку на поиск Авито (откройте поиск в браузере, выставьте город и фильтры, скопируйте адрес) — "
    "соберу все объявления и пришлю таблицу Excel.\n\n"
    "Кнопки внизу: «Мои поиски» (у каждого — «собрать», «таблица», «следить»), «Новый поиск», "
    "«Слежение», «Что делаю», «Остановить».\n\n"
    "«👀 Следить» — раз в минуту смотрю свежие объявления поиска и присылаю сюда новые.\n\n"
    "/list — мои поиски, с кнопками\n"
    "/scan &lt;имя или номер&gt; — собрать поиск (досбор или заново)\n"
    "/table &lt;имя или номер&gt; — таблица из уже собранного, без сбора\n"
    "/watch, /unwatch &lt;имя или номер&gt; — следить за новыми или перестать\n"
    "/delete &lt;имя или номер&gt; — удалить поиск, /clear — очистить весь список\n"
    "/status — что сейчас делаю\n"
    "/stop — остановить сбор (пришлю таблицу из того, что успел)\n\n"
    "Сбор идёт спокойно, ~30 с на страницу из 50 объявлений, чтобы Авито не блокировал."
)


WATCH_HELP = (
    "Раз в ~{minutes} мин смотрю первую страницу каждого поиска (сортировка «по дате») и присылаю сюда "
    "объявления, которых раньше не было: для товаров — от частных лиц своего города, для квартир — "
    "от собственников; выставленные за последние часы, без переопубликованных старых."
)


@dataclass
class Job:
    title: str
    run: Callable[[], None]


def search_names() -> list[str]:
    return sorted(path.stem for path in avito.SEARCHES_DIR.glob("*.json"))


def find_search(argument: str) -> str | None:
    """A search by its name or by its number in /list."""
    names = search_names()
    argument = argument.strip()
    if argument.isdigit() and 1 <= int(argument) <= len(names):
        return names[int(argument) - 1]
    return argument if argument in names else None


def search_for_link(url: str) -> str | None:
    """An already saved search made from this very link."""
    for path in sorted(avito.SEARCHES_DIR.glob("*.json")):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if url in (config.get("source_link"), config.get("catalog_url")):
            return path.stem
    return None


def watching_path() -> Path:
    return avito.DATA_DIR.parent / "bot_watching.json"


def load_watching() -> set[str]:
    known = set(search_names())
    return {name for name in avito.load_json(watching_path()).get("searches", []) if name in known}


def title_of(name: str) -> str:
    try:
        return avito.load_search(name).title
    except SystemExit:  # the search file is gone
        return name


class Progress:
    """The status message of one job and the ``log`` callback that edits it."""

    def __init__(self, bot: "Bot", head: str) -> None:
        self.bot = bot
        self.head = head
        self.line = ""
        self.event = ""
        self.collected = 0
        self.table: dict[str, Any] = {}
        self.error = ""
        self.message_id = bot.say(head)

    def show(self) -> None:
        if self.message_id is None:
            return
        text = "\n".join(part for part in (self.head, self.line, self.event) if part)
        try:
            self.bot.notifier.edit(self.message_id, text)
        except TelegramError as exc:
            avito.say("TELEGRAM-ERROR", error=str(exc))

    def set_head(self, head: str) -> None:
        self.head, self.line, self.event = head, "", ""
        self.show()

    def log(self, kind: str, **fields: Any) -> None:
        avito.say(kind, **fields)
        if self.bot.stop.is_set():
            self.bot.stop.clear()
            raise avito.ScanStopped
        if kind == "PAGE":
            self.collected += int(fields.get("items") or 0)
            pages = min(max(1, math.ceil(int(fields.get("total") or 0) / avito_client.FULL_PAGE_SIZE)), avito.MAX_PAGES)
            left = max(0, pages - int(fields["page"]))
            eta = f" · осталось ~{math.ceil(left * avito_client.DEFAULT_DELAY_SECONDS / 60)} мин" if left else ""
            self.line = f"стр. {fields['page']} из ~{pages} · собрано {self.collected}{eta}"
            self.event = ""
        elif kind == "TABLE":
            self.table = {k: v for k, v in fields.items() if k not in ("time", "search", "path")}
            return
        elif kind in NOTEWORTHY:
            if kind == "ERROR":
                self.error = str(fields.get("error", ""))
            self.event = esc(avito.HUMAN[kind](fields).strip())
        else:
            return
        self.show()


class Bot:
    def __init__(
        self, notifier: telegram_notify.Notifier, *, delay: float, watch_interval: float = WATCH_INTERVAL_SECONDS
    ) -> None:
        self.notifier = notifier
        self.delay = delay
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.current: Job | None = None
        self.stop = threading.Event()
        self.watch_interval = watch_interval
        self.watching = load_watching()  # names; changed by the Telegram side, read by the worker
        self.watch_lock = threading.Lock()
        self.watch_due = 0.0  # time.monotonic() of the next watch cycle
        self.announce: set[str] = set()  # newly watched: tell when the current listings are remembered
        # Worker side only:
        self.watches: dict[str, avito.Watch] = {}
        self.failures: dict[str, int] = {}
        self.watch_client: avito_client.CalmClient | None = None
        self.cycles = 0

    # --- Telegram side -------------------------------------------------

    def say(self, text: str, **extra: Any) -> int | None:
        try:
            return self.notifier.send(text, preview=False, **extra)
        except TelegramError as exc:
            avito.say("TELEGRAM-ERROR", error=str(exc))
            return None

    def run(self) -> None:
        token = self.notifier.token
        try:
            telegram_notify.call(token, "setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
        except TelegramError:
            pass  # only the "/" menu in the app; commands work without it
        # Messages sent while the bot was off are dropped: a stale /scan from
        # yesterday should not start on its own.
        offset = None
        backlog = telegram_notify.call(token, "getUpdates", {"offset": -1, "timeout": 0}) or []
        if backlog:
            offset = backlog[-1]["update_id"] + 1
        threading.Thread(target=self.work, daemon=True).start()
        watching = f"\n\n👀 Слежу за: {esc(', '.join(title_of(n) for n in sorted(self.watching)))}" if self.watching else ""
        self.say("🤖 Бот на связи.\n\n" + HELP + watching, reply_markup=KEYBOARD)
        avito.say("BOT", text="бот запущен, жду команд в Telegram. Остановить: Ctrl+C")
        while True:
            try:
                updates = telegram_notify.call(
                    token, "getUpdates",
                    {"offset": offset, "timeout": POLL_SECONDS, "allowed_updates": ["message", "callback_query"]},
                    timeout=POLL_SECONDS + 15,
                ) or []
            except TelegramNetworkError as exc:
                avito.say("TELEGRAM-ERROR", error=str(exc))
                time.sleep(NETWORK_PAUSE_SECONDS)
                continue
            except TelegramError as exc:
                # 409: the same bot is polled elsewhere (a second `avito.py bot`).
                avito.say("TELEGRAM-ERROR", error=str(exc))
                time.sleep(CONFLICT_PAUSE_SECONDS)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except TelegramError as exc:
                    avito.say("TELEGRAM-ERROR", error=str(exc))

    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            chat = ((query.get("message") or {}).get("chat") or {}).get("id")
            telegram_notify.call(self.notifier.token, "answerCallbackQuery", {"callback_query_id": query["id"]})
            if chat != self.notifier.chat_id:
                return
            action, _, name = str(query.get("data", "")).partition(":")
            self.command(action, name, origin=query.get("message"))
            return
        message = update.get("message") or {}
        if (message.get("chat") or {}).get("id") != self.notifier.chat_id:
            if message:
                avito.say("BOT", text=f"сообщение из чужого чата {message.get('chat', {}).get('id')} пропущено")
            return
        text = str(message.get("text") or "").strip()
        link = AVITO_LINK.search(text)
        if text in BUTTONS:
            self.command(BUTTONS[text], "")
        elif link:
            self.enqueue(Job("ссылка", lambda: self.add_and_scan(link.group(0).rstrip(".,;)"))))
        elif text.startswith("/"):
            command, _, argument = text[1:].partition(" ")
            self.command(command.split("@")[0].lower(), argument)
        else:
            self.say("Не понял. Пришлите ссылку на поиск Авито или /help.")

    def command(self, command: str, argument: str, origin: dict[str, Any] | None = None) -> None:
        if command in ("start", "help"):
            self.say(HELP, reply_markup=KEYBOARD)
        elif command == "new":
            self.say(NEW_SEARCH)
        elif command == "list":
            self.send_list()
        elif command == "status":
            self.send_status()
        elif command == "watching":
            text, markup = self.watching_view()
            self.say(text, reply_markup=markup)
        elif command == "clear":
            self.confirm_delete(search_names(), origin)
        elif command == "clearyes":
            self.delete(search_names(), origin)
        elif command == "delyes":
            self.delete([argument] if argument in search_names() else [], origin)
        elif command == "cancel":
            self.close(origin, "Отменено, ничего не удалял.")
        elif command == "stop":
            self.stop_jobs()
        elif command in ("scan", "table", "watch", "unwatch", "delete"):
            names = search_names()
            if argument.strip():
                name = find_search(argument)
            else:
                name = names[0] if len(names) == 1 else None
            if name is None:
                if argument.strip():
                    self.say(f"Нет поиска «{esc(argument.strip())}».")
                self.send_list()
                return
            if command == "scan":
                self.enqueue(Job(f"сбор «{name}»", lambda: self.scan(name)))
            elif command == "table":
                self.enqueue(Job(f"таблица «{name}»", lambda: self.table(name)))
            elif command == "delete":
                self.confirm_delete([name], origin)
            else:
                self.set_watch(name, command == "watch")
                if origin:
                    self.refresh(origin)
        else:
            self.say("Не знаю такой команды. /help — что я умею.")

    def send_list(self) -> None:
        if not search_names():
            self.say("Поисков пока нет. Пришлите ссылку на поиск Авито.")
            return
        text, markup = self.list_view()
        self.say(text, reply_markup=markup)

    def list_view(self) -> tuple[str, dict[str, Any]]:
        lines, keyboard = ["<b>Мои поиски</b>"], []
        for number, name in enumerate(search_names(), start=1):
            search = avito.load_search(name)
            stored = len(search.load("listings.json"))
            watched = name in self.watching
            lines.append(f"{number}. {esc(search.title)} — {stored} объявл.{' 👀' if watched else ''} (<code>{esc(name)}</code>)")
            if len(f"unwatch:{name}".encode()) <= 64:  # Telegram's callback_data limit
                keyboard.append([
                    {"text": f"📥 {number}. Собрать", "callback_data": f"scan:{name}"},
                    {"text": "📊 Таблица", "callback_data": f"table:{name}"},
                    {"text": "🔕 Не следить", "callback_data": f"unwatch:{name}"} if watched
                    else {"text": "👀 Следить", "callback_data": f"watch:{name}"},
                    {"text": "🗑", "callback_data": f"delete:{name}"},
                ])
        keyboard.append([{"text": "🗑 Очистить весь список", "callback_data": "clear:"}])
        return "\n".join(lines), {"inline_keyboard": keyboard}

    def confirm_delete(self, names: list[str], origin: dict[str, Any] | None) -> None:
        if not names:
            self.say("Поисков нет, удалять нечего.")
            return
        if len(names) == 1:
            question = f"Удалить поиск «{esc(title_of(names[0]))}» вместе с собранными объявлениями?"
            yes = f"delyes:{names[0]}"
        else:
            question = f"Удалить все поиски ({len(names)}) вместе с собранными объявлениями?"
            yes = "clearyes:"
        self.say(question + "\nТаблицы, которые я уже присылал, останутся у вас в чате.",
                 reply_markup={"inline_keyboard": [[{"text": "🗑 Да, удалить", "callback_data": yes},
                                                    {"text": "Отмена", "callback_data": "cancel:"}]]})

    def close(self, origin: dict[str, Any] | None, text: str) -> None:
        """Replace a question with its outcome, so its buttons cannot be pressed twice."""
        if origin and "message_id" in origin:
            try:
                self.notifier.edit(origin["message_id"], text, reply_markup={"inline_keyboard": []})
                return
            except TelegramError as exc:
                avito.say("TELEGRAM-ERROR", error=str(exc))
        self.say(text)

    def delete(self, names: list[str], origin: dict[str, Any] | None) -> None:
        if not names:
            self.close(origin, "Этого поиска уже нет.")
            return
        what = f"«{esc(title_of(names[0]))}»" if len(names) == 1 else f"все поиски ({len(names)})"
        self.close(origin, f"🗑 Удаляю {what}…")
        # Through the queue: never in the middle of a scan or a watch check of the same search.
        self.enqueue(Job(f"удаление: {what}", lambda: self.remove(names, what)))

    def remove(self, names: list[str], what: str) -> None:
        for name in names:
            with self.watch_lock:
                self.watching.discard(name)
            self.announce.discard(name)
            self.watches.pop(name, None)
            avito.delete_search(name)
        self.save_watching()
        self.say(f"🗑 Удалено: {what}." + ("" if search_names() else " Список поисков пуст — пришлите ссылку на новый."))

    def watching_view(self) -> tuple[str, dict[str, Any]]:
        names = sorted(self.watching)
        if not names:
            return ("<b>Слежение</b>\nНи за чем не слежу. Откройте «📋 Мои поиски» и нажмите «👀 Следить» "
                    "у нужного поиска."), {"inline_keyboard": []}
        lines = ["<b>Слежение</b>", WATCH_HELP.format(minutes=max(1, round(self.watch_interval / 60))), ""]
        keyboard = []
        for number, name in enumerate(names, start=1):
            title = title_of(name)
            lines.append(f"{number}. {esc(title)}")
            keyboard.append([{"text": f"🔕 Не следить: {title[:40]}", "callback_data": f"unwatch:{name}"}])
        return "\n".join(lines), {"inline_keyboard": keyboard}

    def refresh(self, origin: dict[str, Any]) -> None:
        """Redraw the list a button was pressed in, so its buttons show the new state."""
        view = self.watching_view if str(origin.get("text", "")).startswith("Слежение") else self.list_view
        text, markup = view()
        try:
            self.notifier.edit(origin["message_id"], text, reply_markup=markup)
        except (TelegramError, KeyError) as exc:
            avito.say("TELEGRAM-ERROR", error=str(exc))

    def set_watch(self, name: str, on: bool) -> None:
        title = esc(title_of(name))
        if on and name not in self.watching:
            self.announce.add(name)
            with self.watch_lock:
                self.watching.add(name)
            self.watch_due = 0.0
            self.say(f"👀 Слежу за «{title}». Сначала запомню, что уже выставлено, дальше буду присылать сюда "
                     f"новые объявления.\n\n" + WATCH_HELP.format(minutes=max(1, round(self.watch_interval / 60))))
        elif on:
            self.say(f"Уже слежу за «{title}».")
        elif name in self.watching:
            with self.watch_lock:
                self.watching.discard(name)
            self.announce.discard(name)
            self.say(f"🔕 Больше не слежу за «{title}».")
        else:
            self.say(f"За «{title}» я и не следил.")
        self.save_watching()

    def save_watching(self) -> None:
        with self.watch_lock:
            avito.save_json(watching_path(), {"searches": sorted(self.watching)})

    def send_status(self) -> None:
        lines = []
        if self.current is None:
            lines.append("Сейчас ничего не делаю.")
        else:
            lines.append(f"Сейчас: {esc(self.current.title)}")
            if self.jobs.qsize():
                lines.append(f"В очереди: {self.jobs.qsize()}")
        if self.watching:
            wait = round(self.watch_due - time.monotonic())
            lines.append(f"👀 Слежу за: {esc(', '.join(title_of(n) for n in sorted(self.watching)))}"
                         + (f"; следующая проверка через ~{wait} с" if wait > 0 else ""))
        elif self.current is None:
            lines.append("Пришлите ссылку или откройте «📋 Мои поиски».")
        self.say("\n".join(lines))

    def stop_jobs(self) -> None:
        dropped = 0
        while True:
            try:
                self.jobs.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        if self.current is None:
            self.say("Нечего останавливать." if not dropped else f"Очередь очищена ({dropped}).")
            return
        if self.current.title == WATCH_JOB:
            self.say("Сейчас идёт проверка новых объявлений, она скоро закончится. "
                     "Выключить слежение — кнопка «👀 Слежение»." + (f" Очередь очищена ({dropped})." if dropped else ""))
            return
        self.stop.set()
        self.say("Останавливаю после текущего запроса к Авито…"
                 + (f" Очередь очищена ({dropped})." if dropped else ""))

    def enqueue(self, job: Job) -> None:
        ahead = self.jobs.qsize() + (self.current is not None)
        self.jobs.put(job)
        if ahead:
            self.say(f"Поставил в очередь: {esc(job.title)} (перед ним {ahead}).")

    # --- worker side ---------------------------------------------------

    def work(self) -> None:
        while True:
            try:
                job = self.jobs.get(timeout=1)
            except queue.Empty:
                if self.watching and time.monotonic() >= self.watch_due:
                    try:
                        self.watch_cycle()
                    except Exception:  # a bug in one cycle must not stop the worker
                        traceback.print_exc()
                        self.watch_due = time.monotonic() + self.watch_interval
                continue
            self.current = job
            self.stop.clear()
            try:
                job.run()
            except avito.ScanStopped:
                self.say("⏹ Остановлено.")
            except avito.SearchError as exc:
                self.say(f"✗ {esc(exc)}")
            except SystemExit:
                self.say("✗ Не получилось, подробности в окне программы.")
            except Exception as exc:  # the bot must outlive any single job
                traceback.print_exc()
                self.say(f"✗ Ошибка: {esc(type(exc).__name__)}: {esc(str(exc)[:300])}")
            finally:
                self.current = None
                # A scan may have stored listings a watch has not seen yet.
                for watch in self.watches.values():
                    watch.reload()

    def watch_cycle(self) -> None:
        """Check every watched search once; a job sent meanwhile goes first."""
        self.current = Job(WATCH_JOB, lambda: None)
        try:
            if self.watch_client is None:
                self.watch_client = avito_client.CalmClient(delay=self.delay, log=avito.say)
            self.cycles += 1
            with self.watch_lock:
                names = sorted(self.watching)
            for name in set(self.watches) - set(names):
                del self.watches[name]
            for name in names:
                if not self.jobs.empty():
                    return  # due stays in the past: the cycle starts over after the job
                self.check_one(name)
            self.watch_due = time.monotonic() + self.watch_interval * random.uniform(0.8, 1.2)
        finally:
            self.current = None

    def check_one(self, name: str) -> None:
        watch = self.watches.get(name)
        if watch is None:
            try:
                watch = self.watches[name] = avito.Watch(avito.load_search(name))
            except SystemExit:  # the search was deleted
                with self.watch_lock:
                    self.watching.discard(name)
                self.save_watching()
                return
        elif self.cycles % avito.MARKET_REFRESH_EVERY_CYCLES == 0 and hasattr(watch.profile, "refresh_market"):
            watch.profile.refresh_market()
        search = watch.search
        try:
            watch.check(self.watch_client, on_alert=lambda fields: self.alert(search, fields))
            self.failures[name] = 0
        except (RuntimeError, ValueError, *avito_client.TRANSPORT_ERRORS) as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
            avito.say("ERROR", search=name, error=error)
            self.failures[name] = self.failures.get(name, 0) + 1
            if self.failures[name] == WATCH_FAILURES_TO_REPORT:
                self.say(f"⚠ Не получается проверить «{esc(search.title)}» ({esc(error)}). Продолжаю пробовать.")
        if name in self.announce and watch.primed:
            self.announce.discard(name)
            self.say(f"✓ «{esc(search.title)}»: текущая выдача запомнена. Новые объявления пришлю сюда.")

    def alert(self, search: avito.Search, fields: dict[str, Any]) -> None:
        try:
            self.notifier.send(telegram_notify.message_for(search.profile, search.name, fields))
        except TelegramError as exc:
            avito.say("TELEGRAM-ERROR", error=str(exc))

    def add_and_scan(self, url: str) -> None:
        existing = search_for_link(url)
        if existing:
            self.say(f"Этот поиск уже есть (<code>{esc(existing)}</code>), собираю его.")
            self.scan(existing)
            return
        progress = Progress(self, "🔎 Проверяю ссылку на Авито, около минуты…")
        name, resolved = avito.add_search(url, delay=5.0, log=progress.log)
        lines = [f"✓ Поиск сохранён: <b>{esc(resolved.title)}</b> · {esc(resolved.city)}",
                 f"Объявлений сейчас: {resolved.total} · имя: <code>{esc(name)}</code>"]
        if resolved.filters:
            lines.append("Фильтры: " + esc("; ".join(resolved.filters)))
        lines += [f"⚠ {esc(warning)}" for warning in resolved.warnings]
        progress.set_head("\n".join(lines))
        if resolved.total == 0:
            self.say("⚠ По этой ссылке сейчас 0 объявлений, собирать нечего. Проверьте ссылку в браузере.")
            return
        self.scan(name)

    def scan(self, name: str) -> None:
        search = avito.load_search(name)
        progress = Progress(self, f"⏳ Собираю <b>{esc(search.title)}</b>…")
        path = avito.scan_search(search, delay=self.delay, log=progress.log)
        self.send_table(search, path, progress)

    def table(self, name: str) -> None:
        search = avito.load_search(name)
        progress = Progress(self, f"📊 Собираю таблицу <b>{esc(search.title)}</b>…")
        path = avito.write_table(search, search.load("seen.json"), log=progress.log)
        self.send_table(search, path, progress)

    def send_table(self, search: avito.Search, path: Path | None, progress: Progress) -> None:
        if path is None:
            progress.set_head(f"В базе «{esc(search.title)}» пока пусто: сначала /scan {esc(search.name)}")
            return
        caption = [f"📊 <b>{esc(search.title)}</b>"]
        caption += [f"{esc(key)}: {esc(value)}" for key, value in progress.table.items()]
        if progress.error:
            caption.append(f"⚠ сбор прерван ({esc(progress.error[:200])}); /scan {esc(search.name)} продолжит")
        progress.set_head(f"✓ Готово: <b>{esc(search.title)}</b>, отправляю таблицу…")
        self.notifier.send_document(path, caption="\n".join(caption)[:1024])


def run(*, delay: float = avito_client.DEFAULT_DELAY_SECONDS, watch_interval: float = WATCH_INTERVAL_SECONDS) -> None:
    notifier = telegram_notify.Notifier.from_settings()
    if notifier is None:
        raise TelegramError("Telegram не подключён: uv run python avito.py telegram setup")
    Bot(notifier, delay=delay, watch_interval=watch_interval).run()
