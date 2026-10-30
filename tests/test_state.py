from datetime import date

from ta.state import Store, load_day_record, save_day_record, save_json, load_json


def test_store_defaults_and_persistence(tmp_path):
    s = Store(tmp_path)
    assert s.mode == "dryrun" and s.last_good_capital == 0 and s.telegram_offset == 0
    s.mode = "live"
    s.last_good_capital = 2_000_000
    s.telegram_offset = 42
    again = Store(tmp_path)
    assert again.mode == "live" and again.last_good_capital == 2_000_000 and again.telegram_offset == 42


def test_day_record_round_trip_and_date_check(tmp_path):
    d = date(2026, 11, 2)
    assert load_day_record(tmp_path, d) is None
    save_day_record(tmp_path, {"date": d.isoformat(), "peak": 5})
    assert load_day_record(tmp_path, d)["peak"] == 5
    assert load_day_record(tmp_path, date(2026, 11, 3)) is None


def test_corrupt_json_returns_default(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_json(p, "dflt") == "dflt"
    save_json(p, {"a": 1})
    assert load_json(p, None) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))
