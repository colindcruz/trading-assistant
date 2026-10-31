from datetime import date

from ta.exit_logic import build_instrument_index
from ta.watchlist import near_money_tokens


def chain(name, exchange, expiries, strikes):
    rows, tok = [], 1
    for exp in expiries:
        for k in strikes:
            for typ in ("CE", "PE"):
                rows.append({"name": name, "exchange": exchange, "tradingsymbol": f"{name}{exp}{k}{typ}",
                             "instrument_type": typ, "instrument_token": tok + (0 if name == "NIFTY" else 100_000),
                             "lot_size": 65, "tick_size": 0.05, "strike": k, "expiry": exp})
                tok += 1
    return rows


def test_strikes_around_spot_on_the_nearest_expiries():
    rows = chain("NIFTY", "NFO", ["2026-10-29", "2026-11-05", "2026-11-12", "2026-11-19"], range(24500, 25550, 50))
    rows += chain("SENSEX", "BFO", ["2026-11-06"], range(81000, 83100, 100))
    inst = build_instrument_index(rows)
    toks = near_money_tokens(inst, {"NIFTY": 25010.0}, date(2026, 11, 2), strikes_each_side=2, expiries=2)
    picked = {(m["expiry"], m["strike"], m["option_type"]) for m in inst.values() if m["instrument_token"] in toks}
    assert len(toks) == 2 * 5 * 2                          # 2 expiries x 5 strikes x CE/PE
    assert {e for e, _, _ in picked} == {"2026-11-05", "2026-11-12"}   # expired and third expiry left out
    assert {k for _, k, _ in picked} == {24900, 24950, 25000, 25050, 25100}
    assert all(m["index"] == "NIFTY" for m in inst.values() if m["instrument_token"] in toks)  # SENSEX: no spot
    both = near_money_tokens(inst, {"NIFTY": 25010.0, "SENSEX": 82040.0}, date(2026, 11, 2), 2, 2)
    assert len(both) == 20 + 1 * 5 * 2


def test_edge_of_the_chain_and_off_switch():
    inst = build_instrument_index(chain("NIFTY", "NFO", ["2026-11-05"], range(24500, 24700, 50)))
    assert len(near_money_tokens(inst, {"NIFTY": 24400.0}, date(2026, 11, 2), 10, 1)) == 4 * 2
    assert near_money_tokens(inst, {"NIFTY": 24600.0}, date(2026, 11, 2), 0, 2) == set()
