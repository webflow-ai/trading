import datetime as dt

from api.index import compute_pcr, build_chain_rows, resample_candles, IST


def make_row(strike, expiry, ce_oi, ce_vol, pe_oi, pe_vol):
    return {
        "strikePrice": strike,
        "expiryDates": expiry,
        "CE": {"openInterest": ce_oi, "totalTradedVolume": ce_vol, "changeinOpenInterest": 0, "lastPrice": 100},
        "PE": {"openInterest": pe_oi, "totalTradedVolume": pe_vol, "changeinOpenInterest": 0, "lastPrice": 50},
    }


def make_oc(expiry, rows, spot=24500):
    return {"records": {"expiryDates": [expiry], "underlyingValue": spot, "data": rows}}


def test_compute_pcr_basic():
    expiry = "11-Aug-2026"
    rows = [
        make_row(24000, expiry, ce_oi=100, ce_vol=10, pe_oi=200, pe_vol=20),
        make_row(24500, expiry, ce_oi=300, ce_vol=30, pe_oi=150, pe_vol=15),
    ]
    result = compute_pcr(make_oc(expiry, rows))
    assert result["expiry"] == expiry
    assert result["callOi"] == 400
    assert result["putOi"] == 350
    assert result["pcrOi"] == round(350 / 400, 4)
    assert result["pcrVol"] == round(35 / 40, 4)


def test_compute_pcr_ignores_other_expiries():
    rows = [
        make_row(24000, "11-Aug-2026", 100, 10, 200, 20),
        make_row(24000, "18-Aug-2026", 999, 999, 999, 999),  # different expiry — must be excluded
    ]
    result = compute_pcr(make_oc("11-Aug-2026", rows), target_expiry="11-Aug-2026")
    assert result["callOi"] == 100
    assert result["putOi"] == 200


def test_compute_pcr_zero_call_oi_returns_none_not_divide_by_zero():
    rows = [make_row(24000, "11-Aug-2026", ce_oi=0, ce_vol=0, pe_oi=100, pe_vol=10)]
    result = compute_pcr(make_oc("11-Aug-2026", rows))
    assert result["pcrOi"] is None
    assert result["pcrVol"] is None


def test_build_chain_rows_picks_nearest_to_spot_and_sorts_ascending():
    expiry = "11-Aug-2026"
    rows = [make_row(s, expiry, 1, 1, 1, 1) for s in (23000, 24000, 24500, 25000, 26000)]
    result = build_chain_rows(make_oc(expiry, rows, spot=24500), top_n=3)
    assert [r["strike"] for r in result["rows"]] == [24000, 24500, 25000]
    assert result["spot"] == 24500


def test_build_chain_rows_row_shape():
    expiry = "11-Aug-2026"
    rows = [make_row(24500, expiry, ce_oi=111, ce_vol=222, pe_oi=333, pe_vol=444)]
    row = build_chain_rows(make_oc(expiry, rows, spot=24500))["rows"][0]
    assert row == {
        "strike": 24500, "ceOi": 111, "ceOiChg": 0, "ceVol": 222, "ceLtp": 100,
        "peOi": 333, "peOiChg": 0, "peVol": 444, "peLtp": 50,
    }


def test_resample_candles_groups_into_buckets_per_day():
    rows = [
        {"dt": dt.datetime(2026, 8, 6, 9, 15, tzinfo=IST), "open": 100, "high": 105, "low": 99, "close": 102},
        {"dt": dt.datetime(2026, 8, 6, 10, 0, tzinfo=IST), "open": 102, "high": 108, "low": 101, "close": 107},
        {"dt": dt.datetime(2026, 8, 6, 12, 30, tzinfo=IST), "open": 107, "high": 110, "low": 106, "close": 109},
    ]
    result = resample_candles(rows, bucket_hours=4)
    # 9:15 and 10:00 fall in the same 8-12 bucket; 12:30 starts a new 12-16 bucket
    assert len(result) == 2
    assert result[0]["open"] == 100 and result[0]["high"] == 108 and result[0]["low"] == 99 and result[0]["close"] == 107
    assert result[1]["open"] == 107


def test_resample_candles_does_not_merge_across_days():
    rows = [
        {"dt": dt.datetime(2026, 8, 6, 10, 0, tzinfo=IST), "open": 100, "high": 101, "low": 99, "close": 100},
        {"dt": dt.datetime(2026, 8, 7, 10, 0, tzinfo=IST), "open": 200, "high": 201, "low": 199, "close": 200},
    ]
    result = resample_candles(rows, bucket_hours=4)
    assert len(result) == 2
