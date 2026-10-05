# SkincareIntel — Fixed Build

This build repairs the broken Instagram integration, reduces false positives, improves Reddit resilience, and removes credentials and generated data from the distributable package.

## Important security action

The original RAR contained active-looking credentials. Rotate these before using the project again:

- Instagram password/session
- Gmail App Password
- Telegram bot token
- YouTube API key
- Anthropic/Claude API key

The fixed ZIP contains blank credential fields only.

## Setup on Windows

1. Install Python 3.10 or newer.
2. Open Command Prompt inside this folder.
3. Install dependencies:

```bat
python -m pip install -r requirements.txt
```

4. Copy the secrets template:

```bat
copy secrets.example.bat secrets.bat
```

5. Open `secrets.bat` and add only the credentials for services you use.
6. Run the offline checks:

```bat
python self_test.py
```

7. Start the agent:

```bat
run.bat
```

`run.bat` automatically loads `secrets.bat` when it exists.

## Service notes

### Instagram

- The module and config are now correctly named `instagram_monitor.py` and `instagram_config.json`.
- Social RSS monitoring works without an Instagram login.
- Direct Instagram monitoring requires `instaloader` and either:
  - `INSTAGRAM_USERNAME` and `INSTAGRAM_PASSWORD` in `secrets.bat`, or
  - credentials in `instagram_config.json` with `instagram_login.enabled` set to `true`.
- A saved `instagram_session` can be reused without storing the password.

### YouTube

Set `YOUTUBE_API_KEY` in `secrets.bat`. The agent now requires a real brand/alias match in the video title, description, or channel before accepting a search result. Ambiguous names such as **The Ordinary** require skincare/product context.

### Reddit

The monitor now:

- retries conservatively after rate limits;
- tries both standard and old Reddit public JSON endpoints;
- falls back to subreddit RSS for post discovery;
- can use an approved Reddit application token or application-only OAuth credentials from `secrets.bat`;
- scores post text when comments are unavailable.

Public Reddit access can still be blocked depending on the network/IP. For dependable access, use credentials authorized by Reddit and comply with Reddit’s current Data API terms.

### Email/Gmail

Set these values in `secrets.bat`:

- `SMTP_USER`
- `SMTP_PASS`
- `EMAIL_FROM`
- `EMAIL_TO`

For Gmail, `SMTP_PASS` must be a Google App Password created after enabling 2-Step Verification. Do not use the normal Gmail password.

### Telegram

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Start a private chat with the bot before the first alert, or add it to the target group. The code now sends via POST and gives a clearer message for HTTP 403 errors.

## Configuration files

Sensitive local config files are intentionally ignored by Git. Matching `.example.json` files are included as safe templates.

Generated files are also ignored:

- `agent.log`
- `state.json`
- `weekly_state.json`
- `reports/`
- Python cache files

## Main fixes included

- Fixed Python `true`/`True` error in Instagram defaults.
- Fixed Instagram filename/config case mismatch.
- Removed hard-coded Instagram credentials.
- Added environment-variable credential support.
- Prevented YouTube search noise from being accepted without a validated brand mention.
- Prevented rejected YouTube results from being marked as seen prematurely.
- Tightened KEBS/PPB filtering to exclude unrelated medicine, condom, and general-product recalls.
- Added stricter word-boundary and contextual brand matching across RSS, Reddit, regulatory, Instagram, and YouTube sources.
- Added Reddit OAuth support and RSS fallback.
- Improved Gmail and Telegram error messages.
- Removed old logs, reports, states, sessions, and caches from the shared build.
