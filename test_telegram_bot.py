import json

import pytest

import avito
import telegram_bot
import telegram_notify
import tg_screens
import tg_ui

CHAT = 42


class FakeNotifier:
    token = "t"
    chat_id = CHAT

    def __init__(self):
        self.sent, self.edits, self.deleted, self.documents = [], [], [], []

    def send_screen(self, screen):
        self.sent.append(screen)
        return 100 + len(self.sent)

    def edit_screen(self, message_id, screen):
        self.edits.append((message_id, screen))
        return True

    def delete(self, message_id):
        self.deleted.append(message_id)

    def send_document(self, path, caption=""):
        self.documents.append((path, caption))

    @property
    def last(self):
        return self.sent[-1].text


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(avito, "SEARCHES_DIR", tmp_path / "searches")
    monkeypatch.setattr(avito, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(avito, "OUTPUT_DIR", tmp_path / "out")
    avito.SEARCHES_DIR.mkdir()
    for name, url in (("disks-moskva", "https://www.avito.ru/moskva/disks"), ("phones-kazan", "https://www.avito.ru/kazan/phones")):
        (avito.SEARCHES_DIR / f"{name}.json").write_text(json.dumps({
            "title": name, "profile": "goods", "catalog_url": url, "source_link": url,
            "params": {"categoryId": "1"},
        }), encoding="utf-8")
    monkeypatch.setattr(telegram_notify, "call", lambda *a, **k: True)
    return telegram_bot.Bot(FakeNotifier(), delay=0)


def message(text, chat=CHAT):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def callback(data, message_id=9, text="", chat=CHAT):
    return {"update_id": 5, "callback_query": {"id": "q", "data": data,
                                               "message": {"chat": {"id": chat}, "message_id": message_id, "text": text}}}


def data_of(screen):
    return [button.data for row in screen.rows for button in row]


# --- access, panel ----------------------------------------------------------

def test_other_chats_are_ignored(bot):
    bot.handle(message("/start", chat=7))
    bot.handle(message("https://www.avito.ru/moskva/x", chat=7))
    bot.handle(callback("delyes:disks-moskva", chat=7))
    assert bot.notifier.sent == [] and bot.jobs.empty() and telegram_bot.search_names() == ["disks-moskva", "phones-kazan"]


def test_one_panel_in_the_chat_redrawn_in_place(bot):
    bot.handle(message("/start"))
    first = bot.panel_id
    assert "Авито-поиски" in bot.notifier.last and "Поисков · 2" in bot.notifier.last
    assert data_of(bot.notifier.sent[-1])[:4] == ["list:", "watching:", "new:", "help:"]

    bot.handle(callback("list:", message_id=first))  # a button: the same message is redrawn
    assert bot.notifier.edits[-1][0] == first and "Мои поиски · 2" in bot.notifier.edits[-1][1].text
    assert len(bot.notifier.sent) == 1

    bot.handle(message("/start"))  # a typed command: a new panel, the old one goes
    assert bot.panel_id != first and bot.notifier.deleted == [first]


def test_menu_offers_stop_only_while_collecting(bot):
    stop = bot.menu_screen().rows[2][0]
    assert stop.disabled and stop.to_api() == {"text": "Сбор не идёт", "icon_custom_emoji_id": tg_ui.STOP.id,
                                               "disabled": {}}
    bot.current = telegram_bot.Job("сбор «disks-moskva»", lambda: None)
    stop = bot.menu_screen().rows[2][0]
    assert stop.data == "stop:" and stop.style == tg_ui.DANGER and "сбор «disks-moskva»" in bot.menu_screen().text


def test_buttons_of_the_old_keyboard_still_work(bot):
    bot.handle(message("📋 Мои поиски"))
    assert "Мои поиски · 2" in bot.notifier.last
    bot.handle(message("ℹ️ Что делаю"))
    assert "Авито-поиски" in bot.notifier.last


# --- searches and jobs -------------------------------------------------------

def test_list_opens_a_card_with_the_search_actions(bot):
    screen = tg_screens.searches(bot.infos())
    assert data_of(screen)[:2] == ["card:disks-moskva", "card:phones-kazan"] and "clear:" in data_of(screen)
    bot.handle(callback("card:disks-moskva"))
    card = bot.notifier.edits[-1][1]
    assert data_of(card)[:4] == ["scan:disks-moskva", "table:disks-moskva", "watch:disks-moskva", "delete:disks-moskva"]
    assert "ещё не собирали" in card.text and card.rows[0][0].text == "Собрать"


def test_link_and_commands_queue_jobs(bot):
    bot.handle(message("вот https://www.avito.ru/moskva/tovary?q=hdd."))
    bot.handle(message("/scan 2"))
    bot.handle(callback("table:disks-moskva"))
    titles = [bot.jobs.get_nowait().title for _ in range(3)]
    assert titles == ["проверка ссылки", "сбор «phones-kazan»", "таблица «disks-moskva»"]
    assert "Поставил в очередь, перед ним 2" in bot.notifier.edits[-1][1].text


def test_unknown_search_shows_the_list(bot):
    bot.handle(message("/scan nope"))
    assert "Нет поиска «nope»" in bot.notifier.last and "Мои поиски · 2" in bot.notifier.last


def test_known_link_is_scanned_not_added_again(bot, monkeypatch):
    scanned = []
    monkeypatch.setattr(bot, "scan", scanned.append)
    monkeypatch.setattr(avito, "add_search", lambda *a, **k: pytest.fail("added twice"))
    bot.add_and_scan("https://www.avito.ru/kazan/phones")
    assert scanned == ["phones-kazan"]


def test_scan_reports_pages_and_sends_the_table(bot, monkeypatch, tmp_path):
    table = tmp_path / "t.xlsx"
    table.write_bytes(b"xlsx")

    def fake_scan(search, *, delay, log, client=None):
        log("PAGE", search=search.name, page=1, items=50, new=50, total=120)
        log("TABLE", search=search.name, path=str(table), объявлений=50)
        return table

    monkeypatch.setattr(avito, "scan_search", fake_scan)
    bot.scan("disks-moskva")
    progress = bot.notifier.sent[0]
    assert data_of(progress) == ["stop:"]
    page = next(screen.text for _, screen in bot.notifier.edits if "Страница" in screen.text)
    assert "1 из ~3" in page and "50 объявлений" in page
    done = bot.notifier.edits[-1][1]
    assert "Готово" in done.text and not done.rows  # no stop button once done
    path, caption = bot.notifier.documents[0]
    assert path == table and "Объявлений: 50" in caption


def test_stop_interrupts_at_the_next_event_once(bot):
    progress = telegram_bot.Progress(bot, "<h3>head</h3>")
    bot.stop.set()
    with pytest.raises(avito.ScanStopped):
        progress.log("PAGE", search="x", page=1, items=1, new=1, total=1)
    progress.log("ERROR", search="x", page=1, error="остановлено")  # the scan's own report goes through
    assert progress.error == "остановлено"


def test_stop_under_a_progress_message_answers_there_not_in_the_panel(bot):
    bot.handle(message("/start"))
    bot.current = telegram_bot.Job("сбор «disks-moskva»", lambda: None)
    bot.handle(callback("stop:", message_id=555))
    assert bot.stop.is_set() and "Останавливаю" in bot.notifier.last and bot.notifier.edits == []


# --- watching ---------------------------------------------------------------

def test_watch_from_the_card_redraws_it_and_survives_restart(bot):
    bot.handle(callback("watch:disks-moskva", message_id=7))
    assert bot.watching == {"disks-moskva"}
    message_id, card = bot.notifier.edits[-1]
    assert message_id == 7 and "Слежу за «disks-moskva»" in card.text
    assert card.rows[1][0].data == "unwatch:disks-moskva" and "👀 включено" in card.text
    assert telegram_bot.Bot(FakeNotifier(), delay=0).watching == {"disks-moskva"}  # saved to disk

    bot.handle(callback("stopwatch:disks-moskva", message_id=8))  # from the watching screen
    assert bot.watching == set() and "Ни за чем не слежу" in bot.notifier.edits[-1][1].text
    assert telegram_bot.Bot(FakeNotifier(), delay=0).watching == set()


def test_stop_does_not_touch_watching(bot):
    bot.set_watch("disks-moskva", True)
    bot.handle(message("/stop"))
    assert "Слежение продолжается" in bot.notifier.last and not bot.stop.is_set()
    bot.handle(message("/watching"))
    assert "Слежение · 1" in bot.notifier.last and "disks-moskva" in bot.notifier.last


# --- deleting -----------------------------------------------------------------

def test_delete_asks_first_then_removes_the_search_its_data_and_watch(bot):
    (avito.DATA_DIR / "disks-moskva").mkdir(parents=True)
    (avito.DATA_DIR / "disks-moskva" / "listings.json").write_text("{}", encoding="utf-8")
    bot.set_watch("disks-moskva", True)

    bot.handle(callback("delete:disks-moskva"))
    question = bot.notifier.edits[-1][1]
    assert "Удалить «disks-moskva»?" in question.text
    assert data_of(question) == ["delyes:disks-moskva", "card:disks-moskva"]  # «Отмена» goes back to the card
    assert telegram_bot.search_names() == ["disks-moskva", "phones-kazan"]

    bot.handle(callback("delyes:disks-moskva", message_id=11))
    assert bot.notifier.edits[-1][0] == 11 and "Удаляю «disks-moskva»" in bot.notifier.edits[-1][1].text
    bot.jobs.get_nowait().run()
    assert telegram_bot.search_names() == ["phones-kazan"]
    assert not (avito.DATA_DIR / "disks-moskva").exists() and bot.watching == set()
    assert telegram_bot.load_watching() == set() and "Удалено" in bot.notifier.last


def test_clear_removes_every_search(bot):
    bot.handle(callback("clear:"))
    question = bot.notifier.edits[-1][1]
    assert "Удалить все поиски (2)?" in question.text and data_of(question) == ["clearyes:", "list:"]
    bot.handle(callback("clearyes:", message_id=13))
    bot.jobs.get_nowait().run()
    assert telegram_bot.search_names() == []
    bot.handle(message("/list"))
    assert "Поисков пока нет" in bot.notifier.last


# --- second server ------------------------------------------------------------

def test_jobs_go_through_the_proxy_with_its_own_pace(bot, monkeypatch):
    monkeypatch.setattr(telegram_bot, "proxy_reachable", lambda proxy: True)
    bot.scan_proxy = "socks5h://127.0.0.1:1080"
    client, via_proxy = bot.job_client()
    assert via_proxy and client.proxy == bot.scan_proxy and client.pacer is bot.proxy_pacer
    assert "через второй сервер" in bot.menu_screen().text

    monkeypatch.setattr(telegram_bot, "proxy_reachable", lambda proxy: False)
    bot._job_clients.clear()
    client, via_proxy = bot.job_client()
    assert not via_proxy and client.pacer is bot.pacer  # the same address: the same pace as the checks
    assert "Второй сервер для сбора недоступен" in bot.notifier.last
    assert "Второй сервер недоступен" in bot.menu_screen().text


@pytest.fixture
def watched(bot, monkeypatch):
    """Four watched searches; FakeWatch records which address each check used."""
    for name in ("cars-moskva", "flats-moskva"):
        (avito.SEARCHES_DIR / f"{name}.json").write_text(json.dumps({
            "title": name, "profile": "goods", "catalog_url": "https://www.avito.ru/moskva/x", "params": {},
        }), encoding="utf-8")
    checks, turned_away = [], set()

    class FakeWatch:
        def __init__(self, search):
            self.search, self.primed, self.profile, self.model, self.seen, self.reloads = search, True, None, None, {}, 0

        def merge_keys(self):
            return []

        def check(self, client, *, on_alert, log=None, instant=False):
            via = "proxy" if client.proxy else "direct"
            checks.append((self.search.name, via))
            if via in turned_away:
                client.log("IP-BLOCK", reason="captcha", wait_min=10)

        def reload(self):
            self.reloads += 1

    monkeypatch.setattr(avito, "Watch", FakeWatch)
    monkeypatch.setattr(telegram_bot, "proxy_reachable", lambda proxy: True)
    bot.scan_proxy = "socks5h://127.0.0.1:1080"
    bot.watch_interval = 0
    for name in telegram_bot.search_names():
        bot.set_watch(name, True)
    return checks, turned_away

def test_scan_goes_on_from_this_server_when_the_proxy_is_blocked(bot, monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "proxy_reachable", lambda proxy: True)
    bot.scan_proxy = "socks5h://127.0.0.1:1080"
    table = tmp_path / "t.xlsx"
    table.write_bytes(b"xlsx")
    calls = []

    def fake_scan(search, *, delay, log, client=None):  # like avito.scan_search: a stop still writes the table
        calls.append(client.proxy)
        try:
            log("PAGE", search=search.name, page=len(calls), items=50, new=50, total=100)
            if client.proxy:
                client.log("IP-BLOCK", reason="captcha", wait_min=10)
        except avito.ScanStopped:
            log("ERROR", search=search.name, page=1, error="остановлено; следующий scan продолжит с этой страницы")
        log("TABLE", search=search.name, path=str(table), объявлений=100)
        return table

    monkeypatch.setattr(avito, "scan_search", fake_scan)
    bot.scan("disks-moskva")
    assert calls == [bot.scan_proxy, None]
    assert any("Второй сервер для сбора недоступен" in screen.text for screen in bot.notifier.sent)
    path, caption = bot.notifier.documents[0]
    assert path == table and "прерван" not in bot.notifier.edits[-1][1].text
    assert not bot.proxy_usable()  # retried later, not on every job


def test_clients_from_one_address_share_one_pace(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(telegram_bot.avito_client.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(telegram_bot.avito_client.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s))
    monkeypatch.setattr(telegram_bot.avito_client.random, "uniform", lambda a, b: 1.0)
    pacer = telegram_bot.avito_client.Pacer(30)
    starts = []
    for _ in range(3):
        pacer.wait()  # a scan and a watch check taking turns
        starts.append(clock["now"])
    assert starts == [1000.0, 1030.0, 1060.0]


def test_scan_and_watch_saves_keep_each_others_listings(tmp_path, monkeypatch):
    monkeypatch.setattr(avito, "DATA_DIR", tmp_path)
    search = avito.Search(name="s", title="s", profile="goods", catalog_url="https://www.avito.ru/moskva/x", params={})
    search.save_collected({"1": {"id": 1, "price": 10}}, {"1": "10:00"})        # the scan, page 1
    search.save_collected({"2": {"id": 2}}, {"2": "10:01", "1": "10:05"})        # a watch check in between
    search.save_collected({"1": {"id": 1, "price": 9}, "3": {"id": 3}}, {"1": "10:00", "3": "10:02"})  # the scan, page 2
    assert search.load("listings.json") == {"1": {"id": 1, "price": 9}, "2": {"id": 2}, "3": {"id": 3}}
    assert search.load("seen.json") == {"1": "10:00", "2": "10:01", "3": "10:02"}


# --- Telegram plumbing ----------------------------------------------------------

def test_document_upload_is_multipart_with_the_file(tmp_path):
    path = tmp_path / "table.xlsx"
    path.write_bytes(b"\x00binary")
    body, content_type = telegram_notify._multipart({"chat_id": "42", "caption": "Таблица"}, "document", path)
    boundary = content_type.split("boundary=")[1]
    assert body.count(f"--{boundary}".encode()) == 4
    assert b'name="document"; filename="table.xlsx"' in body and b"\x00binary" in body
    assert "Таблица".encode() in body


def test_a_refused_rich_message_goes_out_as_plain_text(monkeypatch):
    calls = []

    def fake_call(token, method, payload=None, **kwargs):
        calls.append((method, payload))
        if method == "sendRichMessage":
            raise telegram_notify.TelegramError("Telegram ответил 400: Bad Request: can't parse rich message")
        return {"message_id": 77}

    monkeypatch.setattr(telegram_notify, "call", fake_call)
    screen = tg_ui.Screen("<h3>Заголовок</h3><table><tr><td>Цена</td><td>1 ₽</td></tr></table>",
                          ((tg_ui.Button("В меню", "menu:"),),))
    assert telegram_notify.Notifier("t", 42).send_screen(screen) == 77
    method, payload = calls[-1]
    assert method == "sendMessage" and payload["text"] == "Заголовок\nЦена · 1 ₽"
    assert payload["reply_markup"] == {"inline_keyboard": [[{"text": "В меню", "callback_data": "menu:"}]]}


def test_buttons_and_plain_text_like_the_other_bot():
    assert tg_ui.Button("Открыть", url="https://x", style=tg_ui.PRIMARY, icon=tg_ui.SEARCHES).to_api() == {
        "text": "Открыть", "style": "primary", "icon_custom_emoji_id": tg_ui.SEARCHES.id, "url": "https://x"}
    assert [tg_ui.plural(n, "поиск", "поиска", "поисков") for n in (1, 3, 5, 11, 21)] == [
        "поиск", "поиска", "поисков", "поисков", "поиск"]
    assert tg_ui.to_plain(f"<h2>{tg_ui.ce(tg_ui.STATS)} Меню</h2><p>a<br>b</p>") == "📊 Меню\na\nb"


def test_a_flat_alert_carries_the_draft_and_the_link():
    screen = tg_screens.alert("realty-owner", "flats-spb", {
        "title": "2-к. квартира", "price": "12 000 000 ₽", "price_per_m2": "240 000 ₽", "address": "ул. Примерная, 1",
        "metro": "Примерная", "verdict": "собственник", "reasons": ["пишет «собственник»"], "notes": [],
        "published": "1 час назад", "url": "https://www.avito.ru/y_2", "draft": "Здравствуйте! <тест>",
    })
    assert "12 000 000 ₽" in screen.text and "Черновик" in screen.text and "&lt;тест&gt;" in screen.html
    assert screen.rows[0][0].url == "https://www.avito.ru/y_2" and "#flats_spb" in screen.text


def test_watch_checks_take_turns_between_the_two_addresses(bot, watched):
    checks, _ = watched
    for _ in range(4):
        bot.watch_step()
    assert [via for _, via in checks] == ["direct", "proxy", "direct", "proxy"]
    assert sorted(name for name, _ in checks) == ["cars-moskva", "disks-moskva", "flats-moskva", "phones-kazan"]
    assert bot.watch_client(True).pacer is bot.proxy_pacer and bot.watch_client(False).pacer is bot.pacer

    checks.clear()
    bot.proxy_off_until = telegram_bot.time.monotonic() + 60  # the second server is out
    for _ in range(4):
        bot.watch_step()
    assert [via for _, via in checks] == ["direct"] * 4


def test_each_address_keeps_its_own_spacing(bot, watched):
    checks, _ = watched
    bot.watch_interval = 3600
    assert bot.watch_step() and bot.watch_step()  # one check from each address
    before = len(checks)
    bot.watch_step()  # both just used: it waits instead of checking
    assert len(checks) == before == 2


def test_a_turned_away_address_rests_and_the_other_carries_on(bot, watched):
    checks, turned_away = watched
    turned_away.add("direct")
    for _ in range(4):
        bot.watch_step()
    # the first check is turned away; the rest go through the second server instead of waiting 10 minutes
    assert checks[0][1] == "direct" and [via for _, via in checks[1:]] == ["proxy", "proxy", "proxy"]
    assert bot.rested_until[False] > telegram_bot.time.monotonic() + 500
    assert bot.watches[checks[0][0]].reloads == 1  # what the cut check saw is not taken as seen


def test_with_both_addresses_turned_away_the_watch_waits(bot, watched):
    checks, turned_away = watched
    turned_away.update({"direct", "proxy"})
    for _ in range(4):
        bot.watch_step()
    assert len(checks) == 2 and bot.watch_addresses() == []


def test_alert_goes_out_at_once_then_shows_what_the_page_told(bot):
    search = avito.load_search("disks-moskva")
    fields = {"title": "iPhone 15", "price": "30 000 ₽", "seller": "частное лицо", "url": "https://www.avito.ru/x_1"}
    update = bot.alert(search, fields)
    first = bot.notifier.sent[-1]
    assert "проверяю, новое ли" in first.text and first.rows[0][0].url == "https://www.avito.ru/x_1"
    update({**fields, "views": "3 (+3 сегодня)"}, True)
    assert "3 (+3 сегодня)" in bot.notifier.edits[-1][1].text and "Переопубликовано" not in bot.notifier.edits[-1][1].text
    update({**fields, "views": "8526 (+109 сегодня)"}, False)
    assert "Переопубликовано" in bot.notifier.edits[-1][1].text
