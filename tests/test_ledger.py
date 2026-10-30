from datetime import timedelta

from ta.config import ChargeRates
from ta.ledger import Fill, Ledger, charges_for_fill, fill_from_order_update, fill_from_trade
from helpers import ist

T0 = ist(2026, 11, 2, 9, 30)


def f(i, side, qty, price, sym="NIFTY26N0525000CE", ex="NFO", order=None, minutes=0):
    return Fill(fill_id=f"t{i}", order_id=order or f"o{i}", exchange=ex, tradingsymbol=sym, product="MIS",
                side=side, quantity=qty, price=price, ts=T0 + timedelta(minutes=minutes))


def test_round_trip_books_pnl_and_counts_one_trade():
    lg = Ledger(ChargeRates())
    ev1 = lg.apply(f(1, "BUY", 650, 100))
    ev2 = lg.apply(f(2, "SELL", 650, 110, minutes=5))
    assert [e.kind for e in ev1] == ["TRADE_OPENED"] and ev1[0].trade_no == 1
    assert [e.kind for e in ev2] == ["TRADE_CLOSED"] and ev2[0].trade_no == 1
    assert lg.booked == 6500
    assert lg.trades_opened == 1 and lg.trades_closed == 1
    assert lg.quantities() == {}
    assert lg.brokerage == 40  # two orders


def test_adds_and_partial_exits_stay_one_trade():
    lg = Ledger(ChargeRates())
    lg.apply(f(1, "BUY", 650, 100))
    ev = lg.apply(f(2, "BUY", 325, 112, minutes=1))
    assert ev[0].kind == "POSITION_INCREASED"
    c = lg.contracts["NFO:NIFTY26N0525000CE:MIS"]
    assert c.qty == 975 and abs(c.avg_cost - 104.0) < 1e-9
    ev = lg.apply(f(3, "SELL", 325, 120, minutes=2))
    assert ev[0].kind == "POSITION_REDUCED"
    assert abs(lg.booked - 325 * 16) < 1e-6
    ev = lg.apply(f(4, "SELL", 650, 100, minutes=3))
    assert ev[0].kind == "TRADE_CLOSED" and ev[0].trade_no == 1
    assert lg.trades_opened == 1
    assert abs(lg.booked - (325 * 16 - 650 * 4)) < 1e-6


def test_second_contract_is_second_trade_and_reopen_is_third():
    lg = Ledger(ChargeRates())
    lg.apply(f(1, "BUY", 65, 100))
    lg.apply(f(2, "BUY", 20, 400, sym="SENSEX26N0682000CE", ex="BFO"))
    lg.apply(f(3, "SELL", 65, 90, minutes=1))
    ev = lg.apply(f(4, "BUY", 65, 95, minutes=2))
    assert ev[0].kind == "TRADE_OPENED" and ev[0].trade_no == 3
    assert lg.trades_opened == 3 and lg.trades_closed == 1


def test_open_pnl_marks_to_ltp_and_cost_when_unknown():
    lg = Ledger(ChargeRates())
    lg.apply(f(1, "BUY", 650, 100))
    assert lg.open_pnl({"NFO:NIFTY26N0525000CE": 103.0}) == 650 * 3
    assert lg.open_pnl({}) == 0


def test_duplicate_fill_ids_are_ignored():
    lg = Ledger(ChargeRates())
    lg.apply(f(1, "BUY", 65, 100))
    assert lg.apply(f(1, "BUY", 65, 100)) == []
    assert lg.contracts["NFO:NIFTY26N0525000CE:MIS"].qty == 65


def test_charges_match_october_effective_rate():
    """A NIFTY round trip at equal buy and sell value should cost about 0.1186% of
    turnover before brokerage (spec research item 9)."""
    rates = ChargeRates()
    buy = f(1, "BUY", 6500, 100)
    sell = f(2, "SELL", 6500, 100)
    total = charges_for_fill(buy, rates) + charges_for_fill(sell, rates)
    turnover = buy.value + sell.value
    assert abs(total / turnover * 100 - 0.1186) < 0.002
    lg = Ledger(rates)
    lg.apply(buy)
    lg.apply(sell)
    # with brokerage + GST on it the net is below zero for a flat round trip
    assert lg.net_pnl({}) < 0
    assert abs(lg.total_charges - (total + 40 * 1.18)) < 1e-6


def test_bfo_uses_bse_exchange_rate():
    rates = ChargeRates()
    nfo = charges_for_fill(f(1, "BUY", 100, 100), rates)
    bfo = charges_for_fill(f(2, "BUY", 100, 100, sym="SENSEX26N0682000CE", ex="BFO"), rates)
    assert bfo < nfo


def test_fill_from_order_update_deltas():
    base = {"order_id": "9", "exchange": "NFO", "tradingsymbol": "NIFTY26N0525000CE", "product": "MIS",
            "transaction_type": "BUY", "average_price": 100.5}
    now = T0
    first = fill_from_order_update({**base, "filled_quantity": 130, "status": "OPEN"}, 0, now)
    assert first.quantity == 130 and first.fill_id == "9:130"
    second = fill_from_order_update({**base, "filled_quantity": 195, "status": "COMPLETE"}, 130, now)
    assert second.quantity == 65 and second.fill_id == "9:195"
    assert fill_from_order_update({**base, "filled_quantity": 195}, 195, now) is None


def test_fill_from_trade_row():
    t = {"trade_id": "55", "order_id": "9", "exchange": "BFO", "tradingsymbol": "SENSEX26N0682000CE", "product": "NRML",
         "transaction_type": "SELL", "quantity": 20, "average_price": 410.25, "fill_timestamp": "2026-11-02 10:15:00"}
    fl = fill_from_trade(t)
    assert fl.key == "BFO:SENSEX26N0682000CE:NRML" and fl.value == 20 * 410.25
