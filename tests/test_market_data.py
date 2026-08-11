import asyncio
import datetime as dt
import json

import httpx

import market_data
from market_data import IST


class FakeResponse:
    def __init__(self, json_data=None, text="", raise_exc=None):
        self._json = json_data
        self.text = text
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response

    async def aclose(self):
        pass


def test_fetch_ohlc_candles_parses_rows_and_skips_gaps():
    ts1 = int(dt.datetime(2026, 8, 10, 9, 15, tzinfo=IST).timestamp())
    ts2 = int(dt.datetime(2026, 8, 10, 9, 30, tzinfo=IST).timestamp())
    json_data = {
        "chart": {
            "result": [{
                "timestamp": [ts1, ts2],
                "indicators": {"quote": [{
                    "open": [100.0, None],  # second row has a gap and must be skipped
                    "high": [105.0, 106.0],
                    "low": [99.0, 100.0],
                    "close": [104.0, 105.0],
                }]},
            }],
        },
    }
    client = FakeAsyncClient(FakeResponse(json_data=json_data))

    rows = asyncio.run(market_data.fetch_ohlc_candles(client, "^NSEI", "15m", "1d"))

    assert len(rows) == 1
    assert rows[0]["open"] == 100.0
    assert rows[0]["dt"] == dt.datetime(2026, 8, 10, 9, 15, tzinfo=IST)


def test_fetch_ohlc_candles_returns_empty_list_when_no_result():
    client = FakeAsyncClient(FakeResponse(json_data={"chart": {"result": None}}))
    assert asyncio.run(market_data.fetch_ohlc_candles(client, "^NSEI", "1d", "5d")) == []


def test_fetch_quote_computes_pct_change():
    json_data = {"chart": {"result": [{"meta": {"regularMarketPrice": 42500.5, "previousClose": 42000.0}}]}}
    client = FakeAsyncClient(FakeResponse(json_data=json_data))

    result = asyncio.run(market_data.fetch_quote(client, "^DJI"))

    assert result["price"] == 42500.5
    assert result["previous_close"] == 42000.0
    assert result["pct_change"] == round((42500.5 - 42000.0) / 42000.0 * 100, 4)


def test_fetch_quote_returns_none_when_previous_close_missing():
    json_data = {"chart": {"result": [{"meta": {"regularMarketPrice": 100.0}}]}}
    client = FakeAsyncClient(FakeResponse(json_data=json_data))
    assert asyncio.run(market_data.fetch_quote(client, "^DJI")) is None


def test_fetch_quote_returns_none_on_http_error():
    client = FakeAsyncClient(FakeResponse(raise_exc=httpx.HTTPError("boom")))
    assert asyncio.run(market_data.fetch_quote(client, "^DJI")) is None


def test_fetch_quotes_returns_an_entry_per_symbol_even_on_failure(monkeypatch):
    async def fake_fetch_quote(client, symbol):
        return None if symbol == "BAD" else {"price": 1.0, "previous_close": 1.0, "pct_change": 0.0}

    monkeypatch.setattr(market_data, "fetch_quote", fake_fetch_quote)

    result = asyncio.run(market_data.fetch_quotes(["^DJI", "BAD"]))

    assert result["^DJI"]["price"] == 1.0
    assert result["BAD"] is None


def _next_data_html(gift_data: dict) -> str:
    payload = json.dumps({"props": {"pageProps": {"initialGiftData": gift_data}}})
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{payload}</script></body></html>'


def test_parse_gift_nifty_html_extracts_price_and_change():
    html = _next_data_html({"last_trade_price": 24700.1, "change_value": -84.5})
    assert market_data._parse_gift_nifty_html(html) == {"price": 24700.1, "change": -84.5}


def test_parse_gift_nifty_html_returns_none_when_no_next_data_tag():
    assert market_data._parse_gift_nifty_html("<html><body>no data here</body></html>") is None


def test_parse_gift_nifty_html_returns_none_when_price_field_missing():
    html = _next_data_html({"change_value": -84.5})  # no last_trade_price
    assert market_data._parse_gift_nifty_html(html) is None


def test_fetch_gift_nifty_returns_parsed_dict_on_success():
    html = _next_data_html({"last_trade_price": 24700.1, "change_value": 12.5})
    client = FakeAsyncClient(FakeResponse(text=html))
    assert asyncio.run(market_data.fetch_gift_nifty(client)) == {"price": 24700.1, "change": 12.5}


def test_fetch_gift_nifty_returns_none_on_http_error():
    client = FakeAsyncClient(FakeResponse(raise_exc=httpx.HTTPError("boom")))
    assert asyncio.run(market_data.fetch_gift_nifty(client)) is None


def test_fetch_gift_nifty_returns_none_when_markup_does_not_match():
    client = FakeAsyncClient(FakeResponse(text="<html>nothing here</html>"))
    assert asyncio.run(market_data.fetch_gift_nifty(client)) is None
