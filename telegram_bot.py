"""Run Avito searches from Telegram: send a link or /scan, get the Excel table back.

    uv run python avito.py bot

The bot answers only the chat saved by ``avito.py telegram setup``; everyone
else is ignored. It looks like the owner's other bot: one "live" panel (menu,
searches, a search's card, watching, help) redrawn in place as buttons are
pressed, rich messages with coloured buttons (tg_ui, tg_screens). A job's
progress is a message of its own, edited as pages come in, with a stop
button; the finished table arrives as an .xlsx document.

Jobs (add a link, collect, build a table) run one at a time in a thread, so
the bot keeps answering while a scan walks Avito at its calm pace. Watched
searches are checked about once a minute in a thread of their own: the first
page of each, new listings sent to the chat. The watch list survives restarts.

Avito limits requests per IP address. Watch checks leave from this server;
jobs leave through ``--scan-proxy`` (an SSH tunnel to another server,
SSH-TUNNEL.md), so both run at full pace without waiting for each other.
If the proxy is down or blocked, jobs fall back to this server's address
and share its pace with the checks (avito_client.Pacer) until it is retried.
Both threads add to a search's stored listings (Search.save_collected).
"""

from __future__ import annotations

import json
import math
import queue
import random
import re
import socket
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import avito
import avito_client
import telegram_notify
import tg_screens as screens
from telegram_notify import TelegramError, TelegramNetworkError
from tg_ui import RUNNING, STATS, STOP, Screen, ce, esc, plural

POLL_SECONDS = 50
NETWORK_PAUSE_SECONDS = 10
CONFLICT_PAUSE_SECONDS = 30
WATCH_INTERVAL_SECONDS = 60
WATCH_FAILURES_TO_REPORT = 3
PROXY_RETRY_SECONDS = 30 * 60
AVITO_LINK = re.compile(r"https?://(?:www\.|m\.)?avito\.ru/\S+")
# Client events worth showing under a job's progress.
NOTEWORTHY = {"CAPTCHA", "CAPTCHA-SOLVED", "CAPTCHA-FAILED", "SESSION-RESET", "IP-BLOCK", "NETWORK-WAIT", "ERROR"}
COMMANDS = [
    ("start", "меню"),
    ("list", "мои поиски"),
    ("scan", "собрать поиск: /scan имя или номер"),
    ("table", "прислать таблицу: /table имя или номер"),
    ("watch", "следить за новыми: /watch имя или номер"),
    ("unwatch", "перестать следить: /unwatch имя или номер"),
    ("watching", "за чем слежу"),
    ("delete", "удалить поиск: /delete имя или номер"),
    ("clear", "очистить весь список поисков"),
    ("stop", "остановить сбор"),
    ("help", "как пользоваться"),
]
# Texts of the reply keyboard earlier versions showed under the input field:
# a tap on a button still left in a chat sends one of these.
OLD_BUTTONS = {
    "📋 Мои поиски": "list", "🔗 Новый поиск": "new", "👀 Слежение": "watching",
    "ℹ️ Что делаю": "menu", "⏹ Остановить": "stop", "❓ Помощь": "help",
}
ARGUMENT_COMMANDS = ("scan", "table", "watch", "unwatch", "delete")


@dataclass
class Job:
    title: str
    run: Callable[[], None]


def search_names() -> list[str]:
    return sorted(path.stem for path in avito.SEARCHES_DIR.glob("*.json"))


def find_search(argument: str) -> str | None:
    """A search by its name or by its number in the list."""
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


def new_today(name: str) -> int:
    """Alerts sent today for a search (its alerts.jsonl)."""
    path = avito.DATA_DIR / name / "alerts.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    today = date.today().isoformat()
    return sum(line.startswith(f'{{"found": "{today}') for line in lines)


class ProxyFailed(avito.ScanStopped):
    """The job's proxy went down or got blocked: stop, then go on without it."""


def proxy_reachable(proxy: str) -> bool:
    parts = urlsplit(proxy)
    try:
        socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 1080), timeout=3).close()
        return True
    except OSError:
        return False


class Progress:
    """The progress message of one job and the ``log`` callback that redraws it."""

    def __init__(self, bot: "Bot", head: str) -> None:
        self.bot = bot
        self.view = screens.ProgressView(head)
        self.via_proxy = False  # set by Bot.with_job_client
        self.proxy_failed = False
        self.collected = 0
        self.table: dict[str, Any] = {}
        self.error = ""
        self.message_id = bot.send(screens.progress(self.view))

    def show(self) -> None:
        if self.message_id is None:
            return
        try:
            self.bot.notifier.edit_screen(self.message_id, screens.progress(self.view))
        except TelegramError as exc:
            avito.say("TELEGRAM-ERROR", error=str(exc))

    def set_head(self, head: str, *, rows: list[tuple[str, str]] | None = None, stoppable: bool = True) -> None:
        self.view = screens.ProgressView(head, rows or [], "", stoppable)
        self.show()

    def log(self, kind: str, **fields: Any) -> None:
        avito.say(kind, **fields)
        if self.bot.stop.is_set():
            self.bot.stop.clear()
            raise avito.ScanStopped
        if self.via_proxy and kind in ("IP-BLOCK", "NETWORK-WAIT") and not self.proxy_failed:
            # Not worth waiting out: the job goes on from this server instead.
            self.proxy_failed = True
            self.bot.proxy_down("Авито не пускает через него" if kind == "IP-BLOCK" else "нет связи через туннель")
            raise ProxyFailed
        if kind == "PAGE":
            self.collected += int(fields.get("items") or 0)
            pages = min(max(1, math.ceil(int(fields.get("total") or 0) / avito_client.FULL_PAGE_SIZE)), avito.MAX_PAGES)
            left = max(0, pages - int(fields["page"]))
            self.view.rows = [
                ("Страница", f"<b>{fields['page']}</b> из ~{pages}"),
                ("Собрано", f"{self.collected} {plural(self.collected, 'объявление', 'объявления', 'объявлений')}"),
                ("Осталось", f"~{math.ceil(left * avito_client.DEFAULT_DELAY_SECONDS / 60)} мин" if left else "последняя"),
                ("Идёт", "через второй сервер" if self.via_proxy else "с этого сервера"),
            ]
            self.view.event = ""
        elif kind == "TABLE":
            self.table = {k: v for k, v in fields.items() if k not in ("time", "search", "path")}
            return
        elif kind in NOTEWORTHY:
            if kind == "ERROR":
                self.error = str(fields.get("error", ""))
            self.view.event = avito.HUMAN[kind](fields).strip().lstrip("·").strip()
        else:
            return
        self.show()


class Bot:
    def __init__(
        self,
        notifier: telegram_notify.Notifier,
        *,
        delay: float,
        watch_interval: float = WATCH_INTERVAL_SECONDS,
        scan_proxy: str | None = None,
    ) -> None:
        self.notifier = notifier
        self.delay = delay
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.current: Job | None = None
        self.stop = threading.Event()
        self.panel_id: int | None = None  # the "live" menu message
        self.watch_interval = watch_interval
        self.watching = load_watching()  # names; changed by the Telegram side, read by the watch thread
        self.watch_lock = threading.Lock()
        self.watch_due = 0.0  # time.monotonic() of the next watch cycle
        self.announce: set[str] = set()  # newly watched: tell when the current listings are remembered
        self.reload_watches = threading.Event()  # a job stored listings the watches have not seen
        # This server's address: watch checks always, jobs only when the proxy is out.
        self.pacer = avito_client.Pacer(delay)
        self.scan_proxy = scan_proxy
        self.proxy_off_until = 0.0
        # Job thread only:
        self._job_clients: dict[bool, avito_client.CalmClient] = {}
        # Watch thread only:
        self.watches: dict[str, avito.Watch] = {}
        self.failures: dict[str, int] = {}
        self.watch_client: avito_client.CalmClient | None = None
        self.cycles = 0

    # --- sending ---------------------------------------------------------

    def send(self, screen: Screen) -> int | None:
        try:
            return self.notifier.send_screen(screen)
        except TelegramError as exc:
            avito.say("TELEGRAM-ERROR", error=str(exc))
            return None

    def say(self, text: str, *rows: tuple) -> int | None:
        """A short message; ``text`` is HTML, line breaks allowed."""
        return self.send(Screen("<p>" + text.replace("\n", "<br>") + "</p>", tuple(rows)))

    def show(self, screen: Screen, origin: dict[str, Any] | None = None) -> None:
        """Show a panel screen: redraw the message a button was pressed in, or send a new panel."""
        if origin and "message_id" in origin:
            try:
                if self.notifier.edit_screen(origin["message_id"], screen):
                    self.panel_id = origin["message_id"]
                    return
            except TelegramError as exc:
                avito.say("TELEGRAM-ERROR", error=str(exc))
        previous = self.panel_id
        self.panel_id = self.send(screen)
        if previous is not None and previous != self.panel_id:
            self.notifier.delete(previous)  # one panel in the chat, the newest

    # --- views -------------------------------------------------------------

    @property
    def check_minutes(self) -> int:
        """How often each watched search is looked at: the interval, or longer with many searches."""
        cycle = max(self.watch_interval, len(self.watching) * self.delay)
        return max(1, round(cycle / 60))

    def info(self, name: str) -> screens.SearchInfo:
        search = avito.load_search(name)
        state = search.load("state.json")
        return screens.SearchInfo(
            name=name, title=search.title, profile=search.profile, url=search.catalog_url,
            stored=len(search.load("seen.json")), complete=bool(state.get("complete")),
            next_page=int(state.get("next_page", 1)), watched=name in self.watching,
            new_today=new_today(name) if name in self.watching else 0,
        )

    def infos(self) -> list[screens.SearchInfo]:
        return [self.info(name) for name in search_names()]

    def route(self) -> str | None:
        if not self.scan_proxy:
            return None
        return "proxy" if self.proxy_usable() else "direct"

    def menu_screen(self) -> Screen:
        items = self.infos()
        return screens.menu(screens.MenuView(
            searches=len(items), listings=sum(s.stored for s in items), watching=len(self.watching),
            check_minutes=self.check_minutes, new_today=sum(s.new_today for s in items),
            job=self.current.title if self.current else None, queued=self.jobs.qsize(), route=self.route(),
        ))

    def watching_screen(self) -> Screen:
        return screens.watching([s for s in self.infos() if s.watched], self.check_minutes)

    # --- Telegram side -----------------------------------------------------

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
        threading.Thread(target=self.watch_loop, daemon=True).start()
        try:  # the old keyboard under the input field gives way to the panel
            telegram_notify.call(token, "sendMessage", {"chat_id": self.notifier.chat_id, "text": "✅ Бот запущен",
                                                        "reply_markup": {"remove_keyboard": True}})
        except TelegramError as exc:
            avito.say("TELEGRAM-ERROR", error=str(exc))
        self.show(self.menu_screen())
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
                except Exception:  # one bad update must not stop the bot
                    traceback.print_exc()

    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            chat = ((query.get("message") or {}).get("chat") or {}).get("id")
            telegram_notify.call(self.notifier.token, "answerCallbackQuery", {"callback_query_id": query["id"]})
            if chat != self.notifier.chat_id:
                return
            action, _, argument = str(query.get("data", "")).partition(":")
            self.command(action, argument, origin=query.get("message"))
            return
        message = update.get("message") or {}
        if (message.get("chat") or {}).get("id") != self.notifier.chat_id:
            if message:
                avito.say("BOT", text=f"сообщение из чужого чата {message.get('chat', {}).get('id')} пропущено")
            return
        text = str(message.get("text") or "").strip()
        link = AVITO_LINK.search(text)
        if text in OLD_BUTTONS:
            self.command(OLD_BUTTONS[text], "")
        elif link:
            self.enqueue(Job("проверка ссылки", lambda: self.add_and_scan(link.group(0).rstrip(".,;)"))))
        elif text.startswith("/"):
            command, _, argument = text[1:].partition(" ")
            self.command(command.split("@")[0].lower(), argument)
        else:
            self.show(screens.new_search().with_notice(screens.notice(
                "Не понял. Пришлите ссылку на поиск Авито — или выберите действие в меню.")))

    def command(self, command: str, argument: str, origin: dict[str, Any] | None = None) -> None:
        argument = argument.strip()
        if command in ("start", "menu", "status"):
            self.show(self.menu_screen(), origin)
        elif command == "list":
            self.show(screens.searches(self.infos()), origin)
        elif command == "watching":
            self.show(self.watching_screen(), origin)
        elif command == "new":
            self.show(screens.new_search(), origin)
        elif command == "help":
            self.show(screens.help_screen(self.check_minutes), origin)
        elif command == "stop":
            self.stop_jobs(origin)
        elif command == "clear":
            names = search_names()
            if names:
                self.show(screens.confirm_delete(f"все поиски ({len(names)})", "clearyes:", "list:"), origin)
            else:
                self.show(screens.searches([]), origin)
        elif command == "clearyes":
            self.delete(search_names(), origin)
        elif command == "noop":
            pass
        elif command in ("card", "delyes", "stopwatch", *ARGUMENT_COMMANDS):
            name = self.pick(argument, origin)
            if name is None:
                return
            if command == "card":
                self.show(screens.card(self.info(name)), origin)
            elif command == "delete":
                self.show(screens.confirm_delete(f"«{title_of(name)}»", f"delyes:{name}", f"card:{name}"), origin)
            elif command == "delyes":
                self.delete([name], origin)
            elif command == "stopwatch":
                self.set_watch(name, False)
                self.show(self.watching_screen(), origin)
            elif command in ("watch", "unwatch"):
                text = self.set_watch(name, command == "watch")
                self.show(screens.card(self.info(name)).with_notice(screens.notice(text)), origin)
            else:
                title = "сбор" if command == "scan" else "таблица"
                ahead = self.enqueue(Job(f"{title} «{title_of(name)}»",
                                         (lambda: self.scan(name)) if command == "scan" else (lambda: self.table(name))))
                text = ("Собираю — ход сбора будет отдельным сообщением ниже." if command == "scan"
                        else "Делаю таблицу — пришлю файлом ниже.")
                if ahead:
                    text = f"Поставил в очередь, перед ним {ahead}. " + text
                self.show(screens.card(self.info(name)).with_notice(screens.notice(f"{ce(RUNNING)} {esc(text)}")),
                          origin)
        else:
            self.show(self.menu_screen().with_notice(screens.notice("Не знаю такой команды — вот меню.")))

    def pick(self, argument: str, origin: dict[str, Any] | None) -> str | None:
        """The search a command is about; the list (with a note) when there is none such."""
        names = search_names()
        name = find_search(argument) if argument else (names[0] if len(names) == 1 else None)
        if name is None:
            note = f"Нет поиска «{esc(argument)}»." if argument else "Выберите поиск."
            self.show(screens.searches(self.infos()).with_notice(screens.notice(note)), origin)
        return name

    def delete(self, names: list[str], origin: dict[str, Any] | None) -> None:
        if not names:
            self.show(screens.searches(self.infos()).with_notice(screens.notice("Этого поиска уже нет.")), origin)
            return
        what = f"«{title_of(names[0])}»" if len(names) == 1 else f"все поиски ({len(names)})"
        # Through the queue: never in the middle of a scan of the same search.
        ahead = self.enqueue(Job(f"удаление: {what}", lambda: self.remove(names, what)))
        note = f"🗑 Удаляю {esc(what)}" + (" — после текущего сбора." if ahead else "…")
        self.show(screens.searches(self.infos()).with_notice(screens.notice(note)), origin)

    def remove(self, names: list[str], what: str) -> None:
        for name in names:
            with self.watch_lock:
                self.watching.discard(name)
            self.announce.discard(name)
            avito.delete_search(name)
        self.save_watching()
        self.say(f"🗑 Удалено: {esc(what)}.", (screens.TO_LIST,))

    def set_watch(self, name: str, on: bool) -> str:
        """Turn watching on or off; returns what happened (HTML)."""
        title = esc(title_of(name))
        if on and name not in self.watching:
            self.announce.add(name)
            with self.watch_lock:
                self.watching.add(name)
            self.watch_due = 0.0
            text = f"👀 Слежу за «{title}». Сначала запомню то, что уже выставлено, дальше пришлю сюда новые."
        elif on:
            text = f"Уже слежу за «{title}»."
        elif name in self.watching:
            with self.watch_lock:
                self.watching.discard(name)
            self.announce.discard(name)
            text = f"Больше не слежу за «{title}»."
        else:
            text = f"За «{title}» я и не следил."
        self.save_watching()
        return text

    def save_watching(self) -> None:
        with self.watch_lock:
            avito.save_json(watching_path(), {"searches": sorted(self.watching)})

    def stop_jobs(self, origin: dict[str, Any] | None = None) -> None:
        dropped = 0
        while True:
            try:
                self.jobs.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        if self.current is None:
            note = "Сбор не идёт." if not dropped else f"Очередь очищена ({dropped})."
        else:
            self.stop.set()
            note = "Останавливаю после текущего запроса к Авито, таблицу из собранного пришлю." + (
                f" Очередь очищена ({dropped})." if dropped else "")
        if self.watching:
            note += " Слежение продолжается — выключается в «Слежении»."
        if origin and origin.get("message_id") not in (None, self.panel_id):
            self.say(note)  # pressed under a job's progress: the panel stays where it is
        else:
            self.show(self.menu_screen().with_notice(screens.notice(esc(note))), origin)

    def enqueue(self, job: Job) -> int:
        """Queue a job; returns how many are ahead of it."""
        ahead = self.jobs.qsize() + (self.current is not None)
        self.jobs.put(job)
        return ahead

    # --- job thread ----------------------------------------------------

    def work(self) -> None:
        while True:
            job = self.jobs.get()
            self.current = job
            self.stop.clear()
            try:
                job.run()
            except avito.ScanStopped:
                self.say("⏹ Остановлено.")
            except avito.SearchError as exc:
                self.say(f"{ce(STOP)} {esc(exc)}", (screens.TO_MENU,))
            except SystemExit:
                self.say("✗ Не получилось, подробности в журнале бота.")
            except Exception as exc:  # the bot must outlive any single job
                traceback.print_exc()
                self.say(f"✗ Ошибка: {esc(type(exc).__name__)}: {esc(str(exc)[:300])}")
            finally:
                self.current = None
                self.reload_watches.set()

    def proxy_usable(self) -> bool:
        return bool(self.scan_proxy) and time.monotonic() >= self.proxy_off_until

    def proxy_down(self, reason: str) -> None:
        self.proxy_off_until = time.monotonic() + PROXY_RETRY_SECONDS
        self._job_clients.pop(True, None)  # a fresh session next time
        self.say(f"⚠️ Второй сервер для сбора недоступен ({esc(reason)}). Собираю отсюда, вперемешку со слежением; "
                 f"попробую снова через {PROXY_RETRY_SECONDS // 60} мин.")

    def job_client(self) -> tuple[avito_client.CalmClient, bool]:
        """The client for a job and whether it goes through the proxy."""
        via_proxy = self.proxy_usable()
        if via_proxy and not proxy_reachable(self.scan_proxy):
            self.proxy_down("туннель не отвечает")
            via_proxy = False
        if via_proxy not in self._job_clients:
            self._job_clients[via_proxy] = (
                avito_client.CalmClient(delay=self.delay, proxy=self.scan_proxy) if via_proxy
                else avito_client.CalmClient(pacer=self.pacer)  # this server's address: share its pace
            )
        return self._job_clients[via_proxy], via_proxy

    def with_job_client(self, progress: Progress, action: Callable[[avito_client.CalmClient], Any]) -> Any:
        """Run ``action`` with the job's client; if the proxy fails midway, again without it."""
        while True:
            client, progress.via_proxy = self.job_client()
            progress.proxy_failed = False
            client.log = progress.log  # captchas and blocks show in the job's progress
            try:
                result = action(client)
            except ProxyFailed:
                result = None
            finally:
                client.log = avito.say
            if not progress.proxy_failed:
                return result
            progress.error = ""  # the scan stopped only to switch; it goes on below
            progress.view.event = "↪ второй сервер недоступен, продолжаю отсюда"
            progress.show()

    def add_and_scan(self, url: str) -> None:
        existing = search_for_link(url)
        if existing:
            self.say(f"Этот поиск уже есть — «{esc(title_of(existing))}», собираю его.")
            self.scan(existing)
            return
        progress = Progress(self, f"<h3>{ce(RUNNING)} Проверяю ссылку на Авито</h3><p>Около минуты…</p>")
        name, resolved = self.with_job_client(
            progress, lambda client: avito.add_search(url, log=progress.log, client=client)
        )
        rows = [("Город", esc(resolved.city)), ("Объявлений сейчас", str(resolved.total))]
        if resolved.filters:
            rows.append(("Фильтры", esc("; ".join(resolved.filters))))
        head = f"<h3>✅ Поиск сохранён: {esc(resolved.title)}</h3>"
        if resolved.warnings:
            head += "".join(f"<p>⚠️ {esc(warning)}</p>" for warning in resolved.warnings)
        progress.set_head(head, rows=rows, stoppable=False)
        if resolved.total == 0:
            self.say("⚠️ По этой ссылке сейчас 0 объявлений, собирать нечего. Проверьте ссылку в браузере.")
            return
        self.scan(name)

    def scan(self, name: str) -> None:
        search = avito.load_search(name)
        progress = Progress(self, f"<h3>{ce(RUNNING)} Собираю «{esc(search.title)}»</h3>")
        path = self.with_job_client(
            progress, lambda client: avito.scan_search(search, delay=self.delay, log=progress.log, client=client)
        )
        self.send_table(search, path, progress)

    def table(self, name: str) -> None:
        search = avito.load_search(name)
        progress = Progress(self, f"<h3>{ce(STATS)} Делаю таблицу «{esc(search.title)}»</h3>")
        path = avito.write_table(search, search.load("seen.json"), log=progress.log)
        self.send_table(search, path, progress)

    def send_table(self, search: avito.Search, path: Path | None, progress: Progress) -> None:
        if path is None:
            progress.set_head(f"<h3>В базе «{esc(search.title)}» пока пусто</h3><p>Сначала соберите объявления.</p>",
                              stoppable=False)
            return
        rows = [(esc(key).capitalize(), esc(value)) for key, value in progress.table.items()]
        head = f"<h3>✅ Готово: {esc(search.title)}</h3>"
        if progress.error:
            head += f"<p>⚠️ Сбор прерван ({esc(progress.error[:200])}); «Собрать» продолжит с той же страницы.</p>"
        progress.set_head(head + "<p>Таблица — следующим сообщением.</p>", rows=rows, stoppable=False)
        caption = [f"📊 <b>{esc(search.title)}</b>"] + [f"{label}: {value}" for label, value in rows]
        self.notifier.send_document(path, caption="\n".join(caption)[:1024])

    # --- watch thread --------------------------------------------------

    def watch_loop(self) -> None:
        while True:
            if self.watching and time.monotonic() >= self.watch_due:
                try:
                    self.watch_cycle()
                except Exception:  # a bug in one cycle must not stop the thread
                    traceback.print_exc()
                    self.watch_due = time.monotonic() + self.watch_interval
            else:
                time.sleep(1)

    def watch_cycle(self) -> None:
        """Check every watched search once."""
        if self.watch_client is None:
            self.watch_client = avito_client.CalmClient(log=avito.say, pacer=self.pacer)
        if self.reload_watches.is_set():
            self.reload_watches.clear()
            for watch in self.watches.values():
                watch.reload()
        started = time.monotonic()
        self.cycles += 1
        with self.watch_lock:
            names = sorted(self.watching)
        for name in set(self.watches) - set(names):
            del self.watches[name]
        for name in names:
            if name in self.watching:  # not unwatched or deleted meanwhile
                self.check_one(name)
        # Counted from the start: with many searches the checks simply run back to back.
        self.watch_due = started + self.watch_interval * random.uniform(0.9, 1.1)

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
                self.say(f"⚠️ Не получается проверить «{esc(search.title)}» ({esc(error)}). Продолжаю пробовать.")
        if name in self.announce and watch.primed:
            self.announce.discard(name)
            self.say(f"✅ «{esc(search.title)}»: текущая выдача запомнена. Новые объявления пришлю сюда.")

    def alert(self, search: avito.Search, fields: dict[str, Any]) -> None:
        self.send(screens.alert(search.profile, search.name, fields))


def run(
    *,
    delay: float = avito_client.DEFAULT_DELAY_SECONDS,
    watch_interval: float = WATCH_INTERVAL_SECONDS,
    scan_proxy: str | None = None,
) -> None:
    notifier = telegram_notify.Notifier.from_settings()
    if notifier is None:
        raise TelegramError("Telegram не подключён: uv run python avito.py telegram setup")
    Bot(notifier, delay=delay, watch_interval=watch_interval, scan_proxy=scan_proxy).run()
