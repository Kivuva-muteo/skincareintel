# SkincareIntel

A Python monitoring agent that tracks 11 skincare and beauty brands in the Kenyan market and sends alerts when something important appears: product controversies, regulatory notices, viral reviews and price or reputation signals.

It collects mentions from several public sources, removes duplicates, scores how serious each item is, and delivers the important ones to Telegram, Slack or email, with a weekly summary report.

## Features

- **Multi-source monitoring** across RSS feeds, the YouTube Data API v3, Reddit, Kenyan influencer channels, Instagram (optional) and Kenyan regulatory sources
- **Noise reduction** using Jaccard-similarity deduplication of near-identical items and alert deduplication so the same story is not sent twice
- **Severity scoring and trending** so a growing issue ranks higher than a one-off mention
- **Alert delivery** by Telegram, Slack and email, with minimum-severity thresholds and per-run alert limits
- **Weekly reports** in PDF and HTML
- **Optional AI verification** of higher-severity alerts to reduce false positives (disabled by default)
- **Run health summary** at the end of each run, plus a self-test script

## How it works

```
 RSS | YouTube | Reddit | Influencers | Instagram | Regulators
                          |
                     agent.py  (orchestrator)
                          |
        deduplicate -> score severity -> trend over time
                          |
              Telegram | Slack | Email | Weekly report
```

## Project structure

| File | Purpose |
|---|---|
| `agent.py` | Main orchestrator that runs all monitors and sends alerts |
| `youtube_monitor.py` | YouTube search and view-count monitoring |
| `reddit_monitor.py` | Reddit mention monitoring |
| `Instagram_monitor.py` | Instagram monitoring (optional) |
| `influencer_monitor.py` | Kenyan influencer channel monitoring |
| `kenya_regulatory_monitor.py` | Regulatory and recall notices |
| `weekly_report.py` / `weekly_state.py` | Weekly PDF/HTML report and its state tracking |
| `self_test.py` | Quick checks that the setup works |
| `config.json` | Brands, keywords and general settings |
| `*.example.json`, `secrets.example.bat` | Templates for your own keys and settings |
| `run.bat` | Windows launcher |

## Getting started

**Requirements:** Python 3.9 or newer.

1. **Clone the repo and install dependencies**
```bash
   git clone https://github.com/Kivuva-muteo/skincareintel.git
   cd skincareintel
   pip install -r requirements.txt
```

2. **Create your config files.** Copy each example file and fill in only the services you want to use:
```bash
   copy youtube_config.example.json youtube_config.json
   copy telegram_config.example.json telegram_config.json
   copy email_config.example.json email_config.json
   copy ai_config.example.json ai_config.json
   copy instagram_config.example.json Instagram_config.json
   copy secrets.example.bat secrets.bat
```
   On
