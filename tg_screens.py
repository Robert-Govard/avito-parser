"""The Telegram bot's screens: plain data in, a tg_ui.Screen out.

Navigation buttons carry ``action:argument`` (a search name has no colon).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tg_ui import (
    DANGER, DATE, HOME, IDLE, LINK, PRICE, PRIMARY, REFRESH, ROUTE, RUNNING, SEARCHES, SELLER, SETTINGS,
    SHIELD, STATS, STOP, SUCCESS, WATCH, Button, Icon, Screen, ce, esc, plural,
)

TITLE_IN_BUTTON = 38
TO_MENU = Button("В меню", "menu:", icon=HOME)
TO_LIST = Button("К поискам", "list:", icon=HOME)


def _row(label: str, value: str, icon: Icon | None = None) -> str:
    head = f"{ce(icon)} {label}" if icon else label
    return f'<tr><td>{head}</td><td align="right">{value}</td></tr>'


def _short(title: str) -> str:
    return title if len(title) <= TITLE_IN_BUTTON else title[:TITLE_IN_BUTTON - 1] + "…"


def _count(n: int, one: str, few: str, many: str) -> str:
    return f"{n:,}".replace(",", " ") + " " + plural(n, one, few, many)


def every(seconds: float) -> str:
    """«~45 с», «~2 мин»: how often something happens."""
    if seconds < 90:
        return f"~{max(5, round(seconds / 5) * 5)} с"
    return f"~{round(seconds / 60)} мин"


def notice(text: str) -> str:
    """A line on top of a screen saying what the last button did (``text`` is HTML)."""
    return f"<blockquote>{text}</blockquote>"


# --- main menu --------------------------------------------------------------

@dataclass(frozen=True)
class MenuView:
    searches: int
    listings: int
    watching: int
    check_seconds: float
    new_today: int
    job: str | None = None
    queued: int = 0
    route: str | None = None  # "proxy", "direct" (proxy down) or None (no second server)
    now: datetime = field(default_factory=datetime.now)


def menu(v: MenuView) -> Screen:
    status = [f"{ce(RUNNING)} {esc(v.job)}" if v.job else f"{ce(IDLE)} Сбор не идёт",
              f"{ce(WATCH)} слежу за {_count(v.watching, 'поиском', 'поисками', 'поисками')}" if v.watching
              else "слежение выключено"]
    warnings = []
    if v.route == "direct":
        warnings.append(f"{ce(ROUTE)} Второй сервер недоступен: сбор идёт отсюда, вперемешку со слежением")
    if v.searches == 0:
        warnings.append(f"{ce(LINK)} Поисков пока нет — нажмите «Новый поиск»")
    rows = [
        _row("Поисков", f"<b>{v.searches}</b>", SEARCHES),
        _row("Объявлений собрано", f"{v.listings:,}".replace(",", " "), STATS),
        _row("Слежение", f"{v.watching} · проверка раз в {every(v.check_seconds)}" if v.watching else "выключено", WATCH),
        _row("Новых сегодня", str(v.new_today), DATE),
    ]
    if v.route:
        rows.append(_row("Сбор идёт", "через второй сервер" if v.route == "proxy" else "с этого сервера", ROUTE))
    if v.queued:
        rows.append(_row("В очереди", str(v.queued)))
    html = "".join([
        f"<h2>{ce(STATS)} Авито-поиски</h2>",
        "<p>" + " · ".join(status) + "</p>",
        *(f"<p>{line}</p>" for line in warnings),
        f"<table compact>{''.join(rows)}</table>",
        f"<footer>обновлено {v.now:%H:%M}</footer>",
    ])
    stop = (Button("Остановить сбор", "stop:", DANGER, icon=STOP) if v.job
            else Button("Сбор не идёт", disabled=True, icon=STOP))
    return Screen(html, (
        (Button("Мои поиски", "list:", PRIMARY, icon=SEARCHES), Button("Слежение", "watching:", PRIMARY, icon=WATCH)),
        (Button("Новый поиск", "new:", PRIMARY, icon=LINK), Button("Помощь", "help:", icon=SETTINGS)),
        (stop,),
        (Button("Обновить", "menu:", icon=REFRESH),),
    ))


# --- searches ---------------------------------------------------------------

@dataclass(frozen=True)
class SearchInfo:
    name: str
    title: str
    profile: str
    url: str
    stored: int = 0
    complete: bool = False
    next_page: int = 1
    watched: bool = False
    new_today: int = 0


def searches(items: list[SearchInfo]) -> Screen:
    if not items:
        html = (f"<h2>{ce(SEARCHES)} Мои поиски</h2>"
                "<p>Поисков пока нет. Нажмите «Новый поиск» и пришлите ссылку на поиск Авито.</p>")
        return Screen(html, ((Button("Новый поиск", "new:", PRIMARY, icon=LINK), TO_MENU),))
    lines = "".join(
        f'<tr><td align="center">{n}</td><td>{esc(s.title)}</td>'
        f'<td align="right">{s.stored}</td><td align="center">{"👀" if s.watched else ""}</td></tr>'
        for n, s in enumerate(items, start=1)
    )
    html = (f"<h2>{ce(SEARCHES)} Мои поиски · {len(items)}</h2>"
            "<table bordered striped compact><tr><th>№</th><th>Поиск</th><th>Объявлений</th><th>Слежу</th></tr>"
            f"{lines}</table>"
            "<p>Нажмите поиск, чтобы собрать его, получить таблицу, включить слежение или удалить.</p>")
    rows = [(Button(f"{n}. {_short(s.title)}", f"card:{s.name}", icon=WATCH if s.watched else SEARCHES),)
            for n, s in enumerate(items, start=1) if len(f"delyes:{s.name}".encode()) <= 64]
    rows.append((Button("Новый поиск", "new:", PRIMARY, icon=LINK), TO_MENU))
    if len(items) > 1:
        rows.append((Button("Очистить список", "clear:", DANGER, icon=STOP),))
    return Screen(html, tuple(rows))


def card(s: SearchInfo) -> Screen:
    if s.complete:
        progress, collect = "собран целиком", "Собрать заново"
    elif s.stored:
        progress, collect = f"остановлен, продолжит со стр. {s.next_page}", "Дособрать"
    else:
        progress, collect = "ещё не собирали", "Собрать"
    rows = [
        _row("Тип", "квартиры от собственников" if s.profile == "realty-owner" else "товары"),
        _row("Собрано", _count(s.stored, "объявление", "объявления", "объявлений"), STATS),
        _row("Сбор", progress),
        _row("Слежение", "👀 включено" if s.watched else "выключено", WATCH),
    ]
    if s.watched:
        rows.append(_row("Новых сегодня", str(s.new_today), DATE))
    html = (f"<h3>{ce(SEARCHES)} {esc(s.title)}</h3><table compact>{''.join(rows)}</table>"
            f'<p><a href="{esc(s.url)}">Открыть поиск на Авито</a></p>')
    watch = (Button("Не следить", f"unwatch:{s.name}", icon=WATCH) if s.watched
             else Button("Следить за новыми", f"watch:{s.name}", SUCCESS, icon=WATCH))
    return Screen(html, (
        (Button(collect, f"scan:{s.name}", PRIMARY, icon=RUNNING), Button("Таблица Excel", f"table:{s.name}", icon=STATS)),
        (watch,),
        (Button("Удалить", f"delete:{s.name}", DANGER, icon=STOP),),
        (TO_LIST, TO_MENU),
    ))


def confirm_delete(what: str, yes: str, back: str) -> Screen:
    html = (f"<h3>{ce(STOP)} Удалить {esc(what)}?</h3>"
            "<p>Поиск и собранные по нему объявления будут удалены. "
            "Таблицы, которые я уже присылал, останутся в чате.</p>")
    return Screen(html, ((Button("Да, удалить", yes, DANGER, icon=STOP),), (Button("Отмена", back, icon=HOME),)))


# --- watching ---------------------------------------------------------------

WATCH_HELP = (
    "Раз в {period} смотрю свежие объявления каждого поиска и сразу присылаю сюда те, которых раньше не было: "
    "по товарам — от частных лиц своего города, по квартирам — от собственников; выставленные за последние "
    "часы, без переопубликованных старых."
)


def watching(items: list[SearchInfo], check_seconds: float) -> Screen:
    if not items:
        html = (f"<h2>{ce(WATCH)} Слежение</h2><p>Ни за чем не слежу.</p>"
                "<p>Откройте «Мои поиски», выберите поиск и нажмите «Следить за новыми».</p>")
        return Screen(html, ((Button("Мои поиски", "list:", PRIMARY, icon=SEARCHES), TO_MENU),))
    lines = "".join(f'<tr><td align="center">{n}</td><td>{esc(s.title)}</td><td align="center">{s.new_today}</td></tr>'
                    for n, s in enumerate(items, start=1))
    html = (f"<h2>{ce(WATCH)} Слежение · {len(items)}</h2>"
            f"<p>{esc(WATCH_HELP.format(period=every(check_seconds)))}</p>"
            "<table bordered striped compact><tr><th>№</th><th>Поиск</th><th>Новых сегодня</th></tr>"
            f"{lines}</table>")
    rows = [(Button(f"Не следить: {_short(s.title)}", f"stopwatch:{s.name}", icon=WATCH),) for s in items]
    rows.append((Button("Мои поиски", "list:", PRIMARY, icon=SEARCHES), TO_MENU))
    return Screen(html, tuple(rows))


# --- new search, help -------------------------------------------------------

def new_search() -> Screen:
    html = (f"<h2>{ce(LINK)} Новый поиск</h2><ol>"
            "<li>Откройте поиск на Авито в браузере или приложении.</li>"
            "<li>Выставьте город, категорию и фильтры.</li>"
            "<li>Скопируйте ссылку («Поделиться» → «Копировать») и пришлите её сюда сообщением.</li>"
            "</ol><p>Я проверю ссылку, соберу все объявления и пришлю таблицу Excel.</p>")
    return Screen(html, ((TO_MENU,),))


COMMANDS_HELP = (
    ("/start", "меню"), ("/list", "мои поиски"), ("/scan имя или номер", "собрать поиск"),
    ("/table имя или номер", "таблица из собранного"), ("/watch, /unwatch", "следить за новыми или перестать"),
    ("/delete имя, /clear", "удалить поиск или все"), ("/stop", "остановить сбор"),
)


def help_screen(check_seconds: float) -> Screen:
    commands = "".join(f"<tr><td><code>{esc(c)}</code></td><td>{esc(d)}</td></tr>" for c, d in COMMANDS_HELP)
    html = (f"<h2>{ce(SETTINGS)} Как пользоваться</h2>"
            f"<h4>{ce(STATS)} Собрать объявления</h4>"
            "<p>Пришлите ссылку на поиск Авито — соберу все объявления и пришлю таблицу Excel. "
            "Сбор идёт спокойно, ~30 с на страницу из 50 объявлений, чтобы Авито не блокировал.</p>"
            f"<h4>{ce(WATCH)} Следить за новыми</h4>"
            f"<p>«Мои поиски» → поиск → «Следить за новыми». {esc(WATCH_HELP.format(period=every(check_seconds)))}</p>"
            f"<h4>{ce(SHIELD)} Капча и блокировки</h4>"
            "<p>Капчу решаю сам. Если Авито временно ограничит доступ, подожду и продолжу — "
            "собранное не теряется, «Собрать» продолжит с той же страницы.</p>"
            f"<h4>Команды</h4><table compact>{commands}</table>")
    return Screen(html, ((TO_MENU,),))


# --- a job's progress -------------------------------------------------------

@dataclass
class ProgressView:
    head: str  # HTML
    rows: list[tuple[str, str]] = field(default_factory=list)  # label, value (HTML)
    event: str = ""  # plain text
    stoppable: bool = True


def progress(v: ProgressView) -> Screen:
    html = v.head
    if v.rows:
        html += "<table compact>" + "".join(_row(label, value) for label, value in v.rows) + "</table>"
    if v.event:
        html += f"<p>{esc(v.event)}</p>"
    rows = ((Button("Остановить", "stop:", DANGER, icon=STOP),),) if v.stoppable else ()
    return Screen(html, rows)


# --- a new listing ----------------------------------------------------------

def alert(profile: str, search: str, a: dict, state: str | None = None) -> Screen:
    """A new listing, from the fields avito.Watch passes to ``on_alert``.

    ``state``: "checking" while its page is being opened, "republished" when
    the page shows views from earlier days (an old listing put up again).
    """
    rows: list[str] = []
    if profile == "realty-owner":
        head = f"🏠 {esc(a['title'])}"
        rows.append(_row("Цена", f"<b>{esc(a['price'])}</b>", PRICE))
        if a.get("price_per_m2"):
            rows.append(_row("За м²", esc(a["price_per_m2"])))
        place = esc(a.get("address", "")) + (f" · м. {esc(a['metro'])}" if a.get("metro") else "")
        if place:
            rows.append(_row("Адрес", place))
        rows.append(_row("Продаёт", esc(a.get("verdict", "")), SELLER))
    else:
        head = f"📱 {esc(a['title'])}"
        rows.append(_row("Цена", f"<b>{esc(a['price'])}</b>", PRICE))
        if a.get("vs_market"):
            rows.append(_row("Рынок", f"{esc(a['market'])} ({esc(a['vs_market'])})", STATS))
        if a.get("avito_badge"):
            rows.append(_row("Оценка Авито", esc(a["avito_badge"])))
        seller = esc(a.get("seller", ""))
        if a.get("closed_listings") is not None:
            seller += f", {esc(a['closed_listings'])} завершённых"
        rows.append(_row("Продавец", seller, SELLER))
    when = a.get("listed_at") or a.get("published")
    if when:
        rows.append(_row("В выдаче с", esc(when), DATE))
    if a.get("views"):
        rows.append(_row("Просмотры", esc(a["views"])))
    elif state == "checking":
        rows.append(_row("Просмотры", "🔎 проверяю, новое ли…"))
    if state == "republished":
        head = f"♻️ Переопубликовано: {head}"
    parts = [f"<h3>{head}</h3>", f"<table compact>{''.join(rows)}</table>"]
    if state == "republished":
        parts.append("<blockquote>Старое объявление выставлено заново: у него просмотры с прошлых дней. "
                     "Скорее всего, неинтересно.</blockquote>")
    if a.get("reasons"):
        parts.append(f"<p>{esc('; '.join(a['reasons']))}</p>")
    if a.get("notes"):
        parts.append(f"<blockquote>⚠️ {esc('; '.join(a['notes']))}</blockquote>")
    if a.get("draft"):
        parts.append(f"<h4>Черновик сообщения</h4><pre>{esc(a['draft'])}</pre>")
    parts.append(f"<footer>#{esc(search.replace('-', '_'))}</footer>")
    return Screen("".join(parts), ((Button("Открыть на Авито", url=a["url"], style=PRIMARY, icon=SEARCHES),),))
