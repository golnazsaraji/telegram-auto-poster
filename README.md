# Telegram Content Automation Bot

This bot finds fresh, relevant content from RSS feeds, web search, and YouTube, then posts it into the configured topics of your Telegram forum group.

## Setup

1. Create and activate a virtual environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies.

```bash
pip install -r requirements.txt
```

3. Create your `.env` file.

```bash
cp .env.example .env
```

4. Edit `.env` and set `BOT_TOKEN`, `CHAT_ID`, and your Telegram forum topic IDs.

## Test Without Posting

```bash
python bot.py --once --dry-run
```

This searches sources and prints the messages that would be posted.

## Post Once

```bash
python bot.py --once
```

## Run Continuously

```bash
python bot.py
```

By default it runs every 180 minutes and posts up to 3 fresh items per run. Change `POST_INTERVAL_MINUTES` and `MAX_POSTS_PER_RUN` in `.env` if needed.

If your local Python reports certificate verification errors during testing, install/update `certifi` with the requirements command above. For a temporary local-only dry run, you can set `SSL_VERIFY=false` in `.env`.

## YouTube Search

If `YOUTUBE_API_KEY` is set, the bot uses the official YouTube Data API. Without it, the bot falls back to parsing public YouTube search results.

## Custom Sources

You can override or add sources with a JSON config file and point `CONTENT_CONFIG` to it in `.env`.

```json
{
  "jobs": {
    "queries": ["English speaking software jobs Turin"],
    "rss_feeds": ["https://remoteok.com/remote-python-jobs.rss"],
    "allowed_domains": ["remoteok.com", "linkedin.com", "indeed.com"]
  }
}
```

The bot tracks posted URLs in `posted_items.json` so it does not repeat the same content.

## Publishing Publicly

Do not commit `.env`, `posted_items.json`, local virtual environments, or Telegram API/update dumps. The included `.gitignore` excludes those by default.
