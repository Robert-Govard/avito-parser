import json

import avito
import avito_client
from test_goods import phone

MOSCOW = "https://www.avito.ru/moskva/telefony/mobilnye_telefony/apple/iphone_13"


def iphone(item_id, model, price=30_000):
    return phone(item_id, price, title=f"{model}, 128 ГБ, SIM + eSIM", city="moskva")


def watched_search(tmp_path, monkeypatch, name, model, model_value, stored_from):
    monkeypatch.setattr(avito, "SEARCHES_DIR", tmp_path / "searches")
    monkeypatch.setattr(avito, "DATA_DIR", tmp_path / "data")
    avito.SEARCHES_DIR.mkdir(exist_ok=True)
    (avito.SEARCHES_DIR / f"{name}.json").write_text(json.dumps({
        "title": name, "profile": "goods", "catalog_url": MOSCOW, "new_only": False,
        "params": {"categoryId": "84", "locationId": "637640", "localPriority": "0",
                   "params[110617][0]": model_value, "params[110618][0]": "469735"},
    }), encoding="utf-8")
    search = avito.load_search(name)
    search.save_collected({str(i): iphone(i, model) for i in range(stored_from, stored_from + 12)}, {})
    return avito.Watch(search)


class FakeClient:
    proxy = None

    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def page(self, *, catalog_url, params, number):
        self.requests.append((params, number))
        items = self.pages.get(number, ())
        return avito_client.Page(number=number, items=tuple(items), total_count=100, items_on_page=50)


def test_searches_differing_only_in_the_model_are_read_with_one_request(tmp_path, monkeypatch):
    w13 = watched_search(tmp_path, monkeypatch, "iphone_13", "iPhone 13", "1642358", 0)
    w15 = watched_search(tmp_path, monkeypatch, "iphone_15", "iPhone 15", "18720777", 100)
    assert (w13.model, w15.model) == ("iPhone 13", "iPhone 15")
    [group] = avito.watch_groups([w13, w15])
    assert group.names == ["iphone_13", "iphone_15"]
    assert group.params["params[110617][0]"] == "1642358" and group.params["params[110617][1]"] == "18720777"
    assert group.params["params[110618][0]"] == "469735" and "localPriority" not in group.params

    alerts = []
    client = FakeClient({1: [iphone(1, "iPhone 13"), iphone(101, "iPhone 15")]})
    group.check(client, on_alert=lambda search, fields: alerts.append(search.name))  # first look: remember
    client.pages = {1: [iphone(500, "iPhone 15"), iphone(501, "iPhone 13"), iphone(502, "iPhone 14")]}
    group.check(client, on_alert=lambda search, fields: alerts.append((search.name, fields["id"])))
    assert alerts == [("iphone_13", 501), ("iphone_15", 500)]  # each to its own search; iPhone 14 to none
    assert len(client.requests) == 2  # one request per look for both searches


def test_a_page_full_of_new_listings_reads_the_next_one(tmp_path, monkeypatch):
    w13 = watched_search(tmp_path, monkeypatch, "iphone_13", "iPhone 13", "1642358", 0)
    w15 = watched_search(tmp_path, monkeypatch, "iphone_15", "iPhone 15", "18720777", 100)
    [group] = avito.watch_groups([w13, w15])
    w13.primed = w15.primed = True
    page_one = [iphone(600 + i, "iPhone 13") for i in range(avito_client.FULL_PAGE_SIZE)]
    client = FakeClient({1: page_one, 2: [iphone(700, "iPhone 15")]})
    alerts = []
    group.check(client, on_alert=lambda search, fields: alerts.append(fields["id"]))
    assert [number for _, number in client.requests] == [1, 2] and 700 in alerts and len(alerts) == 51

    client = FakeClient({1: [iphone(800, "iPhone 13")], 2: [iphone(801, "iPhone 15")]})
    group.check(client, on_alert=lambda search, fields: None)
    assert [number for _, number in client.requests] == [1]  # a short page holds everything new


def test_mixed_or_unknown_searches_stay_on_their_own(tmp_path, monkeypatch):
    w13 = watched_search(tmp_path, monkeypatch, "iphone_13", "iPhone 13", "1642358", 0)
    other = watched_search(tmp_path, monkeypatch, "iphone_13_again", "iPhone 13", "1642358", 200)  # same model
    groups = avito.watch_groups([w13, other])
    assert [g.names for g in groups] == [["iphone_13"], ["iphone_13_again"]]
