"""
Inc42 New-Age Tech Index - Capped Free-Float Market Cap methodology (Approach 1)
Version 2.1 (15% single-stock cap, no 5/50 rule) - Inc42 Datalabs

Replaces the v1.0 Colab script (3 Mar 2026). What changed and why:
  - Fixed base (31 Dec 2024 = 1,000) with chain-linking, so a published value never changes.
    v1.0 rebuilt a rolling 365-day history from today's weights every day.
  - Quarterly reconstitution that only uses data known at each cut-off (no look-ahead).
  - The concept note's eligibility rules are now applied in code: mainboard listing,
    INR 1,000 Cr market cap floor, 90-day seasoning (30 days for mega IPOs), INR 5 Cr ADTV.
  - A 15% single-stock cap that never reorders constituents. The 5/50 rule is dropped: it is a
    fund-diversification rule (US 25/50, EU 10/40), not an index-design one, and with a 15%
    cap it pinned mid-sized companies at 4.5%. v1.0's 5/50 step also pushed Delhivery above
    Paytm and Ather above Info Edge.
  - Free-float factors come from filed shareholding (config/universe.csv), not hardcoded guesses.
  - Daily OHLC, 5-minute intraday levels, benchmarks and constituent series are written as
    JSON for the web widget (1D/5D/1M/6M/YTD/1Y/5Y/MAX, Compare, Indicators, Overview).
  - The index path is calculated from 5-minute bars; the published page is refreshed every
    REFRESH_MINUTES (20) on trading days by the nati-refresh-20min scheduled task.

Run:
  python nati_ff.py              fetch prices, compute, validate, write site/data/
  python nati_ff.py --cached     reuse prices already downloaded to .cache/
Exit code is non-zero (and nothing is written) if validation fails.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ============================================================================
# CONFIGURATION
# ============================================================================

ROOT = Path(__file__).resolve().parent
UNIVERSE_CSV = ROOT / "config" / "universe.csv"
CACHE = ROOT / ".cache"
OUT = ROOT / "site" / "data"

INDEX_NAME = "Inc42 New-Age Tech Index"
INDEX_CODE = "NATI"
BASE_DATE = pd.Timestamp("2024-12-31")
BASE_VALUE = 1000.0

SINGLE_STOCK_CAP = 0.15        # no constituent above 15%

MIN_MCAP_CR = 1_000            # total market cap floor, INR crore
MIN_ADTV_CR = 5.0              # average daily traded value floor, INR crore
ADTV_WINDOW = 63               # ~3 months of sessions
SEASONING_DAYS = 90
MEGA_SEASONING_DAYS = 30
MEGA_PERCENTILE = 0.90         # mega IPO = free-float mcap in the top 10% of eligible names

RISK_FREE = 0.06               # annual, for Sharpe and Sortino (same as v1.0)
MAX_DAILY_MOVE = 0.15          # validation: a bigger one-day index move blocks publishing
MIN_PRICED_SHARE = 0.90        # validation: share of constituents priced on the latest day
INTRADAY_SESSIONS = 5          # sessions kept for the 1D and 5D charts
REFRESH_MINUTES = 20           # the page is republished this often on trading days (nati-refresh-20min task)

CR = 1e7
IST = "Asia/Kolkata"

# Compare list (the only symbols offered in Compare): id -> (Yahoo symbol, display name,
# exchange label, region). Indian indices get 5-minute sessions for 1D/5D; US indices trade
# outside Indian hours, so they are daily only (1M and longer).
BENCHMARKS = {
    "NIFTY_50": ("^NSEI", "NIFTY 50", "INDEXNSE", "IN"),
    "SENSEX": ("^BSESN", "BSE SENSEX", "INDEXBOM", "IN"),
    "NIFTY_NEXT_50": ("^NSMIDCP", "NIFTY Next 50", "INDEXNSE", "IN"),
    "SP500": ("^GSPC", "S&P 500", "INDEXSP", "US"),
    "NASDAQ_100": ("^NDX", "NASDAQ-100", "INDEXNASDAQ", "US"),
    "NASDAQ": ("^IXIC", "NASDAQ Composite", "INDEXNASDAQ", "US"),
}
US_TZ = "America/New_York"


def log(msg: str) -> None:
    print(msg, flush=True)


# ============================================================================
# STEP 1: UNIVERSE
# ============================================================================

def load_universe() -> pd.DataFrame:
    """Candidate pool. Rows without a share count or free-float factor (unconfirmed
    tickers, reverse-merger listings with no IPO) and non-Indian listings are left out."""
    u = pd.read_csv(UNIVERSE_CSV, dtype={"ticker": str})
    u["listing_date"] = pd.to_datetime(u["listing_date"], errors="coerce")
    domestic = u["ticker"].str.endswith((".NS", ".BO"))
    complete = u["shares"].notna() & u["free_float"].notna() & u["listing_date"].notna()
    dropped = u[~(domestic & complete)]
    for _, r in dropped.iterrows():
        log(f"  - left out of the candidate pool: {r['company']} ({r['ticker']})")
    u = u[domestic & complete].copy()
    bad_ff = ~u["free_float"].between(0.01, 1.0)
    if bad_ff.any():
        raise ValueError(f"free_float outside 0-1 for: {', '.join(u.loc[bad_ff, 'company'])}")
    return u.set_index("ticker")


# ============================================================================
# STEP 2: PRICES (Yahoo Finance)
# ============================================================================

def _download(tickers: list[str], tries: int = 3, actions: bool = False,
              **kw) -> dict[str, pd.DataFrame]:
    """One frame per ticker (Open/High/Low/Close/Volume, plus Stock Splits with actions=True).
    Retries the ones that come back empty."""
    cols = ["Open", "High", "Low", "Close", "Volume"] + (["Stock Splits"] if actions else [])
    out: dict[str, pd.DataFrame] = {}
    todo = list(tickers)
    for attempt in range(1, tries + 1):
        if not todo:
            break
        raw = yf.download(todo, group_by="ticker", auto_adjust=False, actions=actions,
                          progress=False, threads=True, **kw)
        for t in todo:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = df.dropna(subset=["Close"])
            if len(df):
                df = df.reindex(columns=cols)
                if actions:
                    df["Stock Splits"] = df["Stock Splits"].fillna(0)
                out[t] = df.astype(float)
        todo = [t for t in todo if t not in out]
        if todo and attempt < tries:
            log(f"  retrying {len(todo)} symbols ({', '.join(todo[:6])}{'...' if len(todo) > 6 else ''})")
            time.sleep(3 * attempt)
    for t in todo:
        log(f"  ! no data for {t}")
    return out


def fetch_all(tickers: list[str], use_cache: bool) -> dict[str, dict[str, pd.DataFrame]]:
    """Daily bars since a year before the base, hourly bars (Yahoo keeps ~730 days) and
    5-minute bars (Yahoo keeps ~60 days). Close is split-adjusted, not dividend-adjusted,
    so this is a price index. Daily bars carry Yahoo's Stock Splits events (see split_factors)."""
    CACHE.mkdir(exist_ok=True)
    today = datetime.now()
    plan = {
        "daily": dict(start=(BASE_DATE - timedelta(days=400)).strftime("%Y-%m-%d"), interval="1d",
                      actions=True),
        # explicit start dates: with period= Yahoo rejects stocks listed before the window
        "hourly": dict(start=(today - timedelta(days=728)).strftime("%Y-%m-%d"), interval="1h"),
        "m5": dict(start=(today - timedelta(days=58)).strftime("%Y-%m-%d"), interval="5m"),
    }
    data = {}
    for name, kw in plan.items():
        f = CACHE / f"{name}.pkl"
        if use_cache and f.exists():
            data[name] = pd.read_pickle(f)
            # a daily cache written before split events were downloaded cannot be reused
            stale = kw.get("actions") and any("Stock Splits" not in df for df in data[name].values())
            if not stale and set(tickers) <= set(data[name]):
                log(f"  {name}: {len(data[name])} symbols from cache")
                continue
        log(f"  {name}: downloading {len(tickers)} symbols ...")
        data[name] = _download(tickers, **kw)
        pd.to_pickle(data[name], f)
    return data


def to_daily_index(df: pd.DataFrame) -> pd.DataFrame:
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(IST).tz_localize(None)
    df = df.copy()
    df.index = pd.DatetimeIndex(idx).normalize()
    return df[~df.index.duplicated(keep="last")]


def to_ist(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    idx = df.index
    df.index = idx.tz_localize("UTC").tz_convert(IST) if idx.tz is None else idx.tz_convert(IST)
    return df[~df.index.duplicated(keep="last")]


def split_factors(u: pd.DataFrame, daily: dict[str, pd.DataFrame]) -> pd.Series:
    """Multiplier that puts each config share count on Yahoo's current (adjusted) basis.

    Config shares are as of `ff_period` (the shareholding-pattern date), but Yahoo restates
    every past price after a split, bonus or consolidation. Any such event after that date
    changes the count: a 2:1 split is 2.0, a 1:10 consolidation is 0.1. Same rule as
    split_factors() in nati-embed/nati.py.
    """
    out = pd.Series(1.0, index=u.index)
    for t in u.index:
        if t not in daily or "Stock Splits" not in daily[t]:
            continue
        ev = daily[t]["Stock Splits"]
        ev = ev[ev > 0]
        since = pd.to_datetime(u.at[t, "ff_period"], errors="coerce")
        if pd.isna(since):
            continue
        ev = ev[ev.index > since]
        if len(ev):
            out[t] = float(ev.prod())
            log(f"  {u.at[t, 'company']}: split/bonus factor {out[t]:g} since {since:%d %b %Y}; "
                f"update shares in config/universe.csv at the next review")
    return out


def panel(frames: dict[str, pd.DataFrame], field: str, tickers: list[str]) -> pd.DataFrame:
    return pd.DataFrame({t: frames[t][field] for t in tickers if t in frames})


# ============================================================================
# STEP 3: TRADING CALENDAR AND REVIEW DATES
# ============================================================================

def trading_calendar(daily_close: pd.DataFrame, daily_volume: pd.DataFrame,
                     nifty: pd.Series) -> pd.DatetimeIndex:
    """Sessions where most listed stocks actually traded. Yahoo sometimes adds bars on
    exchange holidays that repeat the previous close; those have no volume.
    A NIFTY bar is not required: Yahoo sometimes drops one (21 Oct 2026), and requiring it
    would skip a real session and fold its move into the next day. Same rule as nati-embed."""
    listed = daily_close.notna().sum(axis=1)
    traded = (daily_volume.fillna(0) > 0).sum(axis=1)
    ok = (traded >= 0.6 * listed.clip(lower=1)) & (listed > 0)
    missing = ok[ok].index.difference(nifty.dropna().index)
    if len(missing):
        log(f"  no NIFTY bar on {', '.join(str(d.date()) for d in missing[-5:])}; kept as sessions")
    return pd.DatetimeIndex(ok[ok].index.sort_values())


def review_cutoffs(cal: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Last session of each quarter from the base date on. Weights set at a cut-off's close
    apply from the next session."""
    s = pd.Series(cal, index=cal)
    q_last = s.groupby(cal.to_period("Q")).max()
    cuts = [d for d in q_last if d >= BASE_DATE]
    if cuts and cuts[-1] == cal[-1]:
        cuts = cuts[:-1]                      # the quarter in progress has not closed yet
    if BASE_DATE not in cuts:
        cuts = [BASE_DATE] + cuts
    return cuts


# ============================================================================
# STEP 4: ELIGIBILITY SCREEN
# ============================================================================

def screen(cut: pd.Timestamp, u: pd.DataFrame, close: pd.DataFrame,
           volume: pd.DataFrame) -> pd.DataFrame:
    """Every candidate with its numbers at the cut-off and the reason it is in or out."""
    hist_c = close.loc[:cut]
    hist_v = volume.loc[:cut]
    rows = []
    for t, r in u.iterrows():
        rec = {"ticker": t, "company": r["company"], "sector": r["sector"]}
        if t not in close.columns or cut not in close.index or pd.isna(close.at[cut, t]):
            rec.update(status="out", reason="not listed or no price at cut-off")
            rows.append(rec)
            continue
        px = close.at[cut, t]
        age = (cut - r["listing_date"]).days
        mcap = px * r["shares"] / CR
        tv = (hist_c[t] * hist_v[t]).dropna().tail(ADTV_WINDOW)
        tv = tv[tv > 0]
        adtv = tv.mean() / CR if len(tv) else 0.0
        rec.update(price=px, age_days=age, mcap_cr=mcap, ffmc_cr=mcap * r["free_float"],
                   adtv_cr=adtv)
        rows.append(rec)
    df = pd.DataFrame(rows).set_index("ticker")
    priced = df["price"].notna() if "price" in df else pd.Series(False, index=df.index)
    big_enough = priced & (df["mcap_cr"] >= MIN_MCAP_CR) & (df["adtv_cr"] >= MIN_ADTV_CR)
    seasoned = big_enough & (df["age_days"] >= SEASONING_DAYS)
    df["status"] = df["status"].where(df["status"].notna(), "out") if "status" in df else "out"
    df.loc[seasoned, "status"] = "in"
    df.loc[seasoned, "reason"] = "eligible"
    # Mega IPO fast track: 30+ days of trading and free-float mcap in the top 10% of eligible names
    if seasoned.any():
        bar = df.loc[seasoned, "ffmc_cr"].quantile(MEGA_PERCENTILE)
        mega = big_enough & ~seasoned & (df["age_days"] >= MEGA_SEASONING_DAYS) & (df["ffmc_cr"] >= bar)
        df.loc[mega, "status"] = "in"
        df.loc[mega, "reason"] = "mega IPO fast track (30 days)"
    out = priced & (df["status"] != "in")
    df.loc[out & (df["mcap_cr"] < MIN_MCAP_CR), "reason"] = "below INR 1,000 Cr market cap"
    df.loc[out & (df["mcap_cr"] >= MIN_MCAP_CR) & (df["adtv_cr"] < MIN_ADTV_CR), "reason"] = \
        "below INR 5 Cr daily traded value"
    df.loc[out & (df["mcap_cr"] >= MIN_MCAP_CR) & (df["adtv_cr"] >= MIN_ADTV_CR), "reason"] = \
        "in seasoning period"
    return df


# ============================================================================
# STEP 5: CAPPED WEIGHTS (15% single stock)
# ============================================================================

def capped_weights(ffmc: pd.Series) -> pd.Series:
    """Free-float weights with no constituent above 15%. The excess of a capped company is
    shared out pro rata among the rest, repeating until nothing is above the cap. Order by
    size is preserved (capped companies tie at 15%)."""
    raw = ffmc / ffmc.sum()
    if SINGLE_STOCK_CAP * len(raw) < 1:
        raise ArithmeticError(f"only {len(raw)} constituents; a {SINGLE_STOCK_CAP:.0%} cap needs more")
    capped = pd.Series(False, index=raw.index)
    w = raw.copy()
    for _ in range(len(raw)):
        free = ~capped
        w[free] = raw[free] / raw[free].sum() * (1.0 - SINGLE_STOCK_CAP * capped.sum())
        w[capped] = SINGLE_STOCK_CAP
        breach = free & (w > SINGLE_STOCK_CAP + 1e-12)
        if not breach.any():
            return w
        capped |= breach
    raise ArithmeticError("capping did not converge")


def check_weights(w: pd.Series, raw: pd.Series, when) -> None:
    assert abs(w.sum() - 1) < 1e-9, f"{when}: weights sum to {w.sum()}"
    assert w.max() <= SINGLE_STOCK_CAP + 1e-9, f"{when}: max weight {w.max():.4f}"
    # ranking check: a bigger company never ends up with a smaller weight
    o = raw.sort_values(ascending=False).index
    assert (np.diff(w[o].values) <= 1e-9).all(), f"{when}: capping reordered constituents"


# ============================================================================
# STEP 6: INDEX LEVELS (chain-linked, daily OHLC and intraday)
# ============================================================================

def split_by_day(frames: dict[str, pd.DataFrame]) -> dict[str, dict[pd.Timestamp, pd.DataFrame]]:
    """{ticker: {session date: that session's bars}} so each day is looked up once."""
    return {t: {d: g for d, g in df.groupby(df.index.normalize().tz_localize(None))}
            for t, df in frames.items()}


def intraday_path(day: pd.Timestamp, units: pd.Series, bars: dict[str, dict],
                  day_open: pd.Series, day_close: pd.Series) -> pd.Series | None:
    """Index level after each intraday bar of one session.
    A stock with no intraday bars that day (Yahoo dropped the pre-rename history of
    ETERNAL, CCAVENUE and ARIS) is moved in a straight line from its open to its close,
    so it neither lifts the high nor drags the low. Returns None when stocks with real
    bars hold less than 75% of the index value."""
    cols = {}
    for t in units.index:
        if t in bars and day in bars[t] and len(bars[t][day]):
            bar_close = bars[t][day]["Close"]
            # Yahoo's intraday bars are sometimes not adjusted for a later bonus or split
            # (Nazara's 1:1 bonus): rescale the session when its last bar is >2% off the close
            k = day_close[t] / bar_close.iloc[-1]
            cols[t] = bar_close * k if abs(np.log(k)) > 0.02 else bar_close
    if not cols:
        return None
    value = units * day_close
    if value[list(cols)].sum() < 0.75 * value.sum():
        return None
    px = pd.DataFrame(cols).sort_index()
    n = len(px)
    for t in units.index:
        if t in px:
            px[t] = px[t].ffill().fillna(day_open[t])
        else:
            px[t] = np.linspace(day_open[t], day_close[t], n + 1)[1:]
    px = px[units.index]
    if px.isna().any().any():
        return None
    return px @ units


def build_index(u, cal, close, open_, high, low, volume, hourly, m5):
    cuts = review_cutoffs(cal)
    log(f"  review cut-offs: {', '.join(d.strftime('%d %b %Y') for d in cuts)}")
    c = close.reindex(cal).ffill()
    o = open_.reindex(cal)
    hi = high.reindex(cal)
    lo = low.reindex(cal)

    level = pd.Series(np.nan, index=cal)
    level[BASE_DATE] = BASE_VALUE
    unit_of_day: dict[pd.Timestamp, pd.Series] = {}
    reviews = []
    for k, cut in enumerate(cuts):
        scr = screen(cut, u, close.reindex(cal).ffill(limit=5), volume.reindex(cal))
        members = scr[scr["status"] == "in"]
        w = capped_weights(members["ffmc_cr"])
        check_weights(w, members["ffmc_cr"], cut.date())
        units = w * level[cut] / c.loc[cut, w.index]
        end = cuts[k + 1] if k + 1 < len(cuts) else cal[-1]
        days = cal[(cal > cut) & (cal <= end)]
        level[days] = c.loc[days, units.index] @ units
        for d in days:
            unit_of_day[d] = units
        effective = days[0] if len(days) else None
        reviews.append({
            "cutoff": cut.strftime("%Y-%m-%d"),
            "effective": effective.strftime("%Y-%m-%d") if effective is not None else None,
            "constituents": int(len(w)),
            "capped": [t for t in w.index if w[t] >= SINGLE_STOCK_CAP - 1e-9],
            "weights": {t: round(float(x), 6) for t, x in w.sort_values(ascending=False).items()},
            "top3_total": round(float(w.nlargest(3).sum()), 4),
            "excluded": [{"ticker": t, "company": r["company"], "reason": r["reason"]}
                         for t, r in scr[scr["status"] != "in"].iterrows()],
        })
        log(f"  {cut.date()}: {len(w):2d} constituents · max {w.max():.1%} · "
            f"top 3 {w.nlargest(3).sum():.1%} · at cap: {(w >= SINGLE_STOCK_CAP - 1e-9).sum()}")

    # Daily OHLC. Open uses each stock's opening price; high and low come from the index's own
    # path sampled every 5 minutes (hourly where 5-minute bars are no longer available).
    rows = []
    days = cal[cal >= BASE_DATE]
    for d in days:
        cl = level[d]
        if d == BASE_DATE:
            rows.append((d, cl, cl, cl, cl, "base"))
            continue
        units = unit_of_day[d]
        prev = c.shift(1).loc[d, units.index]
        op_px = o.loc[d, units.index].where(o.loc[d, units.index] > 0, prev)
        op = float(op_px @ units)
        cl_px = c.loc[d, units.index]
        path, src = intraday_path(d, units, m5, op_px, cl_px), "5m"
        if path is None:
            path, src = intraday_path(d, units, hourly, op_px, cl_px), "1h"
        # Outer bound: every constituent at its own high (or low) at the same moment
        bound_h = max(op, cl, float(hi.loc[d, units.index].fillna(cl_px) @ units))
        bound_l = min(op, cl, float(lo.loc[d, units.index].fillna(cl_px) @ units))
        if path is not None:
            h, l = max(op, cl, path.max()), min(op, cl, path.min())
        else:
            h, l, src = np.nan, np.nan, "estimated"
        rows.append((d, op, h, l, cl, src, bound_h, bound_l))
    ohlc = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "hl_source",
                                       "bound_h", "bound_l"]).set_index("date")
    # Days with no usable intraday bars (e.g. the Saturday Budget session): place high and low
    # the same typical fraction of the way to the outer bound as on days with real bars.
    top, bot = ohlc[["open", "close"]].max(axis=1), ohlc[["open", "close"]].min(axis=1)
    real = ohlc["hl_source"].isin(["5m", "1h"])
    k_up = ((ohlc["high"] - top) / (ohlc["bound_h"] - top)).where(real & (ohlc["bound_h"] > top)).median()
    k_dn = ((bot - ohlc["low"]) / (bot - ohlc["bound_l"])).where(real & (ohlc["bound_l"] < bot)).median()
    est = ohlc["hl_source"] == "estimated"
    ohlc.loc[est, "high"] = top[est] + k_up * (ohlc.loc[est, "bound_h"] - top[est])
    ohlc.loc[est, "low"] = bot[est] - k_dn * (bot[est] - ohlc.loc[est, "bound_l"])
    if est.any():
        log(f"  {int(est.sum())} days without intraday bars: high/low estimated "
            f"(calibration {k_up:.2f} up, {k_dn:.2f} down)")
    return ohlc.drop(columns=["bound_h", "bound_l"]), unit_of_day, reviews, cuts


def sessions_payload(series_bars: dict[str, pd.DataFrame] | pd.Series, prev_close: pd.Series,
                     sessions: list[pd.Timestamp], day_open: pd.Series | None = None,
                     day_close: pd.Series | None = None) -> list[dict]:
    """[{date, prev_close, points: [[epoch_seconds_IST_as_UTC, value], ...]}] for the last
    sessions. Times are shifted so a chart that prints UTC shows IST wall-clock time."""
    out = []
    for d in sessions:
        s = series_bars[series_bars.index.normalize().tz_localize(None) == d]
        if not len(s):
            continue
        pts = []
        start = pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=9, minutes=15)
        if day_open is not None and d in day_open.index and pd.notna(day_open[d]):
            pts.append([_epoch(start), round(float(day_open[d]), 2)])
        for ts, v in s.items():
            end_ts = ts + pd.Timedelta(minutes=5)
            pts.append([_epoch(end_ts), round(float(v), 2)])
        # end each session on the official (or latest) daily close, not the last 5-minute bar
        if day_close is not None and d in day_close.index and pd.notna(day_close[d]) and pts:
            pts[-1][1] = round(float(day_close[d]), 2)
        pc = prev_close.get(d, np.nan)
        out.append({"date": d.strftime("%Y-%m-%d"),
                    "prev_close": None if pd.isna(pc) else round(float(pc), 2),
                    "points": pts})
    return out


def _epoch(ts: pd.Timestamp) -> int:
    """Seconds since epoch of the IST wall-clock time read as UTC."""
    naive = ts.tz_convert(IST).tz_localize(None)
    return int((naive - pd.Timestamp("1970-01-01")).total_seconds())


# ============================================================================
# STEP 7: METRICS (trailing 365 days, as in the concept note)
# ============================================================================

def metrics(close: pd.Series) -> dict:
    s = close[close.index >= close.index[-1] - pd.Timedelta(days=365)]
    r = s.pct_change().dropna()
    total = s.iloc[-1] / s.iloc[0] - 1
    years = (s.index[-1] - s.index[0]).days / 365.25
    ann = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    vol = r.std() * np.sqrt(252)
    rf_d = (1 + RISK_FREE) ** (1 / 252) - 1
    downside = np.sqrt((np.minimum(r - rf_d, 0) ** 2).mean()) * np.sqrt(252)
    wealth = (1 + r).cumprod()
    dd = (wealth / wealth.cummax() - 1).min()
    return {
        "window_start": s.index[0].strftime("%Y-%m-%d"),
        "total_return": round(float(total), 6),
        "annualised_return": round(float(ann), 6),
        "volatility": round(float(vol), 6),
        "sharpe": round(float((ann - RISK_FREE) / vol), 3) if vol > 0 else None,
        "sortino": round(float((ann - RISK_FREE) / downside), 3) if downside > 0 else None,
        "max_drawdown": round(float(dd), 6),
        "risk_free": RISK_FREE,
    }


# ============================================================================
# STEP 8: RUN, VALIDATE, WRITE
# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cached", action="store_true", help="reuse prices in .cache/")
    args = ap.parse_args()

    log("=" * 78)
    log(f"{INDEX_NAME} ({INDEX_CODE}) · capped free-float · base {BASE_DATE.date()} = {BASE_VALUE:,.0f}")
    log("=" * 78)
    u = load_universe()
    log(f"  candidate pool: {len(u)} mainboard companies")

    bench_syms = [v[0] for v in BENCHMARKS.values()]
    data = fetch_all(list(u.index) + bench_syms, args.cached)
    daily = {t: to_daily_index(df) for t, df in data["daily"].items()}
    hourly = {t: to_ist(df) for t, df in data["hourly"].items()}
    m5 = {t: to_ist(df) for t, df in data["m5"].items()}
    hourly_days, m5_days = split_by_day(hourly), split_by_day(m5)
    u["shares"] = u["shares"] * split_factors(u, daily)

    stocks = [t for t in u.index if t in daily]
    close, open_, high, low, volume = (panel(daily, f, stocks) for f in
                                       ("Close", "Open", "High", "Low", "Volume"))
    nifty = daily["^NSEI"]["Close"]
    cal = trading_calendar(close, volume, nifty)
    cal = cal[cal >= BASE_DATE - pd.Timedelta(days=400)]
    if BASE_DATE not in cal:
        raise SystemExit(f"base date {BASE_DATE.date()} is not a trading session")

    log("-" * 78)
    ohlc, unit_of_day, reviews, cuts = build_index(u, cal, close, open_, high, low, volume, hourly_days, m5_days)

    # ---------------------------------------------------------------- validation
    problems = []
    if abs(ohlc["close"].iloc[0] - BASE_VALUE) > 1e-9:
        problems.append("base value is not 1,000")
    if ohlc[["open", "high", "low", "close"]].isna().any().any():
        problems.append("missing index values")
    moves = ohlc["close"].pct_change().abs()
    if (moves > MAX_DAILY_MOVE).any():
        problems.append(f"one-day move above {MAX_DAILY_MOVE:.0%} on "
                        f"{', '.join(str(d.date()) for d in moves[moves > MAX_DAILY_MOVE].index)}")
    if not ((ohlc["high"] >= ohlc[["open", "close"]].max(axis=1) - 1e-6) &
            (ohlc["low"] <= ohlc[["open", "close"]].min(axis=1) + 1e-6)).all():
        problems.append("high/low inconsistent with open/close")
    last = cal[-1]
    units_now = unit_of_day[last]
    priced_today = sum(1 for t in units_now.index if last in daily[t].index)
    if priced_today < MIN_PRICED_SHARE * len(units_now):
        problems.append(f"only {priced_today}/{len(units_now)} constituents priced on {last.date()}")
    if problems:
        log("\nVALIDATION FAILED - nothing written:")
        for p in problems:
            log(f"  x {p}")
        return 1

    # ---------------------------------------------------------------- overview numbers
    c_now = close.reindex(cal).ffill().loc[last, units_now.index]
    mcap_total = float((c_now * u.loc[units_now.index, "shares"]).sum() / CR)
    ffmc_total = float((c_now * u.loc[units_now.index, "shares"] * u.loc[units_now.index, "free_float"]).sum() / CR)
    yr = ohlc[ohlc.index > last - pd.Timedelta(days=365)]
    prev_close = float(ohlc["close"].iloc[-2])
    now_ist = pd.Timestamp.now(tz=IST)

    # ---------------------------------------------------------------- intraday (1D / 5D)
    sess_days = [d for d in cal[-INTRADAY_SESSIONS:]]
    idx_paths, idx_open = [], {}
    for d in sess_days:
        units = unit_of_day[d]
        prev = close.reindex(cal).ffill().shift(1).loc[d, units.index]
        op_px = open_.reindex(cal).loc[d, units.index]
        op_px = op_px.where(op_px > 0, prev)
        p = intraday_path(d, units, m5_days, op_px,
                          close.reindex(cal).ffill().loc[d, units.index])
        if p is not None:
            idx_paths.append(p)
            idx_open[d] = float(ohlc.at[d, "open"])
    idx_bars = pd.concat(idx_paths) if idx_paths else pd.Series(dtype=float)
    prev_by_day = ohlc["close"].shift(1)
    sessions = sessions_payload(idx_bars, prev_by_day, sess_days, pd.Series(idx_open), ohlc["close"])
    last_bar = idx_bars.index.max() if len(idx_bars) else None
    as_of = (last_bar + pd.Timedelta(minutes=5)) if last_bar is not None else \
        pd.Timestamp(last).tz_localize(IST) + pd.Timedelta(hours=15, minutes=30)
    as_of = min(as_of, pd.Timestamp(last).tz_localize(IST) + pd.Timedelta(hours=15, minutes=30))
    market_open = (last == pd.Timestamp(now_ist.date()) and
                   now_ist.time() < datetime.strptime("15:30", "%H:%M").time())

    # ---------------------------------------------------------------- compare symbols
    symbols, series_files = [], {}

    def add_symbol(sid, sym, name, exch, region):
        if sym not in daily:
            log(f"  ! compare: no data for {name} ({sym}); left out of Compare")
            return
        d = daily[sym]["Close"].dropna()
        d = d[d.index >= BASE_DATE - pd.Timedelta(days=10)]
        if region == "US":
            # a bar for today's US date is a live partial until the US close settles
            now_us = pd.Timestamp.now(tz=US_TZ)
            if now_us.hour < 17:
                d = d[d.index < pd.Timestamp(now_us.date())]
        if len(d) < 2:
            log(f"  ! compare: too little history for {name} ({sym}); left out of Compare")
            return
        sess = []
        bars = m5.get(sym) if region == "IN" else None
        if bars is not None and len(bars):
            sess = sessions_payload(bars["Close"], d.shift(1), [x for x in sess_days if x in d.index],
                                    bars["Open"].groupby(bars.index.normalize().tz_localize(None)).first(), d)
        last_v, prev_v = float(d.iloc[-1]), float(d.iloc[-2])
        symbols.append({"id": sid, "name": name, "sub": f"{name} · {exch}", "kind": "benchmark",
                        "region": region, "intraday": bool(sess),
                        "last": round(last_v, 2), "change_pct": round(last_v / prev_v - 1, 6),
                        "last_date": d.index[-1].strftime("%Y-%m-%d")})
        series_files[sid] = {"id": sid, "name": name, "region": region,
                             "daily": [[i.strftime("%Y-%m-%d"), round(float(v), 2)] for i, v in d.items()],
                             "sessions": sess}

    for sid, (sym, name, exch, region) in BENCHMARKS.items():
        add_symbol(sid, sym, name, exch, region)

    # ---------------------------------------------------------------- constituents table
    first_in = {}
    for rv in reviews:
        for t in rv["weights"]:
            first_in.setdefault(t, rv["effective"])
    level_now = float(ohlc["close"].iloc[-1])
    cur_w = units_now * c_now / level_now
    prev_px = close.reindex(cal).ffill().loc[cal[-2], units_now.index]
    constituents = []
    for t in cur_w.sort_values(ascending=False).index:
        r = u.loc[t]
        constituents.append({
            "id": t.replace(".", "_"), "ticker": t.replace(".NS", "").replace(".BO", ""),
            "company": r["company"], "sector": r["sector"],
            "weight": round(float(cur_w[t]), 6),
            "weight_at_review": reviews[-1]["weights"].get(t),
            "price": round(float(c_now[t]), 2),
            "change_pct": round(float(c_now[t] / prev_px[t] - 1), 6),
            "mcap_cr": round(float(c_now[t] * r["shares"] / CR), 0),
            "ffmc_cr": round(float(c_now[t] * r["shares"] * r["free_float"] / CR), 0),
            "free_float": float(r["free_float"]),
            "in_index_since": first_in.get(t),
        })
    sectors = (pd.DataFrame(constituents).groupby("sector")["weight"].agg(["sum", "count"])
               .sort_values("sum", ascending=False))

    q = pd.Period(cuts[-1], "Q") + 1
    next_review = (q.end_time.normalize()).strftime("%Y-%m-%d")

    index_json = {
        "id": INDEX_CODE, "name": INDEX_NAME,
        "base_date": BASE_DATE.strftime("%Y-%m-%d"), "base_value": BASE_VALUE,
        "as_of": as_of.isoformat(), "session_date": last.strftime("%Y-%m-%d"),
        "market_open": bool(market_open),
        "refresh_minutes": REFRESH_MINUTES,
        "generated_at": now_ist.isoformat(timespec="seconds"),
        "last": round(level_now, 2), "prev_close": round(prev_close, 2),
        "change": round(level_now - prev_close, 2),
        "change_pct": round(level_now / prev_close - 1, 6),
        "overview": {
            "open": round(float(ohlc["open"].iloc[-1]), 2),
            "high": round(float(ohlc["high"].iloc[-1]), 2),
            "low": round(float(ohlc["low"].iloc[-1]), 2),
            "prev_close": round(prev_close, 2),
            "wk52_high": round(float(yr["high"].max()), 2),
            "wk52_high_date": yr["high"].idxmax().strftime("%Y-%m-%d"),
            "wk52_low": round(float(yr["low"].min()), 2),
            "wk52_low_date": yr["low"].idxmin().strftime("%Y-%m-%d"),
            "total_mcap_cr": round(mcap_total, 0),
            "ffmc_cr": round(ffmc_total, 0),
            "constituents": int(len(units_now)),
            "last_review": reviews[-1]["cutoff"],
            "last_review_effective": reviews[-1]["effective"],
            "next_review": next_review,
        },
        "metrics_1y": metrics(ohlc["close"]),
        "since_base": round(level_now / BASE_VALUE - 1, 6),
        "sectors": [{"sector": s, "weight": round(float(r["sum"]), 6), "count": int(r["count"])}
                    for s, r in sectors.iterrows()],
        "daily": [[d.strftime("%Y-%m-%d"), round(r.open, 2), round(r.high, 2), round(r.low, 2),
                   round(r.close, 2)] for d, r in ohlc.iterrows()],
        "hl_source": {k: int(v) for k, v in ohlc["hl_source"].value_counts().items()},
        "sessions": sessions,
    }

    # ---------------------------------------------------------------- write
    (OUT / "series").mkdir(parents=True, exist_ok=True)
    for f in (OUT / "series").glob("*.json"):
        f.unlink()

    def dump(path: Path, obj) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str))
        tmp.replace(path)

    dump(OUT / "index.json", index_json)
    dump(OUT / "symbols.json", symbols)
    dump(OUT / "constituents.json", constituents)
    dump(OUT / "reviews.json", reviews)
    for sid, obj in series_files.items():
        dump(OUT / "series" / f"{sid}.json", obj)
    ohlc.round(2).to_csv(ROOT / "index_history.csv")

    # ---------------------------------------------------------------- report
    m = index_json["metrics_1y"]
    log("-" * 78)
    log(f"  {INDEX_CODE} {level_now:,.2f}  ({index_json['change_pct']:+.2%} on the day) · session {last.date()}")
    log(f"  52-wk range {index_json['overview']['wk52_low']:,.2f} - {index_json['overview']['wk52_high']:,.2f}")
    log(f"  total mcap INR {mcap_total:,.0f} Cr · free-float INR {ffmc_total:,.0f} Cr · {len(units_now)} constituents")
    log(f"  1Y return {m['total_return']:.2%} · vol {m['volatility']:.2%} · Sharpe {m['sharpe']} · "
        f"Sortino {m['sortino']} · max DD {m['max_drawdown']:.2%}")
    log(f"  high/low source: {index_json['hl_source']}")
    log(f"  wrote {OUT.relative_to(ROOT)}/ ({len(series_files)} compare series) and index_history.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
