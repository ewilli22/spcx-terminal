# SPCX Terminal

A free, self-hosted news and price monitor for **SpaceX (SPCX)** and the broad market, inspired by Fincept Terminal but focused on one job: tell you when something happens that could move SpaceX stock or the market.

- **Runs free on GitHub Actions** every 10 minutes. No server, no API keys, no paid data.
- **Emails you** (Gmail) when a headline scores above your threshold, when SPCX or the indexes make a big move, and once a day after the close with a digest.
- **Terminal-style dashboard** on GitHub Pages: SPCX price and intraday chart, indexes, VIX and yields, a scored news feed, and a countdown to upcoming catalysts (FOMC, CPI, PCE, SPCX unlocks and earnings).

## What it watches

| Source | Why |
|---|---|
| Google News: SpaceX/SPCX, Starship/Starlink, xAI/Grok | Company and launch news (SPCX includes xAI since the Feb 2026 acquisition) |
| SEC EDGAR filings for SPCX | 8-Ks, 10-Qs, insider and offering filings, often the first signal |
| Yahoo Finance headlines for SPCX and the indexes | Analyst actions, earnings coverage |
| Federal Reserve press releases | FOMC statements, rate decisions |
| CNBC Markets, MarketWatch, Google News macro queries | Fed, CPI, jobs, tariffs, selloffs |
| Yahoo quotes: SPCX, S&P 500, Nasdaq, Dow, Russell, VIX, 10Y, TSLA, RKLB, ASTS, BTC | Price-move alerts and the dashboard |

Each headline gets an **impact score** from keyword weights in `config.json` (for example: `explod` 6, `faa` 4, `8-k` 6, `lockup` 6, `downgrade` 5, `rate hike` 7, `circuit breaker` 9). SpaceX stories alert at score 6+, market stories at 8+. Duplicate stories from multiple outlets are collapsed into one.

**Price alerts** (market hours only, once per level per day): SPCX moves of 4%, 8%, 12%; S&P 500 1.5% and 3%; Nasdaq 2% and 4%; VIX up 20%+ or above 25 / 35; 10Y yield moves of 3%+.

## Setup (about 15 minutes)

### 1. Create the repo
1. Sign in to GitHub (free account is fine) and create a **new public repository**, for example `spcx-terminal`.
   Public matters: GitHub Actions minutes and GitHub Pages are free and unlimited for public repos. Your secrets stay private; only headlines and quotes are visible.
2. Upload everything in this folder. The easiest way is **Add file > Upload files** and drag the folder contents in.
   Make sure the hidden `.github` folder comes along (on Mac press `Cmd+Shift+.` in Finder to show it; on Windows enable "Hidden items" in Explorer's View menu). If it doesn't upload, create the file `.github/workflows/monitor.yml` by hand with **Add file > Create new file** and paste its contents.

### 2. Create a Gmail app password
1. Go to your Google Account > **Security** and make sure **2-Step Verification** is on.
2. Search the account settings for **App passwords**, create one named `SPCX Terminal`, and copy the 16-character password.

### 3. Add the secrets to GitHub
In the repo: **Settings > Secrets and variables > Actions > New repository secret**. Add:

| Name | Value |
|---|---|
| `GMAIL_ADDRESS` | your Gmail address (sends the alerts) |
| `GMAIL_APP_PASSWORD` | the 16-character app password |
| `ALERT_TO` | optional: where alerts go if not the same address (comma-separate several) |

### 4. Turn on the dashboard
**Settings > Pages > Build and deployment**: Source **Deploy from a branch**, Branch **main**, folder **/docs**. Save. Your dashboard will be at `https://YOUR-USERNAME.github.io/spcx-terminal/`.

### 5. Start it
**Actions** tab > enable workflows if asked > **SPCX monitor** > **Run workflow**.
Within a minute you should get a "Monitor is live" email. The first run indexes current headlines without alerting so you don't get flooded; real alerts start on the next run.

## Tuning

Everything lives in `config.json`, editable right on GitHub:

- **Too many emails?** Raise `thresholds.spacex` / `thresholds.market`, or lower individual keyword weights.
- **Missing something?** Add keywords with a weight, or add a feed. Any Google News search works as a feed:
  `https://news.google.com/rss/search?q=YOUR+TERMS+when:1d&hl=en-US&gl=US&ceid=US:en`
- **Quiet the digest:** set `email.daily_digest` to `false`.
- **Catalysts:** edit the `catalysts` list. Entries marked `"confirmed": false` show an UNCONFIRMED tag on the dashboard. The SPCX unlock and earnings dates are from third-party trackers and a placeholder, so update them when SpaceX announces its Q3 earnings date or you verify the lockup terms in the prospectus.

## Notes and limits

- **Not real-time.** Checks run every 10 minutes and GitHub sometimes delays scheduled runs at busy times, so expect alerts within roughly 10 to 20 minutes of a headline. Fine for news awareness, not for trading the first tick.
- **Keyword scoring is crude.** It catches the obvious market movers well; it can't judge nuance. The "why" line in each alert shows which words triggered it, which makes tuning easy.
- **Free feeds can change.** The dashboard's Feed Health panel shows any source that stops responding; swap its URL in `config.json`.
- GitHub pauses scheduled workflows in repos with no activity for 60 days. The bot commits its data every run, which normally counts as activity; if GitHub ever emails you that the workflow was disabled, just re-enable it in the Actions tab.
- Automated headline alerts, not investment advice.

## Run it locally (optional)

```bash
python bot/monitor.py --dry-run     # prints emails instead of sending
cd docs && python -m http.server    # then open http://localhost:8000
```

Python 3.9+ standard library only, nothing to install.
