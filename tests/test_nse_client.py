import datetime as dt
import os

import pytest

from nse_client import (
    NSEClient,
    _ddmmyyyy,
    _parse_participant_oi_csv,
    _trading_day_candidates,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def test_ddmmyyyy_formats_with_leading_zeros():
    assert _ddmmyyyy(dt.date(2026, 8, 10)) == "10082026"
    assert _ddmmyyyy(dt.date(2026, 1, 5)) == "05012026"


def test_trading_day_candidates_skips_weekends():
    # 10 Aug 2026 is a Monday — walking back should land on Fri 8/7 and
    # Thu 8/6, skipping Sat 8/8 and Sun 8/9 entirely.
    candidates = _trading_day_candidates(dt.date(2026, 8, 10), max_candidates=3, holidays=set())
    assert candidates == [dt.date(2026, 8, 10), dt.date(2026, 8, 7), dt.date(2026, 8, 6)]


def test_trading_day_candidates_skips_holidays():
    holidays = {dt.date(2026, 8, 10)}
    candidates = _trading_day_candidates(dt.date(2026, 8, 10), max_candidates=2, holidays=holidays)
    assert candidates == [dt.date(2026, 8, 7), dt.date(2026, 8, 6)]


def test_trading_day_candidates_walks_back_through_a_holiday_cluster():
    # simulate a long weekend: Fri 8/7 is also a holiday
    holidays = {dt.date(2026, 8, 7)}
    candidates = _trading_day_candidates(dt.date(2026, 8, 10), max_candidates=2, holidays=holidays)
    assert candidates == [dt.date(2026, 8, 10), dt.date(2026, 8, 6)]


def test_parse_participant_oi_csv_keeps_only_known_participants_and_stamps_date():
    with open(os.path.join(FIXTURES, "participant_oi_sample.csv")) as f:
        text = f.read()
    df = _parse_participant_oi_csv(text, as_of=dt.date(2026, 8, 10))
    assert list(df["Client Type"]) == ["Client", "DII", "FII", "Pro"]
    assert (df["date"] == "2026-08-10").all()
    fii_row = df[df["Client Type"] == "FII"].iloc[0]
    assert int(fii_row["Future Index Long"]) == 300000
    assert int(fii_row["Future Index Short"]) == 250000


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _client_without_network():
    # Bypasses __init__ so no real httpx.Client / cookie-priming request is
    # created — these tests only exercise the walk-back logic around _get.
    return NSEClient.__new__(NSEClient)


def test_fetch_participant_oi_walks_back_to_the_previous_trading_day_on_404(monkeypatch):
    with open(os.path.join(FIXTURES, "participant_oi_sample.csv")) as f:
        good_csv = f.read()

    requested_urls = []

    def fake_get(url, attempts=3):
        requested_urls.append(url)
        if url.endswith("10082026.csv"):
            return _FakeResponse(404)
        return _FakeResponse(200, good_csv)

    client = _client_without_network()
    monkeypatch.setattr(client, "_get", fake_get)

    df = client.fetch_participant_oi(dt.date(2026, 8, 10))

    assert len(requested_urls) == 2
    assert requested_urls[0].endswith("10082026.csv")
    assert requested_urls[1].endswith("07082026.csv")  # previous trading day (Friday)
    assert not df.empty


def test_fetch_participant_oi_raises_after_exhausting_lookback_window(monkeypatch):
    def fake_get(url, attempts=3):
        return _FakeResponse(404)

    client = _client_without_network()
    monkeypatch.setattr(client, "_get", fake_get)

    with pytest.raises(FileNotFoundError):
        client.fetch_participant_oi(dt.date(2026, 8, 10), max_lookback_days=2)


def test_fetch_ban_list_fetches_the_fixed_current_day_url(monkeypatch):
    requested_urls = []

    def fake_get(url, attempts=3):
        requested_urls.append(url)
        return _FakeResponse(200, "Securities in Ban For Trade Date 10-AUG-2026:\n1,RELIANCE\n2,ZEEL\n")

    client = _client_without_network()
    monkeypatch.setattr(client, "_get", fake_get)

    symbols = client.fetch_ban_list()

    assert requested_urls == ["https://nsearchives.nseindia.com/content/fo/fo_secban.csv"]
    assert symbols == ["RELIANCE", "ZEEL"]


def test_fetch_ban_list_returns_empty_list_on_404(monkeypatch):
    def fake_get(url, attempts=3):
        return _FakeResponse(404)

    client = _client_without_network()
    monkeypatch.setattr(client, "_get", fake_get)

    assert client.fetch_ban_list() == []


def test_fetch_fii_dii_cash_maps_categories_to_buy_sell_keys(monkeypatch):
    class FakeJsonResponse:
        status_code = 200

        def json(self):
            return [
                {"category": "FII/FPI *", "buyValue": "12345.67", "sellValue": "11000.50", "date": "10-Aug-2026"},
                {"category": "DII **", "buyValue": "9000.00", "sellValue": "9500.25", "date": "10-Aug-2026"},
            ]

    client = _client_without_network()
    monkeypatch.setattr(client, "_get", lambda url, attempts=3: FakeJsonResponse())

    result = client.fetch_fii_dii_cash()

    assert result["fii_buy"] == 12345.67
    assert result["fii_sell"] == 11000.50
    assert result["dii_buy"] == 9000.00
    assert result["dii_sell"] == 9500.25
    assert result["date"] == "10-Aug-2026"
