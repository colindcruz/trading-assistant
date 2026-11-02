from ta.telegram import Notifier, Telegram, classify_change, parse_command
from helpers import Clock


def test_parse_command():
    assert parse_command("/set loss_limit 150000 force") == ("set", ["loss_limit", "150000", "force"])
    assert parse_command("/STATUS@my_bot") == ("status", [])
    assert parse_command("hello") == ("", [])
    assert parse_command("  /kill ") == ("kill", [])


def test_classify_change_directions():
    assert classify_change("loss_limit", 200_000, 250_000) == "loosen"
    assert classify_change("loss_limit", 200_000, 150_000) == "tighten"
    assert classify_change("max_trades", 4, 5) == "loosen"
    assert classify_change("max_trades", 4, 3) == "tighten"
    assert classify_change("lock_floor", 133_400, 100_000) == "loosen"
    assert classify_change("lock_floor", 133_400, 150_000) == "tighten"
    assert classify_change("cooldown_minutes", 3, 1) == "loosen"
    assert classify_change("cooldown_minutes", 3, 10) == "tighten"
    assert classify_change("trail_distance", 68_000, 90_000) == "loosen"
    assert classify_change("reentry_floor_step", 50_000, 25_000) == "tighten"
    assert classify_change("lock_arm", 200_000, 200_000) == "same"
    assert classify_change("unknown_thing", 1, 2) == "loosen"


def test_max_trades_zero_means_no_cap():
    assert classify_change("max_trades", 0, 4) == "tighten"    # adding a cap
    assert classify_change("max_trades", 4, 0) == "loosen"     # removing it
    assert classify_change("max_trades", 0, 0) == "same"


class FakeResp:
    def __init__(self, payload, ok=True):
        self.payload, self.ok, self.status_code, self.text = payload, ok, 200 if ok else 500, ""

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self, updates):
        self.updates = updates
        self.posts = []

    def get(self, url, params=None, timeout=None):
        return FakeResp({"ok": True, "result": self.updates})

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, data))
        return FakeResp({"ok": True})


def upd(uid, text, from_id="7", chat_id="100"):
    return {"update_id": uid, "message": {"text": text, "from": {"id": int(from_id)}, "chat": {"id": int(chat_id)}}}


def test_fetch_commands_filters_by_user_and_chat_and_advances_offset():
    http = FakeHttp([upd(10, "/status"), upd(11, "/kill", from_id="99"), upd(12, "/live", chat_id="555"), upd(13, "/set")])
    tg = Telegram("bot", "100", "7", http=http)
    cmds, offset = tg.fetch_commands(5)
    assert [c.text for c in cmds] == ["/status", "/set"]
    assert offset == 14


def test_fetch_commands_never_raises():
    class Boom:
        def get(self, *a, **k):
            raise ConnectionError("down")

    tg = Telegram("bot", "100", "7", http=Boom())
    assert tg.fetch_commands(3) == ([], 3)


def test_notifier_mute_urgent_and_repeat_guard():
    http = FakeHttp([])
    tg = Telegram("bot", "100", "7", "topic", http=http)
    muted = {"on": False}
    clock = Clock()
    n = Notifier(tg, is_muted=lambda: muted["on"], clock=clock, repeat_seconds=60)
    n.alert("hello")
    assert len(http.posts) == 1
    muted["on"] = True
    n.alert("quiet")
    assert len(http.posts) == 1
    n.alert("loud", urgent=True)
    assert len(http.posts) == 3            # telegram + ntfy
    assert http.posts[1][1]["text"].startswith("\U0001f6a8 URGENT")
    muted["on"] = False
    n.alert("same", kind="k")
    n.alert("same", kind="k")
    assert len(http.posts) == 4
    clock.t += 61
    n.alert("same", kind="k")
    assert len(http.posts) == 5


def test_alert_repeat_can_be_set_per_call():
    http = FakeHttp([])
    tg = Telegram("bot", "100", "7", "", http=http)
    clock = Clock()
    n = Notifier(tg, is_muted=lambda: False, clock=clock, repeat_seconds=60)
    n.alert("dead token", urgent=True, kind="session", repeat_seconds=900)
    clock.t += 61
    n.alert("dead token", urgent=True, kind="session", repeat_seconds=900)
    assert len(http.posts) == 1                      # the 60 s default does not apply
    clock.t += 840
    n.alert("dead token", urgent=True, kind="session", repeat_seconds=900)
    assert len(http.posts) == 2
