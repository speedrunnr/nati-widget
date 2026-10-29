# NATI widget v2: release notes

Inc42 Datalabs · October 2026

## What v2 adds to v1

| Area | v1 | v2 |
|---|---|---|
| Design | Single theme set | New light theme (cool neutrals, bordered cards, contrast ≥ 4.5:1), refined dark theme, segmented controls, loading shimmer, motion that respects reduced-motion settings |
| Compare | 6 indices | 6 indices and all 50 constituents; each line's change shown in the legend; lines start on a common date when a company listed during the range |
| Constituents | Sortable table | Heatmap by index weight, coloured by 1D, 1W, 1M, YTD or 1Y return, grouped by sector or return band; table kept |
| Insights | None | Key Insights tab: past 5, 10 or 20 sessions vs benchmarks, day by day, exact attribution by company and sector, quarterly correlation with NIFTY 50 and NASDAQ-100, plain-language summary |
| Company pages | None | Profiles for all 50: price chart with NATI comparison, key stats, consolidated financials in ₹ crore, Inc42 stories (refreshed weekly, current month first) |
| Explainers | None | (i) notes on Return, Volatility, Sharpe, Sortino, Max drawdown and correlation |
| Reader state | Lost on reload | Range, chart type, comparisons, indicators, tab, heatmap and insights choices kept for the session, through refreshes and page moves |
| Embedding | Height messages | Same, plus a page-change message so the article scrolls to the widget's top when a reader opens a profile |
| Hosting | Static files + server cron | Static files on GitHub Pages, refreshed by GitHub Actions |

## How Key Insights text is written

No AI model. Fixed sentence templates are filled with numbers computed on every refresh (`v2/build_insights.py`),
so every figure in the text matches the charts. Attribution is exact: for each session, a company's points are its
weight going into the session (its review weight, drifted with its price since the review) × its move × the index
level. Summed over 5, 10 or 20 sessions, company points match the index's actual move to within 0.03 points,
including across a quarterly review.

## Final checks (29 October 2026)

- **Data**: all 62 JSON files parse as strict JSON; 50 constituents, 50 profiles, weights sum to 1.000; every
  profile's last close equals its index price; no duplicate or missing days (Yahoo's daily data skipped 28 Oct for 24
  stocks; those days are now filled from 5-minute bars).
- **Widget sweep, no errors**: every range × chart type; indicators on all ranges; comparisons with an Indian index,
  a US index and two companies across all ranges; heatmap 5 periods × 2 groupings; table sorting and "show all";
  Key Insights 3 periods; all (i) explainers; all tabs.
- **Profiles**: all 50 loaded at 360px in light and dark, each exercised across ranges, vs NATI, candles and the
  annual/quarterly switch: no errors, no horizontal overflow, stories present for every company.
- **Layouts**: 320/360/412px phones, 720px article column, desktop; light and dark.
- **Embedding**: cross-origin iframe in a test article at phone and desktop widths; `nati:height`, AMP
  `embed-size` and `nati:page` messages; frame grows and shrinks with tabs and profiles.
- **Package**: installed from `requirements.txt` into a fresh environment and run end to end from a clean copy
  (`nati_ff.py` → `v2/refresh_v2.py` → full profile rebuild for two companies).

## Known limits

- Not yet tested on physical iPhones or Android phones (tested in a Chromium browser at phone sizes). iOS 16.2 or
  later is needed for some colour blending in the stylesheet; older versions fall back to plainer fills.
- Yahoo Finance may limit requests from GitHub's servers; run the *Yahoo Finance access check* workflow first.
- Yahoo is missing some quarters of financials (shown as "—" with a note); P/E is blank for loss-makers.
- Scheduled GitHub runs can start late at busy times.
