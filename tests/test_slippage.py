from ta.slippage import record, slip_points, summary


def test_points_are_positive_when_they_cost():
    assert slip_points("BUY", 101.0, 100.0) == 1.0      # paid above
    assert slip_points("SELL", 98.0, 100.0) == 2.0      # sold below
    assert slip_points("SELL", 101.0, 100.0) == -1.0    # a resting take-profit can do better


def test_record_and_summary():
    rs = [record("a", "NFO", "N1", "BUY", 2600, 101.0, 100.0, "ltp", 0.4),
          record("b", "NFO", "N1", "SELL", 2600, 117.0, 120.0, "bid_at_send"),
          record("c", "BFO", "S1", "BUY", 1000, 302.5, 300.0, "ltp", 1.2),
          record("d", "BFO", "S2", "BUY", 1000, 250.0, None, "ltp")]
    assert rs[0]["points"] == 1.0 and rs[0]["rupees"] == 2600.0 and rs[0]["ref_age_s"] == 0.4
    assert rs[1]["points"] == 3.0 and rs[1]["ref_kind"] == "bid_at_send"
    assert rs[3]["points"] is None and rs[3]["ref_kind"] == "none"
    line = summary(rs)
    assert "NIFTY 2 fills, avg +2.00 pts" in line and "SENSEX 1 fills, avg +2.50 pts" in line
    assert "1 fills had no reference price" in line
    assert summary([]) == "Slippage: no fills measured"
