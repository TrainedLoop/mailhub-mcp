from mailhub_mcp.core.overview import build_overview


def _s(frm, unread=False, newsletter=False, category=None):
    return {"from": frm, "unread": unread, "is_newsletter": newsletter, "category": category}


def test_overview_counts_and_top_senders():
    s = [
        _s("Shop <News@Shop.com>", unread=True, newsletter=True, category="promotions"),
        _s("Shop <news@shop.com>", newsletter=True, category="promotions"),
        _s("Shop <news@shop.com>", category="promotions"),
        _s("bob@x.com"),
    ]
    o = build_overview(s)
    assert o["total"] == 4 and o["unread"] == 1 and o["newsletters"] == 2
    assert o["top_senders"][0] == {"email": "news@shop.com", "name": "Shop", "count": 3, "unread": 1, "newsletters": 2, "unsubscribe": None}
    assert o["top_senders"][1]["email"] == "bob@x.com"
    assert o["categories"] == {"promotions": 3, "none": 1}


def test_overview_caps_top_n():
    s = [_s(f"u{i:02d}@x.com") for i in range(40)]
    o = build_overview(s)
    assert len(o["top_senders"]) == 30
    assert o["top_senders"][0]["email"] == "u00@x.com"
    assert len(build_overview(s, top_n=5)["top_senders"]) == 5


def test_overview_unsubscribe_from_most_recent_message_with_one():
    old = {**_s("Shop <n@shop.com>"), "internal_date": 1, "unsubscribe": {"url": "https://shop.com/old", "mailto": None, "one_click": False}}
    new = {**_s("Shop <n@shop.com>"), "internal_date": 9, "unsubscribe": {"url": "https://shop.com/new", "mailto": None, "one_click": True}}
    newest_without = {**_s("Shop <n@shop.com>"), "internal_date": 20, "unsubscribe": None}
    o = build_overview([new, old, newest_without])
    assert o["top_senders"][0]["unsubscribe"]["url"] == "https://shop.com/new"
    assert build_overview([_s("a@b.com")])["top_senders"][0]["unsubscribe"] is None
