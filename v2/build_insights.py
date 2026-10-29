"""v2 sandbox: data for the widget's Key Insights tab.

Writes v2/site/data/insights.json from files already in v2/site/data (nothing is downloaded):

  windows       for the past 5, 10 and 20 sessions: the index against NIFTY 50, SENSEX, NIFTY Next 50 and
                NASDAQ-100; breadth; the companies and sectors that moved it most, in index points;
                the biggest gainers and losers
  correlation   correlation of NATI's daily returns with NIFTY 50 and NASDAQ-100, per calendar
                quarter since the base date and over the past year
  headlines     a few plain-language sentences built from the numbers above

Run after build_profiles.py, which supplies each company's daily closes:
    .venv/bin/python v2/build_insights.py
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parent
DATA = V2 / "site" / "data"
BENCH = ["NIFTY_50", "SENSEX", "NIFTY_NEXT_50", "NASDAQ_100"]
MIN_QUARTER_SESSIONS = 20        # fewer than this and a quarter's correlation is marked as partial


def write_json(path: Path, doc) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":"), default=float))
    os.replace(tmp, path)


def close_series(rows, col: int) -> pd.Series:
    return pd.Series({r[0]: r[col] for r in rows if r[col] is not None}, dtype=float).sort_index()


def strength(r: float) -> str:
    a = abs(r)
    return "closely" if a >= 0.7 else "moderately" if a >= 0.4 else "loosely" if a >= 0.2 else "barely"


def pct(x: float, d: int = 1) -> str:
    return f"{'+' if x > 0 else '−' if x < 0 else ''}{abs(x) * 100:.{d}f}%"


def pts(x: float) -> str:
    return f"{'+' if x > 0 else '−' if x < 0 else ''}{abs(x):.1f} pts"


WINDOWS = {5: ("Past week", "the past week"), 10: ("Past two weeks", "the past two weeks"), 20: ("Past month", "the past month")}
CACHE = V2.parent / ".cache" / "daily.pkl"     # nati_ff.py's daily bars for every candidate, constituents or not


def load_prices(cons: list[dict], session_date: str) -> dict[str, pd.Series]:
    """Daily closes by ticker ("ETERNAL.NS"): nati_ff.py's cache covers every company that has been in the
    index; the company profiles fill in anything it lacks. Today's session takes the index build's own
    prices (constituents.json), which come from the same 5-minute bars as the index level; the source's
    daily bar for an open session lags them."""
    out: dict[str, pd.Series] = {}
    if CACHE.exists():
        m5 = pd.read_pickle(CACHE.with_name("m5.pkl")) if CACHE.with_name("m5.pkl").exists() else {}
        for t, df in pd.read_pickle(CACHE).items():
            s = df["Close"].dropna()
            s.index = s.index.strftime("%Y-%m-%d")
            # sessions the daily data skipped: that day's last 5-minute close
            bars = m5.get(t)
            if bars is not None and not bars.empty:
                last5 = bars["Close"].dropna().groupby(bars.index.normalize()).last()
                last5.index = last5.index.strftime("%Y-%m-%d")
                s = pd.concat([s, last5[~last5.index.isin(s.index)]]).sort_index()
            out[t] = s
    for c in cons:
        t = c["id"].replace("_NS", ".NS").replace("_BO", ".BO")
        p = DATA / "companies" / f"{c['id']}.json"
        if t not in out and p.exists():
            out[t] = close_series(json.loads(p.read_text())["daily"], 4)
        if t in out:
            out[t] = out[t].copy()
            out[t][session_date] = c["price"]
            out[t] = out[t].sort_index()
    return out


def attribute(nati: pd.Series, dates: list[str], reviews: list[dict], prices: dict[str, pd.Series]) -> tuple[dict, dict]:
    """Index points each company added on each session in `dates`, summed: the weight it carried into the
    session (its review weight, drifted with its price since the review) × its move × the index level.
    Returns ({ticker: points}, {ticker: (first session held, last)}), exact across reviews."""
    pts: dict[str, float] = {}
    held: dict[str, list[str]] = {}
    for p, d in zip(dates[:-1], dates[1:]):
        rev = max((r for r in reviews if r["effective"] <= d), key=lambda r: r["effective"])
        w, moves = {}, {}
        for t, w0 in rev["weights"].items():
            px = prices.get(t)
            if px is None or p not in px.index or d not in px.index or rev["cutoff"] not in px.index:
                continue
            w[t] = w0 * px[p] / px[rev["cutoff"]]
            moves[t] = px[d] / px[p] - 1
        tot = sum(w.values())
        for t in w:
            pts[t] = pts.get(t, 0.0) + w[t] / tot * moves[t] * nati[p]
            held.setdefault(t, [d, d])[1] = d
    return pts, {t: tuple(v) for t, v in held.items()}


def window_summary(n: int, nati: pd.Series, reviews, prices, names: dict, series: dict, corr_line: str) -> dict:
    label, span = WINDOWS[n]
    dates = list(nati.index)
    win = dates[-n - 1:]
    start, end = win[0], win[-1]
    w = {"sessions_n": n, "label": label, "span": span, "start": start, "end": end,
         "start_level": round(nati[start], 2), "end_level": round(nati[end], 2),
         "change_pct": round(nati[end] / nati[start] - 1, 6), "change_pts": round(nati[end] - nati[start], 2),
         "sessions": [{"date": d, "close": round(nati[d], 2), "change_pct": round(nati[d] / nati[p] - 1, 6)}
                      for p, d in zip(win[:-1], win[1:])]}
    bench = []
    for b in BENCH:
        s = close_series(series[b]["daily"], 1)
        # the last close on or before each end of the window (US indices close after Indian markets)
        a, z = s[s.index <= start], s[s.index <= end]
        if len(a) and len(z):
            bench.append({"id": b, "name": series[b]["name"], "change_pct": round(z.iloc[-1] / a.iloc[-1] - 1, 6),
                          "from": a.index[-1], "to": z.index[-1]})
    w["benchmarks"] = bench

    pts_by, held = attribute(nati, win, reviews, prices)
    joined = [t for t, (first, _) in held.items() if first != win[1]]
    rows = []
    for t, pt in pts_by.items():
        first, last = held[t]
        px = prices[t]
        before = win[win.index(first) - 1]
        meta = names.get(t, {"company": t.split(".")[0].title(), "sector": "Other"})
        rows.append({"id": t.replace(".NS", "_NS").replace(".BO", "_BO"), **meta, "points": pt,
                     "ret": px[last] / px[before] - 1, "since": first if t in joined else None, "current": last == end})
    implied = sum(r["points"] for r in rows)
    lift = sorted([r for r in rows if r["points"] > 0], key=lambda r: -r["points"])[:5]
    drag = sorted([r for r in rows if r["points"] < 0], key=lambda r: r["points"])[:5]
    cur = [r for r in rows if r["current"]]
    by_ret = sorted(cur, key=lambda r: -r["ret"])
    out_row = lambda r: {"id": r["id"], "company": r["company"], "sector": r["sector"], "change_pct": round(r["ret"], 6),
                         "points": round(r["points"], 2), **({"since": r["since"]} if r["since"] else {})}
    sectors = {}
    for r in rows:
        s = sectors.setdefault(r["sector"], {"sector": r["sector"], "points": 0.0, "count": 0})
        s["points"] += r["points"]; s["count"] += 1
    sec = sorted(sectors.values(), key=lambda s: s["points"])
    w.update({
        "breadth": {"up": int(sum(r["ret"] > 0 for r in cur)), "down": int(sum(r["ret"] < 0 for r in cur)), "total": len(cur)},
        "lifted": [out_row(r) for r in lift], "dragged": [out_row(r) for r in drag],
        "gainers": [out_row(r) for r in by_ret[:3] if r["ret"] > 0],
        "losers": [out_row(r) for r in by_ret[::-1][:3] if r["ret"] < 0],
        "sectors": [{"sector": s["sector"], "points": round(s["points"], 2), "count": s["count"]} for s in sec],
        "joined": [{"id": r["id"], "company": r["company"], "since": r["since"]} for r in rows if r["since"]],
        "attribution_check": {"implied_pts": round(implied, 2), "actual_pts": w["change_pts"]},
    })

    # ---------------- headlines: fixed sentences filled from the numbers above ----------------
    nifty_w = next((b for b in bench if b["id"] == "NIFTY_50"), None)
    ch = w["change_pct"]
    lines = []
    vs = ""
    if nifty_w:
        gap = (ch - nifty_w["change_pct"]) * 100
        vs = (f", {'outperforming' if gap > 0 else 'underperforming'} the NIFTY 50 ({pct(nifty_w['change_pct'])}) "
              f"by {abs(gap):.1f} percentage points")
    lines.append(f"NATI {'rose' if ch > 0 else 'fell'} {pct(abs(ch))[1:]} over the past {n} sessions to "
                 f"{w['end_level']:,.2f}{vs}.")
    b = w["breadth"]
    lines.append(f"{b['up']} of {b['total']} constituents rose over {span} and {b['down']} fell.")
    if drag and lift:
        big = drag[0] if abs(drag[0]["points"]) >= lift[0]["points"] else lift[0]
        other = lift[0] if big is drag[0] else drag[0]
        side = "biggest lift" if other is lift[0] else "biggest drag"
        lines.append(f"{big['company']} ({pct(big['ret'])}) moved the index most, {pts(big['points'])}. "
                     f"The {side} came from {other['company']} ({pct(other['ret'])}, {pts(other['points'])}).")
    elif drag or lift:
        top = (drag or lift)[0]
        lines.append(f"{top['company']} ({pct(top['ret'])}) moved the index most, {pts(top['points'])}.")
    if sec:
        worst, best = sec[0], sec[-1]
        if worst["points"] < 0 and best["points"] > 0:
            lines.append(f"By sector, {worst['sector']} weighed most ({pts(worst['points'])}) and {best['sector']} helped most ({pts(best['points'])}).")
        elif worst["points"] < 0:
            lines.append(f"Every sector lost ground; {worst['sector']} weighed most ({pts(worst['points'])}).")
        elif best["points"] > 0:
            lines.append(f"Every sector gained; {best['sector']} helped most ({pts(best['points'])}).")
    if w["joined"]:
        names_j = ", ".join(j["company"] for j in w["joined"])
        lines.append(f"{names_j} joined at the review effective {dstr(w['joined'][0]['since'])}; their moves count from then.")
    if corr_line:
        lines.append(corr_line)
    w["headlines"] = lines
    return w


def dstr(s: str) -> str:
    d = datetime.strptime(s, "%Y-%m-%d")
    return f"{d.day} {d:%b %Y}"


def main() -> int:
    idx = json.loads((DATA / "index.json").read_text())
    cons = json.loads((DATA / "constituents.json").read_text())
    reviews = json.loads((DATA / "reviews.json").read_text())
    series = {b: json.loads((DATA / "series" / f"{b}.json").read_text()) for b in BENCH}
    nati = close_series(idx["daily"], 4)
    dates = list(nati.index)
    prices = load_prices(cons, idx["session_date"])
    universe = {r["ticker"]: {"company": r["company"], "sector": r["sector"]} for r in csv.DictReader(open(V2.parent / "config" / "universe.csv"))}
    for c in cons:                                  # the index build's names win where they differ
        universe[c["id"].replace("_NS", ".NS").replace("_BO", ".BO")] = {"company": c["company"], "sector": c["sector"]}

    # ---------------- correlation ----------------
    r_nati = nati.pct_change()
    nifty = close_series(series["NIFTY_50"]["daily"], 1)
    r_nifty = nifty.reindex(dates).ffill().pct_change()
    # NASDAQ-100 trades overnight for India: pair each Indian session with the last US close
    # strictly before it, the information the Indian market had when it opened
    ndx = close_series(series["NASDAQ_100"]["daily"], 1)
    prev_us = pd.Series({d: ndx[ndx.index < d].iloc[-1] for d in dates if (ndx.index < d).any()})
    r_ndx = prev_us.reindex(dates).pct_change()
    frame = pd.DataFrame({"nati": r_nati, "nifty": r_nifty, "ndx": r_ndx}).dropna()
    frame.index = pd.to_datetime(frame.index)
    quarters = []
    for q, g in frame.groupby(frame.index.to_period("Q")):
        quarters.append({"quarter": f"Q{q.quarter} {q.year}", "months": f"{q.start_time:%b}–{q.end_time:%b}",
                         "sessions": len(g), "partial": len(g) < MIN_QUARTER_SESSIONS or q == frame.index[-1].to_period("Q"),
                         "nifty50": round(g["nati"].corr(g["nifty"]), 3), "nasdaq100": round(g["nati"].corr(g["ndx"]), 3)})
    yr = frame[frame.index > frame.index[-1] - pd.Timedelta(days=365)]
    corr = {"quarters": quarters, "past_year": {"sessions": len(yr), "nifty50": round(yr["nati"].corr(yr["nifty"]), 3),
            "nasdaq100": round(yr["nati"].corr(yr["ndx"]), 3)},
            "method": "Pearson correlation of daily percentage changes. NASDAQ-100 uses the previous US session's close, "
                      "which is the last US move Indian markets have seen when they open."}
    full = [q for q in quarters if not q["partial"]]
    corr_line = ""
    if full:
        q = full[-1]
        corr_line = (f"In {q['quarter']} NATI moved {strength(q['nifty50'])} with the NIFTY 50 (correlation {q['nifty50']:.2f}) "
                     f"and {strength(q['nasdaq100'])} with the NASDAQ-100 ({q['nasdaq100']:.2f}).")

    windows = {str(n): window_summary(n, nati, reviews, prices, universe, series, corr_line) for n in WINDOWS}
    doc = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "as_of": idx["as_of"],
           "windows": windows, "default_window": "5", "correlation": corr,
           # the 5-session view, as earlier readers of this file expect it
           "window_sessions": 5, "headlines": windows["5"]["headlines"], "week": windows["5"]}
    write_json(DATA / "insights.json", doc)
    for k, w in windows.items():
        chk = w["attribution_check"]
        print(f"[{k} sessions] " + w["headlines"][0])
        print(f"   attribution: {chk['implied_pts']:+.2f} pts from companies vs {chk['actual_pts']:+.2f} pts actual"
              + (f"; joined: {', '.join(j['company'] for j in w['joined'])}" if w["joined"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
