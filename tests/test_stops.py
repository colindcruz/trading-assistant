from ta import stops as S

KEY = "NFO:NIFTY26N0525000CE:MIS"


def opened(active="v1", price=200.0, qty=2600):
    book = {}
    ev = S.open_trade(book, KEY, "NIFTY26N0525000CE", "NIFTY", qty, price, active, "10:00:00")
    return book, ev


def kinds(events):
    return [e.kind for e in events]


def test_armed_at_first_fill_minus_1R():
    book, ev = opened()
    assert kinds(ev) == ["STOP_ARMED"] and "stop 180" in ev[0].message and "risk ₹52,000" in ev[0].message
    assert S.active_stop(book, KEY) == 180.0
    sensex = {}
    S.open_trade(sensex, "BFO:S:MIS", "S", "SENSEX", 1000, 300.0, "v1", "10:00:00")
    assert S.active_stop(sensex, "BFO:S:MIS") == 250.0


def test_breakeven_then_v1_trail_and_hit():
    book, _ = opened()
    assert S.on_price(book, KEY, 215.0, "10:01") == []                  # +0.75R: nothing
    ev = S.on_price(book, KEY, 220.0, "10:02")                          # +1R: breakeven
    assert kinds(ev) == ["STOP_MOVED"] and S.active_stop(book, KEY) == 200.0
    ev = S.on_price(book, KEY, 230.0, "10:03")                          # +1.5R: trail 1R below high = 210
    assert kinds(ev) == ["STOP_MOVED"] and S.active_stop(book, KEY) == 210.0
    assert S.on_price(book, KEY, 235.0, "10:04") == []                  # +5 only: silent (alert step is 10)
    assert S.active_stop(book, KEY) == 215.0
    ev = S.on_price(book, KEY, 240.0, "10:05")
    assert kinds(ev) == ["STOP_MOVED"] and S.active_stop(book, KEY) == 220.0
    assert S.on_price(book, KEY, 225.0, "10:06") == []                  # pullback: stop holds
    ev = S.on_price(book, KEY, 219.5, "10:07")
    hit = [e for e in ev if e.kind == "STOP_HIT"]
    assert hit and hit[0].price == 219.5 and hit[0].urgent
    assert book[KEY]["ladders"]["v1"]["hit_at"] == "10:07"


def test_wide_ladder_runs_alongside_and_holds_longer():
    book, _ = opened()
    for p in (220.0, 240.0, 260.0):
        S.on_price(book, KEY, p, "10:0x")
    lad = book[KEY]["ladders"]
    assert lad["v1"]["stop"] == 240.0 and lad["wide"]["stop"] == 230.0     # 1R vs 1.5R below 260
    ev = S.on_price(book, KEY, 235.0, "10:10")
    assert kinds(ev) == ["STOP_HIT"] and lad["wide"]["hit_at"] is None       # v1 out, wide still in
    ev = S.on_price(book, KEY, 229.0, "10:12")
    assert kinds(ev) == ["SHADOW_HIT"] and lad["wide"]["hit_price"] == 229.0


def test_active_ladder_is_a_setting():
    book, _ = opened(active="wide")
    for p in (220.0, 240.0, 260.0):
        S.on_price(book, KEY, p, "t")
    assert S.active_stop(book, KEY) == 230.0
    assert kinds(S.on_price(book, KEY, 235.0, "t")) == ["SHADOW_HIT"]       # v1 is the shadow now


def test_add_ratchets_to_new_average_minus_1R_and_never_lowers():
    book, _ = opened(qty=1300)
    ev = S.add_to_trade(book, KEY, 2600, 215.0)                              # added at 230
    assert kinds(ev) == ["ADD_DETECTED"] and S.active_stop(book, KEY) == 195.0
    assert "risk ₹52,000" in ev[0].message and ev[0].urgent
    S.add_to_trade(book, KEY, 3900, 200.0)                                   # averaging down
    assert S.active_stop(book, KEY) == 195.0                                 # does not move down
    # stages still count from the first fill (200): +1R at 220 moves nothing new (stop already 195 -> 200)
    S.on_price(book, KEY, 220.0, "t")
    assert S.active_stop(book, KEY) == 200.0


def test_gap_through_the_stop_hits_at_the_sampled_price():
    book, _ = opened()
    ev = S.on_price(book, KEY, 170.0, "t")
    assert kinds(ev) == ["STOP_HIT", "SHADOW_HIT"] and ev[0].price == 170.0


def test_reduce_and_close():
    book, _ = opened()
    S.reduce_trade(book, KEY, 1300)
    assert book[KEY]["qty"] == 1300
    t = S.close_trade(book, KEY)
    row = S.summary_row(KEY, t, "11:00", 230.0)
    assert KEY not in book and row["actual_exit"] == 230.0 and set(row["ladders"]) == {"v1", "wide"}
    assert S.on_price(book, KEY, 100.0, "t") == []


def test_compare_and_status():
    book, _ = opened()
    for p in (220.0, 260.0):
        S.on_price(book, KEY, p, "t")
    S.on_price(book, KEY, 239.0, "t")             # v1 (stop 240) hits at 239; wide (230) holds
    assert S.status_lines(book) == ["NIFTY26N0525000CE x2600 @ 200: stop 240 (trail HIT); wide 230"]
    row = S.summary_row(KEY, S.close_trade(book, KEY), "11:00", 250.0)
    line = S.compare([row])
    assert line == "Stops on 1 trades (gross): v1 ₹101,400, wide ₹130,000; actual ₹130,000"
    assert S.compare([]) == "Stops: no closed trades"
