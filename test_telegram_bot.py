import json

import pytest

import avito
import telegram_bot
import telegram_notify

CHAT = 42


class FakeNotifier:
    token = "t"
    chat_id = CHAT

    def __init__(self):
        self.sent, self.edits, self.documents = [], [], []

    def send(self, text, *, preview=True, **extra):
        self.sent.append((text, extra))
        return len(self.sent)

    def edit(self, message_id, text, **extra):
        self.edits.append((message_id, text))

    def send_document(self, path, caption=""):
        self.documents.append((path, caption))


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


def test_other_chats_are_ignored(bot):
    bot.handle(message("/list", chat=7))
    bot.handle(message("https://www.avito.ru/moskva/x", chat=7))
    assert bot.notifier.sent == [] and bot.jobs.empty()


def test_link_and_commands_queue_jobs(bot):
    bot.handle(message("вот https://www.avito.ru/moskva/tovary?q=hdd."))
    bot.handle(message("/scan 2"))
    bot.handle({"update_id": 2, "callback_query": {"id": "q", "data": "table:disks-moskva",
                                                    "message": {"chat": {"id": CHAT}}}})
    titles = [bot.jobs.get_nowait().title for _ in range(3)]
    assert titles == ["ссылка", "сбор «phones-kazan»", "таблица «disks-moskva»"]


def test_menu_buttons_run_their_commands(bot):
    bot.handle(message("/start"))
    assert bot.notifier.sent[0][1]["reply_markup"] == telegram_bot.KEYBOARD
    bot.handle(message("📋 Мои поиски"))
    assert "Мои поиски" in bot.notifier.sent[1][0] and "inline_keyboard" in bot.notifier.sent[1][1]["reply_markup"]
    bot.handle(message("🔗 Новый поиск"))
    assert "пришлите её сюда" in bot.notifier.sent[2][0]
    bot.handle(message("⏹ Остановить"))
    assert bot.notifier.sent[3][0] == "Нечего останавливать."
    assert set(telegram_bot.BUTTONS) == {text for row in telegram_bot.MENU for text in row}


def test_unknown_search_shows_the_list_with_buttons(bot):
    bot.handle(message("/scan nope"))
    assert "Нет поиска" in bot.notifier.sent[0][0]
    keyboard = bot.notifier.sent[1][1]["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0]["callback_data"] == "scan:disks-moskva"


def test_known_link_is_scanned_not_added_again(bot, monkeypatch):
    scanned = []
    monkeypatch.setattr(bot, "scan", scanned.append)
    monkeypatch.setattr(avito, "add_search", lambda *a, **k: pytest.fail("added twice"))
    bot.add_and_scan("https://www.avito.ru/kazan/phones")
    assert scanned == ["phones-kazan"]


def test_scan_reports_pages_and_sends_the_table(bot, monkeypatch, tmp_path):
    table = tmp_path / "t.xlsx"
    table.write_bytes(b"xlsx")

    def fake_scan(search, *, delay, log):
        log("PAGE", search=search.name, page=1, items=50, new=50, total=120)
        log("TABLE", search=search.name, path=str(table), объявлений=50)
        return table

    monkeypatch.setattr(avito, "scan_search", fake_scan)
    bot.scan("disks-moskva")
    assert any("стр. 1 из ~3 · собрано 50" in text for _, text in bot.notifier.edits)
    path, caption = bot.notifier.documents[0]
    assert path == table and "объявлений: 50" in caption


def test_stop_interrupts_at_the_next_event_once(bot):
    progress = telegram_bot.Progress(bot, "head")
    bot.stop.set()
    with pytest.raises(avito.ScanStopped):
        progress.log("PAGE", search="x", page=1, items=1, new=1, total=1)
    progress.log("ERROR", search="x", page=1, error="остановлено")  # the scan's own report goes through
    assert progress.error == "остановлено"


def test_document_upload_is_multipart_with_the_file(tmp_path):
    path = tmp_path / "table.xlsx"
    path.write_bytes(b"\x00binary")
    body, content_type = telegram_notify._multipart({"chat_id": "42", "caption": "Таблица"}, "document", path)
    boundary = content_type.split("boundary=")[1]
    assert body.count(f"--{boundary}".encode()) == 4
    assert b'name="document"; filename="table.xlsx"' in body and b"\x00binary" in body
    assert "Таблица".encode() in body


def test_watch_button_toggles_redraws_the_list_and_survives_restart(bot):
    origin = {"message_id": 7, "text": "Мои поиски\n1. disks-moskva"}
    bot.handle({"update_id": 3, "callback_query": {"id": "q", "data": "watch:disks-moskva",
                                                    "message": {"chat": {"id": CHAT}, **origin}}})
    assert bot.watching == {"disks-moskva"} and "Слежу за «disks-moskva»" in bot.notifier.sent[0][0]
    message_id, text = bot.notifier.edits[-1]
    assert message_id == 7 and "👀" in text
    _, markup = bot.list_view()
    assert markup["inline_keyboard"][0][2] == {"text": "🔕 Не следить", "callback_data": "unwatch:disks-moskva"}
    assert telegram_bot.Bot(FakeNotifier(), delay=0).watching == {"disks-moskva"}  # saved to disk

    bot.handle(message("/unwatch 1"))
    assert bot.watching == set() and "Больше не слежу" in bot.notifier.sent[-1][0]
    assert telegram_bot.Bot(FakeNotifier(), delay=0).watching == set()


def test_watch_cycle_primes_then_alerts_and_waits_for_the_interval(bot, monkeypatch):
    checks = []

    class FakeWatch:
        def __init__(self, search):
            self.search, self.primed, self.profile = search, False, None

        def check(self, client, *, on_alert, log=None):
            checks.append(self.search.name)
            if self.primed:
                on_alert({"title": "iPhone", "price": "1 ₽", "seller": "частное лицо", "url": "https://www.avito.ru/x_1"})
            self.primed = True

        def reload(self):
            pass

    monkeypatch.setattr(avito, "Watch", FakeWatch)
    monkeypatch.setattr(telegram_bot.avito_client, "CalmClient", lambda **kwargs: object())
    bot.set_watch("phones-kazan", True)
    bot.watch_cycle()
    assert checks == ["phones-kazan"] and "текущая выдача запомнена" in bot.notifier.sent[-1][0]
    assert bot.watch_due > telegram_bot.time.monotonic() and bot.current is None
    bot.watch_cycle()
    assert 'href="https://www.avito.ru/x_1"' in bot.notifier.sent[-1][0]


def test_a_waiting_job_goes_before_the_watch_cycle(bot, monkeypatch):
    monkeypatch.setattr(avito, "Watch", lambda search: pytest.fail("checked while a job waits"))
    monkeypatch.setattr(telegram_bot.avito_client, "CalmClient", lambda **kwargs: object())
    bot.set_watch("phones-kazan", True)
    bot.jobs.put(telegram_bot.Job("сбор", lambda: None))
    bot.watch_cycle()
    assert bot.watch_due == 0.0  # the cycle starts over right after the job


def test_status_and_stop_know_about_watching(bot):
    bot.set_watch("disks-moskva", True)
    bot.handle(message("ℹ️ Что делаю"))
    assert "Слежу за: disks-moskva" in bot.notifier.sent[-1][0]
    bot.current = telegram_bot.Job(telegram_bot.WATCH_JOB, lambda: None)
    bot.handle(message("⏹ Остановить"))
    assert "проверка новых объявлений" in bot.notifier.sent[-1][0] and not bot.stop.is_set()
    bot.handle(message("👀 Слежение"))
    assert bot.notifier.sent[-1][0].startswith("<b>Слежение</b>")


def callback(data, message_id=9, text="Мои поиски"):
    return {"update_id": 5, "callback_query": {"id": "q", "data": data,
                                               "message": {"chat": {"id": CHAT}, "message_id": message_id, "text": text}}}


def test_delete_asks_first_then_removes_the_search_its_data_and_watch(bot):
    (avito.DATA_DIR / "disks-moskva").mkdir(parents=True)
    (avito.DATA_DIR / "disks-moskva" / "listings.json").write_text("{}", encoding="utf-8")
    bot.set_watch("disks-moskva", True)
    _, markup = bot.list_view()
    assert markup["inline_keyboard"][0][3] == {"text": "🗑", "callback_data": "delete:disks-moskva"}

    bot.handle(callback("delete:disks-moskva"))
    question, extra = bot.notifier.sent[-1]
    assert "Удалить поиск «disks-moskva»" in question and telegram_bot.search_names() == ["disks-moskva", "phones-kazan"]
    yes = extra["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

    bot.handle(callback(yes, message_id=11, text=question))
    assert bot.notifier.edits[-1] == (11, "🗑 Удаляю «disks-moskva»…")
    bot.jobs.get_nowait().run()
    assert telegram_bot.search_names() == ["phones-kazan"]
    assert not (avito.DATA_DIR / "disks-moskva").exists() and bot.watching == set()
    assert telegram_bot.load_watching() == set() and "Удалено" in bot.notifier.sent[-1][0]


def test_clear_removes_every_search_and_cancel_keeps_them(bot):
    bot.handle(callback("clear:"))
    assert "Удалить все поиски (2)" in bot.notifier.sent[-1][0]
    bot.handle(callback("cancel:", message_id=12))
    assert bot.notifier.edits[-1] == (12, "Отменено, ничего не удалял.") and len(telegram_bot.search_names()) == 2

    bot.handle(message("/clear"))
    bot.handle(callback("clearyes:", message_id=13))
    bot.jobs.get_nowait().run()
    assert telegram_bot.search_names() == [] and "Список поисков пуст" in bot.notifier.sent[-1][0]
