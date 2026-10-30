# Inc42 New-Age Tech Index (NATI) widget

An embeddable, self-updating widget for the **Inc42 New-Age Tech Index**: 50 Indian new-age tech companies
listed on the NSE or BSE mainboard, weighted by free-float market cap with a 15% cap per company, rebalanced
every quarter, base 1,000 on 31 December 2024. Built by Inc42 Datalabs.

It is a static site (HTML, CSS, JavaScript and JSON files, no server code), so GitHub Pages can host it. A
GitHub Actions workflow rebuilds the data from Yahoo Finance every 20 minutes during market hours and
republishes it.

## What the widget does

- **Index chart**: ranges from 1D to MAX; Area, Line, Candle and Bar; zoom and pan; moving average, MACD and
  envelope indicators; previous-close line on intraday ranges.
- **Compare**: NATI against up to four indices (NIFTY 50, SENSEX, NIFTY Next 50, S&P 500, NASDAQ-100, NASDAQ)
  or any of its 50 constituents, with each line's change in the legend.
- **Overview**: day and 52-week stats, market caps, risk and return over the past year (each metric with an
  (i) explainer), sector weights.
- **Constituents**: a heatmap sized by index weight, coloured by return over 1D, 1W, 1M, YTD or 1Y, grouped by
  sector or by return band; or a sortable, filterable table.
- **Key Insights**: what moved the index over the past 5, 10 or 20 sessions, against benchmarks, day by day,
  by company and by sector (exact attribution in index points), plus quarterly correlation with NIFTY 50 and
  NASDAQ-100. The text is generated from the numbers by fixed templates, so it is always consistent with them.
- **Company profiles** (`company.html`) for all 50 constituents: price chart with NATI comparison, key stats,
  consolidated financials in ₹ crore (annual and quarterly), and the latest Inc42 stories, scanned weekly.
- **Built for articles**: reports its height so the iframe never scrolls inside; follows `?theme=light|dark`
  from the first paint; vertical swipes over charts scroll the article; keeps the reader's choices when it
  refreshes or moves between pages; light and dark themes; works from 320px phones up.

## Folder layout

```
.github/workflows/
  refresh.yml          every 20 min in market hours: rebuild data, publish v2/site; Mondays: full profile rebuild
  yahoo-check.yml      run once by hand: can GitHub's servers download Yahoo prices?
nati_ff.py             builds the index from Yahoo Finance prices → site/data/ (not committed)
config/universe.csv    every candidate company: ticker, sector, listing date, shares, free float
requirements.txt       pinned Python packages (tested on Python 3.14)
v2/
  site/                THE WIDGET: this folder is what GitHub Pages serves
    index.html         the index widget
    company.html       company profiles (company.html?id=ETERNAL_NS)
    assets/nati-ui.css shared design tokens, light and dark
    vendor/            TradingView Lightweight Charts 5.0.9 (Apache 2.0, licence included)
    data/              JSON the pages read; rebuilt on every run
      companies/       one file per company, plus index.json (list and period returns)
  refresh_v2.py        after nati_ff.py: copies the index data, refreshes company prices, rebuilds insights
  build_profiles.py    company profiles: full build (weekly), --prices-only (every run), --news-only
  build_insights.py    Key Insights (data/insights.json)
  config/profile_aliases.csv   names and brands each company's Inc42 stories are matched on
embed/
  wordpress-embed.html paste into a WordPress Custom HTML block
  amp-embed.html       the AMP version
  embed-test.html      local test page
```

## Put it on GitHub (about 15 minutes)

1. **Create the repository.** On GitHub, create a new **public** repository, for example `nati-widget`
   (GitHub Pages on a private repository needs a paid plan). Do not add a README or licence there.
2. **Push this folder.** In a terminal, inside this folder:
   ```bash
   git init -b main
   git add .
   git commit -m "NATI widget v2"
   git remote add origin https://github.com/YOUR-USERNAME/nati-widget.git
   git push -u origin main
   ```
3. **Turn on Pages.** Repository → Settings → Pages → Build and deployment → Source: **GitHub Actions**.
4. **Allow the weekly commit.** Settings → Actions → General → Workflow permissions: **Read and write
   permissions** → Save.
5. **Check Yahoo access.** Actions → *Yahoo Finance access check* → Run workflow. It should end with "Yahoo
   access works from GitHub Actions". If it fails, see *If Yahoo blocks GitHub* below.
6. **First publish.** Actions → *Refresh and deploy* → Run workflow. When it finishes (3–5 minutes), the
   widget is live at `https://YOUR-USERNAME.github.io/nati-widget/`.
7. **Set up the outside scheduler.** GitHub's own schedule can be hours late or skip runs entirely, so a free
   [cron-job.org](https://cron-job.org) job starts each refresh instead (see *Reliable refresh schedule* below).
   The workflow's own schedule stays on as a backup.

## Embed it in an Inc42 article

Use `embed/wordpress-embed.html`: replace `https://YOUR-USERNAME.github.io/YOUR-REPO/` (two places) with the
widget's address and paste the block into a Custom HTML block. Use `embed/amp-embed.html` for AMP pages.

What the Inc42 tech team may need to do (small):

- **Allow framing**: if inc42.com sends a Content-Security-Policy with `frame-src` or `child-src`, add the
  widget's address (for example `https://YOUR-USERNAME.github.io`).
- **Unfiltered HTML**: the embed's small script (it sizes the frame) needs an editor or administrator account.
  Without it the widget still works, at a fixed height with an inner scrollbar.
- **Optional custom domain** (recommended): a DNS CNAME such as `nati.inc42.com` → `YOUR-USERNAME.github.io`,
  then Settings → Pages → Custom domain. Articles then embed an Inc42 address, and the widget can later move to
  another host or an Inc42 GitHub organisation without editing a single article.

Each page loads its data from `data/` next to it; nothing else needs configuring. Fonts come from Google Fonts.

## How the data flows

```
Yahoo Finance ──► nati_ff.py ──► site/data/ (index, constituents, compare series) + .cache/ (raw prices)
                                      │
                                      ▼
                        v2/refresh_v2.py: copies site/data into v2/site/data
                                          build_profiles.py --prices-only (company prices from .cache/)
                                          build_insights.py (Key Insights)
                                      │
                                      ▼
                        v2/site/ ──► GitHub Pages
Weekly: build_profiles.py (full): Yahoo financials and history, Inc42 stories ──► v2/site/data/companies (committed)
```

- `nati_ff.py` validates every build; if a check fails it writes nothing and the workflow stops, so the site
  keeps its last good data.
- Every file is written to a temporary name and renamed, so a reader never gets half a file.
- An open widget looks for a new build every 5 minutes and keeps the reader's range, comparisons, tab and zoom.
- Company stories: inc42.com WordPress search, current month first, headlines naming the company or one of its
  brands (`v2/config/profile_aliases.csv`), about one request a second.

## Run it on your own computer

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python nati_ff.py              # 2–4 minutes: downloads prices, builds the index
.venv/bin/python v2/refresh_v2.py        # seconds: updates the widget's data
.venv/bin/python -m http.server 8000     # then open http://localhost:8000/v2/site/
```

`v2/site/data/` already holds a working snapshot, so the last command alone is enough to look at the widget.
`embed/embed-test.html` (open it as `http://127.0.0.1:8000/embed/embed-test.html`) shows it framed in an article.

## Looking after it

- **New listings and share data**: edit `config/universe.csv` (company, ticker, sector, listing date, shares,
  free float). Quarterly reviews (last trading day of March, June, September, December) apply automatically.
- **A new constituent**: gets a full profile automatically on the next run. Add its brand names to
  `v2/config/profile_aliases.csv` so its stories are found (`ID,Name|Brand|Other name`).
- **A run failed**: Actions shows the log. Most failures are Yahoo hiccups and the next run recovers.
- **Schedule paused**: GitHub pauses scheduled workflows in a public repository after 60 days without
  activity. The Monday profile commit keeps it active; if it ever pauses, Actions → *Refresh and deploy* →
  Enable workflow.

### Reliable refresh schedule

Two cron-job.org jobs start the workflow through GitHub's API. Both use `Asia/Kolkata` time and POST to:

```
https://api.github.com/repos/YOUR-USERNAME/nati-widget/actions/workflows/refresh.yml/dispatches
```

| Job | Schedule | Crontab | Request body |
|---|---|---|---|
| NATI refresh | Weekdays, 09:05 to 16:45 IST, every 20 min | `5,25,45 9-16 * * 1-5` | `{"ref":"main"}` |
| NATI weekly rebuild | Mondays, 07:30 IST | `30 7 * * 1` | `{"ref":"main","inputs":{"full_rebuild":"true"}}` |

Each sends these headers: `Accept: application/vnd.github+json`, `Authorization: Bearer YOUR-TOKEN`,
`X-GitHub-Api-Version: 2022-11-28` and `Content-Type: application/json`. A successful call returns 204.

The token is a GitHub fine-grained personal access token for this repository only, with **Actions: Read and
write**. When it expires the refresh stops, so create a new one and update both jobs before then. The same
applies if it leaks: delete it in GitHub first.

### If Yahoo blocks GitHub

Yahoo Finance sometimes blocks cloud servers. If the access check fails, or refreshes keep failing, run the
data build on a computer you control and let GitHub host only the files: a scheduled job runs
`nati_ff.py` and `v2/refresh_v2.py`, then commits `v2/site/data` and pushes. In `refresh.yml`, replace the
schedule with `on: push` and delete the three data-building steps, so each push publishes the site.

## Limits to know

- GitHub runs scheduled workflows on a best-effort basis: in this repository's first day they ran hours late
  or not at all. Refreshes therefore rely on the outside scheduler; without it, expect long gaps.
- GitHub Pages has a soft bandwidth limit of about 100 GB a month. A first view of the widget is roughly
  150–250 KB, so that covers several hundred thousand views. For more, Cloudflare Pages can serve this same
  repository with unlimited bandwidth.
- Prices are delayed and come from Yahoo Finance; financials are consolidated figures as Yahoo reports them,
  and some quarters are missing at the source (the profiles show "—" and say so).

## Data and disclaimer

Prices and financials via Yahoo Finance, delayed. Stories from inc42.com. Charts by TradingView Lightweight
Charts™. For information only, not investment advice. Inc42 does not rate or recommend securities.
