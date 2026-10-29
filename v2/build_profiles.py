"""v2 sandbox: company profile data for the NATI widget.

Writes v2/site/data/companies/<ID>.json for each company in v2/config/profile_aliases.csv
(or the ids given on the command line). Reads the v1 build in v2/site/data (constituents,
index snapshot) and never touches the v1 pipeline or its data.

  price chart   Yahoo Finance daily history since listing + the last 5 sessions of 5-minute bars,
                cut at the snapshot's as_of so the chart agrees with the widget's quote
  financials    Yahoo Finance consolidated statements, in INR crore (EPS in rupees)
  news          inc42.com WordPress API; only posts whose headline names the company or one
                of its brands (aliases), newest first

Usage:  .venv/bin/python v2/build_profiles.py [ETERNAL_NS ...]               full build (weekly)
        .venv/bin/python v2/build_profiles.py --news-only [ETERNAL_NS ...]   stories only
        .venv/bin/python v2/build_profiles.py --prices-only                  quotes and bars (every refresh)
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

V2 = Path(__file__).resolve().parent
DATA = V2 / "site" / "data"
OUT = DATA / "companies"
ALIASES = V2 / "config" / "profile_aliases.csv"
IST = "Asia/Kolkata"
CR = 1e7
NEWS_MAX = 8
UA = {"User-Agent": "Mozilla/5.0 (Inc42 Datalabs NATI profile builder)"}


def log(msg: str) -> None:
    print(msg, flush=True)


def epoch_ist(ts: pd.Timestamp) -> int:
    """Seconds since epoch of the IST wall-clock time read as UTC (same as the index sessions)."""
    naive = ts.tz_convert(IST).tz_localize(None)
    return int((naive - pd.Timestamp("1970-01-01")).total_seconds())


def crore(v) -> float | None:
    return None if v is None or pd.isna(v) else round(float(v) / CR, 1)


def num(v, nd=2) -> float | None:
    return None if v is None or pd.isna(v) else round(float(v), nd)


# ---------------------------------------------------------------- prices
def end_on_quote(daily: list, session_date: str, last: float) -> None:
    """The session ends on the index build's quote, not on whatever the source has since."""
    if daily and daily[-1][0] == session_date:
        row = daily[-1]
        row[4] = last
        row[2], row[3] = max(row[2], last), min(row[3], last)


def sessions_from_bars(m: pd.DataFrame, daily: list, as_of: pd.Timestamp) -> list:
    """The last five sessions of 5-minute bars (IST), cut at as_of, as [{date, prev_close, points}];
    each session starts on its open and ends on that day's close in `daily`."""
    m = m[m.index + pd.Timedelta(minutes=5) <= as_of]
    closes = {r[0]: r[4] for r in daily}
    dates = [r[0] for r in daily]
    sessions = []
    for day, bars in m.groupby(m.index.normalize()):
        ds = day.strftime("%Y-%m-%d")
        if ds not in closes or bars.empty:
            continue
        k = dates.index(ds)
        start = day + pd.Timedelta(hours=9, minutes=15)
        pts = [[epoch_ist(start), num(bars.Open.iloc[0])]]
        pts += [[epoch_ist(ts + pd.Timedelta(minutes=5)), num(v)] for ts, v in bars.Close.items() if pd.notna(v)]
        pts[-1][1] = closes[ds]
        sessions.append({"date": ds, "prev_close": daily[k - 1][4] if k else None, "points": pts})
    return sessions[-5:]


def daily_rows(d: pd.DataFrame, session_date: str) -> list:
    d = d[d.index <= pd.Timestamp(session_date)]
    return [[i.strftime("%Y-%m-%d"), num(r.Open), num(r.High), num(r.Low), num(r.Close), int(r.Volume or 0)]
            for i, r in d.iterrows() if pd.notna(r.Close)]


def fill_from_bars(daily: list, m: pd.DataFrame | None, session_date: str) -> list:
    """Daily rows for sessions the source's daily data skipped but its 5-minute bars cover (Yahoo
    sometimes drops a day's daily bar for some stocks): OHLC and volume from that day's bars."""
    if m is None or m.empty:
        return daily
    have = {r[0] for r in daily}
    extra = []
    for day, bars in m.groupby(m.index.normalize()):
        ds = day.strftime("%Y-%m-%d")
        bars = bars.dropna(subset=["Close"])
        if ds in have or ds > session_date or bars.empty or (daily and ds < daily[0][0]):
            continue
        extra.append([ds, num(bars.Open.iloc[0]), num(bars.High.max()), num(bars.Low.min()), num(bars.Close.iloc[-1]),
                      int(bars.Volume.fillna(0).sum())])
    return sorted(daily + extra, key=lambda r: r[0]) if extra else daily


def price_history(t: yf.Ticker, as_of: pd.Timestamp, session_date: str, last: float) -> tuple[list, list]:
    d = t.history(period="max", interval="1d", auto_adjust=False)
    d.index = d.index.tz_convert(IST).normalize().tz_localize(None)
    m = t.history(period="5d", interval="5m", auto_adjust=False).tz_convert(IST)
    daily = fill_from_bars(daily_rows(d, session_date), m, session_date)
    end_on_quote(daily, session_date, last)
    return daily, sessions_from_bars(m, daily, as_of)


def week52(daily: list, session_date: str) -> dict:
    closes = pd.Series({r[0]: r[4] for r in daily})
    yr = closes[closes.index >= (pd.Timestamp(session_date) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")]
    return {"wk52_high": num(yr.max()), "wk52_high_date": yr.idxmax(), "wk52_low": num(yr.min()), "wk52_low_date": yr.idxmin()}


# ---------------------------------------------------------------- financials
def _row(df: pd.DataFrame, *names):
    for n in names:
        if n in df.index:
            return df.loc[n]
    return pd.Series(index=df.columns, dtype=float)


def fy_label(d: pd.Timestamp) -> str:
    """Indian fiscal year ending March: 2026-03-31 → FY26."""
    return f"FY{(d.year + (1 if d.month > 3 else 0)) % 100:02d}"


def q_label(d: pd.Timestamp) -> str:
    q = {6: 1, 9: 2, 12: 3, 3: 4}[d.month]
    return f"Q{q} {fy_label(d)}"


def statement_rows(df: pd.DataFrame, labeller) -> list[dict]:
    rev = _row(df, "Total Revenue", "Operating Revenue")
    ebitda = _row(df, "EBITDA", "Normalized EBITDA")
    pat = _row(df, "Net Income Common Stockholders", "Net Income")
    eps = _row(df, "Diluted EPS", "Basic EPS")
    out = []
    for c in df.columns:
        if pd.isna(rev.get(c)) and pd.isna(pat.get(c)):
            continue
        out.append({"label": labeller(c), "end": c.strftime("%Y-%m-%d"), "revenue": crore(rev.get(c)),
                    "ebitda": crore(ebitda.get(c)), "pat": crore(pat.get(c)), "eps": num(eps.get(c))})
    return out


def quarter_slots(rows: list[dict], n: int = 5) -> list[dict]:
    """The last n quarter-ends in order, with an empty slot where Yahoo has no data."""
    if not rows:
        return []
    have = {r["end"]: r for r in rows}
    ends = pd.date_range(end=pd.Timestamp(rows[0]["end"]), periods=n, freq="QE")[::-1]
    return [have.get(e.strftime("%Y-%m-%d")) or {"label": q_label(e), "end": e.strftime("%Y-%m-%d"),
            "revenue": None, "ebitda": None, "pat": None, "eps": None} for e in ends]


def financials(t: yf.Ticker) -> dict:
    annual = statement_rows(t.income_stmt, fy_label)[:4]
    quarterly = quarter_slots(statement_rows(t.quarterly_income_stmt, q_label))
    bs = t.balance_sheet
    balance = None
    if not bs.empty:
        c = bs.columns[0]
        balance = {"label": fy_label(c), "end": c.strftime("%Y-%m-%d"),
                   "cash": crore(_row(bs, "Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents").get(c)),
                   "debt": crore(_row(bs, "Total Debt").get(c)),
                   "net_worth": crore(_row(bs, "Stockholders Equity", "Common Stock Equity").get(c))}
    info = t.info or {}
    return {"basis": "Consolidated", "currency": "INR Cr", "source": "Company filings via Yahoo Finance",
            "annual": annual, "quarterly": quarterly, "balance": balance,
            "pe_ttm": num(info.get("trailingPE"), 1), "pb": num(info.get("priceToBook"), 2)}


# ---------------------------------------------------------------- news
NEWS_MONTHS = 12          # how far back to look for a company Inc42 rarely covers
GAP_S = 1.0               # at most about one request a second to inc42.com
_last_call = [0.0]


def _search(q: str, after: pd.Timestamp, before: pd.Timestamp, tries: int = 4) -> list[dict]:
    """One page of WordPress search results, paced and retried: inc42.com answers bursts with 429."""
    u = "https://inc42.com/wp-json/wp/v2/posts?" + urllib.parse.urlencode(
        {"search": q, "per_page": 100, "orderby": "date", "_fields": "id,date,link,title",
         "after": after.strftime("%Y-%m-%dT00:00:00"), "before": before.strftime("%Y-%m-%dT00:00:00")})
    for attempt in range(tries):
        wait = _last_call[0] + GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
            ra = e.headers.get("Retry-After")
            time.sleep(float(ra) if ra and ra.isdigit() else 15 * 2 ** attempt)
    return []


def inc42_news(aliases: list[str], today: pd.Timestamp | None = None) -> tuple[list[dict], bool]:
    """Up to NEWS_MAX inc42.com stories whose headline names the company or a brand, and
    whether every search succeeded. Scans one calendar month at a time, the current month
    first, and steps back a month only while it still has fewer than NEWS_MAX, so this
    month's stories always lead."""
    complete = True
    # case-insensitive: Inc42 headlines mix spellings (BlueStone/Bluestone, MobiKwik/Mobikwik)
    rx = re.compile(r"\b(" + "|".join(map(re.escape, aliases)) + r")\b", re.I)
    today = (today or pd.Timestamp.now(tz=IST)).tz_localize(None).normalize()
    seen: dict[int, dict] = {}
    month = today.replace(day=1)
    for _ in range(NEWS_MONTHS):
        before = today + pd.Timedelta(days=1) if month == today.replace(day=1) else month + pd.offsets.MonthBegin(1)
        for q in aliases:
            try:
                posts = _search(q, month, before)
            except Exception as e:                   # one alias failing should not lose the rest
                log(f"  ! Inc42 search '{q}' {month:%b %Y} failed: {e}")
                complete = False
                continue
            for p in posts:
                title = html.unescape(re.sub(r"<[^>]+>", "", p["title"]["rendered"])).strip()
                if p["id"] in seen or not rx.search(title) or not p["link"].startswith("https://inc42.com/"):
                    continue
                seen[p["id"]] = {"date": p["date"], "title": title, "url": p["link"], "section": p["link"].split("/")[3]}
        if len(seen) >= NEWS_MAX:
            break
        month = month - pd.offsets.MonthBegin(1)
    return sorted(seen.values(), key=lambda x: x["date"], reverse=True)[:NEWS_MAX], complete


def merge_news(new: list[dict], old: list[dict], complete: bool) -> list[dict]:
    """A run where some searches failed only adds stories; it never drops ones already shown."""
    if complete:
        return new
    by_url = {n["url"]: n for n in old}
    by_url.update({n["url"]: n for n in new})
    return sorted(by_url.values(), key=lambda x: x["date"], reverse=True)[:NEWS_MAX]


def write_json(path: Path, doc) -> None:
    """Write next to the target and rename, so a page polling for updates never reads half a file."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    os.replace(tmp, path)


def refresh_news(ids: list[str], aliases: dict[str, list[str]]) -> int:
    """Weekly job: only the Inc42 stories change; prices and financials are left as they are."""
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    for cid in ids:
        path = OUT / f"{cid}.json"
        if not path.exists():
            log(f"  - {cid}: no profile yet, run a full build first")
            continue
        doc = json.loads(path.read_text())
        news, complete = inc42_news(aliases.get(cid, [doc["company"]]))
        doc["news"] = merge_news(news, doc.get("news", []), complete)
        if complete:
            doc["news_updated"] = stamp
        write_json(path, doc)
        top = doc["news"][0]["date"][:10] if doc["news"] else "none"
        log(f"{doc['company']}: {len(doc['news'])} stories, latest {top}{'' if complete else ' (some searches failed; kept earlier stories)'}")
    return 0


# ---------------------------------------------------------------- prices only
CACHE = V2.parent / ".cache"      # nati_ff.py's own download: daily since late 2023, 5-minute for ~58 days


def refresh_prices(ids: list[str], aliases: dict[str, list[str]]) -> int:
    """Every data refresh: each company's quote, recent bars, 52-week range, weight and rank,
    taken from the bars nati_ff.py has just downloaded, so this makes no calls of its own.
    Older history, financials and news stay as the last full build left them. A company that
    has joined the index and has no profile yet gets a full build."""
    idx = json.loads((DATA / "index.json").read_text())
    cons = json.loads((DATA / "constituents.json").read_text())
    daily_c, m5_c = pd.read_pickle(CACHE / "daily.pkl"), pd.read_pickle(CACHE / "m5.pkl")
    as_of = pd.Timestamp(idx["as_of"]).tz_convert(IST)
    ranked = [c["id"] for c in sorted(cons, key=lambda c: -c["weight"])]
    by_id = {c["id"]: c for c in cons}
    ids = ids or list(by_id)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    missing = []
    for cid in ids:
        c = by_id.get(cid)
        path = OUT / f"{cid}.json"
        if c is None:
            continue
        if not path.exists():
            missing.append(cid)
            continue
        doc = json.loads(path.read_text())
        y = doc["yahoo"]
        d, m = daily_c.get(y), m5_c.get(y)
        if d is None or d.empty:
            log(f"  ! {cid}: no daily bars in the pipeline cache; kept as is")
            continue
        fresh = {r[0]: r for r in fill_from_bars(daily_rows(d, idx["session_date"]), m, idx["session_date"])}
        older = [r for r in doc["daily"] if r[0] < min(fresh)]       # history before the cache window
        daily = older + [fresh[k] for k in sorted(fresh)]
        end_on_quote(daily, idx["session_date"], c["price"])
        sessions = sessions_from_bars(m, daily, as_of) if m is not None and not m.empty else doc["sessions"]
        st = doc["stats"]
        eps, bvps = st.get("eps_ttm"), st.get("bvps")
        if eps is None and st.get("pe_ttm"):                       # profiles built before these were stored
            eps = doc["price"] / st["pe_ttm"]
        if bvps is None and st.get("pb"):
            bvps = doc["price"] / st["pb"]
        doc.update({
            "weight": c["weight"], "rank": ranked.index(cid) + 1, "constituents": len(cons), "in_index_since": c["in_index_since"],
            "price": c["price"], "change_pct": c["change_pct"], "prev_close": sessions[-1]["prev_close"] if sessions else doc.get("prev_close"),
            "mcap_cr": c["mcap_cr"], "ffmc_cr": c["ffmc_cr"], "free_float": c["free_float"],
            "as_of": idx["as_of"], "session_date": idx["session_date"], "market_open": idx["market_open"],
            "refresh_minutes": idx.get("refresh_minutes", 20), "daily": daily, "sessions": sessions,
            "stats": {**st, **week52(daily, idx["session_date"]),
                      "pe_ttm": round(c["price"] / eps, 1) if eps and eps > 0 else None,
                      "pb": round(c["price"] / bvps, 2) if bvps and bvps > 0 else None,
                      "eps_ttm": round(eps, 4) if eps else None, "bvps": round(bvps, 4) if bvps else None},
            "prices_updated": stamp, "generated_at": stamp,
        })
        write_json(path, doc)
    log(f"prices refreshed for {len(ids) - len(missing)} companies, as of {idx['as_of']}")
    if missing:
        log(f"new constituents without a profile, building in full: {', '.join(missing)}")
        main(missing)
    write_index(cons)
    return 0


PERIODS = {"1W": None, "1M": (0, 1), "YTD": None, "1Y": (1, 0)}


def period_returns(daily: list, session_date: str) -> dict:
    """Each period's return from the last close on or before its start (the widget's range rule).
    A company listed after that start gets its return since its first close, with the date in "since"."""
    if not daily:
        return {}
    dates = [r[0] for r in daily]
    last = daily[-1][4]
    end = pd.Timestamp(session_date)
    out, since = {}, {}
    for k, back in PERIODS.items():
        if k == "1W":
            i = max(0, len(daily) - 6)                       # five sessions back
        else:
            start = (pd.Timestamp(f"{end.year - 1}-12-31") if k == "YTD"
                     else end - pd.DateOffset(years=back[0], months=back[1])).strftime("%Y-%m-%d")
            prior = [j for j, d in enumerate(dates) if d <= start]
            i = prior[-1] if prior else 0
            if not prior:
                since[k] = dates[0]
        out[k] = round(last / daily[i][4] - 1, 6) if daily[i][4] else None
    if since:
        out["since"] = since
    return out


def write_index(cons: list[dict]) -> None:
    """The widget links and offers for comparison only current constituents that have a profile.
    It also carries each one's 1W, 1M, YTD and 1Y returns for the heatmap."""
    have = sorted(c["id"] for c in cons if (OUT / f"{c['id']}.json").exists())
    rets = {}
    for cid in have:
        doc = json.loads((OUT / f"{cid}.json").read_text())
        rets[cid] = period_returns(doc["daily"], doc["session_date"])
    write_json(OUT / "index.json", {"ids": have, "returns": rets})


# ---------------------------------------------------------------- main
def main(ids: list[str], news_only: bool = False, prices_only: bool = False) -> int:
    aliases = {r["id"]: r["aliases"].split("|") for r in csv.DictReader(open(ALIASES))}
    if news_only:
        return refresh_news(ids or list(aliases), aliases)
    if prices_only:
        return refresh_prices(ids, aliases)
    idx = json.loads((DATA / "index.json").read_text())
    cons = json.loads((DATA / "constituents.json").read_text())
    universe = {r["ticker"]: r for r in csv.DictReader(open(V2.parent / "config" / "universe.csv"))}
    ids = ids or list(aliases)
    by_id = {c["id"]: c for c in cons}
    ranked = [c["id"] for c in sorted(cons, key=lambda c: -c["weight"])]
    as_of = pd.Timestamp(idx["as_of"]).tz_convert(IST)
    OUT.mkdir(parents=True, exist_ok=True)
    for cid in ids:
        c = by_id[cid]
        yahoo = cid.replace("_NS", ".NS").replace("_BO", ".BO")
        log(f"{c['company']} ({yahoo})")
        t = yf.Ticker(yahoo)
        daily, sessions = price_history(t, as_of, idx["session_date"], c["price"])
        fin = financials(t)
        news, complete = inc42_news(aliases.get(cid, [c["company"]]))
        prev = OUT / f"{cid}.json"
        if not complete and prev.exists():
            news = merge_news(news, json.loads(prev.read_text()).get("news", []), complete)
        u = universe.get(yahoo, {})
        doc = {
            "id": cid, "ticker": c["ticker"], "yahoo": yahoo, "company": c["company"], "sector": c["sector"],
            "listing_date": u.get("listing_date") or None, "in_index_since": c["in_index_since"],
            "weight": c["weight"], "rank": ranked.index(cid) + 1, "constituents": len(cons),
            "price": c["price"], "change_pct": c["change_pct"],
            "prev_close": sessions[-1]["prev_close"] if sessions else None,
            "mcap_cr": c["mcap_cr"], "ffmc_cr": c["ffmc_cr"], "free_float": c["free_float"],
            "as_of": idx["as_of"], "session_date": idx["session_date"], "market_open": idx["market_open"],
            "refresh_minutes": idx.get("refresh_minutes", 20),
            "stats": {**week52(daily, idx["session_date"]), "pe_ttm": fin.get("pe_ttm"), "pb": fin.get("pb"),
                      # per-share earnings and book behind P/E and P/B, so a price refresh can move the ratios
                      "eps_ttm": round(c["price"] / fin["pe_ttm"], 4) if fin.get("pe_ttm") else None,
                      "bvps": round(c["price"] / fin["pb"], 4) if fin.get("pb") else None},
            "financials": {k: v for k, v in fin.items() if k not in ("pe_ttm", "pb")}, "news": news, "news_updated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "daily": daily, "sessions": sessions,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        write_json(OUT / f"{cid}.json", doc)
        log(f"  {len(daily)} days, {len(sessions)} sessions, {len(fin['annual'])} years, "
            f"{sum(q['revenue'] is not None for q in fin['quarterly'])}/{len(fin['quarterly'])} quarters, {len(news)} stories")
    # the index widget links only the companies that have a profile
    write_index(cons)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="profile ids such as ETERNAL_NS (default: all in profile_aliases.csv)")
    ap.add_argument("--news-only", action="store_true", help="refresh only the Inc42 stories")
    ap.add_argument("--prices-only", action="store_true", help="refresh quotes and recent bars from nati_ff.py's cache (every data refresh)")
    a = ap.parse_args()
    sys.exit(main(a.ids, a.news_only, a.prices_only))
