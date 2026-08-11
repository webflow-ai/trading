"""
nse_client.py — NSE session-based fetcher for participant-wise open interest,
FII/DII cash activity, and the F&O ban list.

Module 1 of the Nifty Pre-Market Analysis Engine addition — see
docs/PREMARKET_ENGINE.md for the full spec and how this fits alongside the
existing PCR tracker (backend.py / api/index.py).

This is a *separate* client from the option-chain one in backend.py rather
than a shared one: it hits different NSE hosts (nsearchives.nseindia.com for
the CSV archives vs. www.nseindia.com/api for the option chain) with
different response shapes, and keeping them independent means an outage or
format change in one never touches the other. Both use the same
cookie-priming trick because both are NSE.

Verified live 2026-08-11: fetch_participant_oi and fetch_fii_dii_cash work
as originally written. fetch_ban_list's URL/parser were both wrong on the
first guess (NSE serves the ban list at a fixed, non-dated URL with no CSV
header row — see BAN_LIST_URL and _parse_ban_list_csv) and have been fixed
against the real response.
"""

import time
import datetime as dt
from io import StringIO

import httpx
import pandas as pd

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

BASE = "https://www.nseindia.com"
PRIME_URL = BASE + "/option-chain"
FII_DII_URL = BASE + "/api/fiidiiTradeReact"

ARCHIVES = "https://nsearchives.nseindia.com/content/nsccl"
PARTICIPANT_OI_URL = ARCHIVES + "/fao_participant_oi_{ddmmyyyy}.csv"
# Verified live: unlike participant OI, NSE does not archive the ban list by
# date — this fixed URL always serves the current trade date's list.
BAN_LIST_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": PRIME_URL,
    "Connection": "keep-alive",
}

MIN_REQUEST_INTERVAL_SECONDS = 2.0
DEFAULT_LOOKBACK_DAYS = 5

PARTICIPANT_ROWS = {"Client", "DII", "FII", "Pro"}

# NSE trading holidays for 2026 — TODO: verify/complete against NSE's official
# holiday circular (published each December for the following year). Only
# fixed-date national holidays are filled in below; festival holidays (Holi,
# Diwali, Eid, etc.) shift every year and are deliberately left out rather
# than guessed. This list is an optimization only — fetch_participant_oi and
# fetch_ban_list walk back on a 404 regardless, so an incomplete list costs
# one extra request per missing holiday, not incorrect data.
NSE_HOLIDAYS_2026 = {
    dt.date(2026, 1, 26),   # Republic Day
    dt.date(2026, 8, 15),   # Independence Day
    dt.date(2026, 10, 2),   # Gandhi Jayanti
}


def _ddmmyyyy(date: dt.date) -> str:
    return date.strftime("%d%m%Y")


def _trading_day_candidates(date: dt.date, max_candidates: int, holidays: set[dt.date]) -> list[dt.date]:
    """Dates to try, walking backward from `date`, skipping weekends and
    known holidays, until `max_candidates` trading days have been collected."""
    candidates: list[dt.date] = []
    d = date
    while len(candidates) < max_candidates:
        if d.weekday() < 5 and d not in holidays:
            candidates.append(d)
        d -= dt.timedelta(days=1)
    return candidates


def _parse_participant_oi_csv(text: str, as_of: dt.date) -> pd.DataFrame:
    # First line is a title row ("Participant wise Open Interest for FUTURE
    # and OPTION Contracts as on DD-Mon-YYYY,,,,..."); the real header is
    # line 2.
    df = pd.read_csv(StringIO(text), skiprows=1)
    df.columns = [c.strip() for c in df.columns]
    df = df[df["Client Type"].isin(PARTICIPANT_ROWS)].reset_index(drop=True)
    df.insert(0, "date", as_of.isoformat())
    return df


def _parse_ban_list_csv(text: str) -> list[str]:
    # Verified live: first line is a title ("Securities in Ban For Trade
    # Date DD-MON-YYYY:"), not a CSV header — there is no header row at all,
    # just "<srno>,<symbol>" rows below it.
    df = pd.read_csv(StringIO(text), skiprows=1, header=None, names=["srno", "symbol"])
    return [s.strip() for s in df["symbol"].dropna().astype(str) if s.strip()]


class NSEClient:
    def __init__(self):
        self._client = httpx.Client(headers=HEADERS, timeout=15.0, follow_redirects=True)
        self._last_request_at = 0.0
        self._primed = False

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _throttle(self):
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _prime(self):
        self._client.get(PRIME_URL)
        self._primed = True

    def _get(self, url: str, attempts: int = 3) -> httpx.Response:
        if not self._primed:
            self._prime()
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                resp = self._client.get(url)
                self._last_request_at = time.monotonic()
                if resp.status_code in (401, 403):
                    print(f"nse_client: got {resp.status_code} for {url}, re-priming session")
                    self._prime()
                    continue
                if resp.status_code == 404:
                    return resp  # caller walks back to the previous trading day
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as e:
                last_exc = e
                backoff = 2 ** (attempt - 1)
                print(f"nse_client: request to {url} failed ({e}), retrying in {backoff}s "
                      f"(attempt {attempt}/{attempts})")
                time.sleep(backoff)
        raise last_exc

    def fetch_participant_oi(self, date: dt.date, max_lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> pd.DataFrame:
        tried = []
        for d in _trading_day_candidates(date, max_lookback_days + 1, NSE_HOLIDAYS_2026):
            tried.append(d)
            url = PARTICIPANT_OI_URL.format(ddmmyyyy=_ddmmyyyy(d))
            resp = self._get(url)
            if resp.status_code == 200 and resp.text.strip():
                return _parse_participant_oi_csv(resp.text, as_of=d)
        raise FileNotFoundError(f"no participant OI file found for any of {tried}")

    def fetch_ban_list(self, date: dt.date | None = None) -> list[str]:
        """`date` is accepted only for call-site consistency with
        fetch_participant_oi — NSE has no per-date archive for the ban list
        (unlike participant OI), only a fixed URL that always reflects the
        current trade date, so there's nothing to walk back through."""
        resp = self._get(BAN_LIST_URL)
        if resp.status_code == 200 and resp.text.strip():
            return _parse_ban_list_csv(resp.text)
        return []

    def fetch_fii_dii_cash(self) -> dict:
        resp = self._get(FII_DII_URL)
        rows = resp.json()
        result: dict = {}
        for row in rows:
            category = str(row.get("category", "")).upper()
            key = "fii" if category.startswith("FII") else "dii" if category.startswith("DII") else None
            if key is None:
                continue
            result[f"{key}_buy"] = float(row["buyValue"])
            result[f"{key}_sell"] = float(row["sellValue"])
            result["date"] = row.get("date", result.get("date"))
        return result


if __name__ == "__main__":
    # Manual smoke test — run `python nse_client.py` from a normal machine
    # (not a cloud/datacenter IP) to sanity-check against live NSE data.
    today = dt.datetime.now(IST).date()
    with NSEClient() as client:
        print("participant OI:")
        print(client.fetch_participant_oi(today))
        print("\nban list:", client.fetch_ban_list(today))
        print("\nFII/DII cash:", client.fetch_fii_dii_cash())
