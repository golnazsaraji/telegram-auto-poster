import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from dotenv import load_dotenv
from telegram import Bot


load_dotenv(".env")

CHAT_ID = int(os.getenv("CHAT_ID", "-1003492949456"))
STATE_FILE = Path(os.getenv("STATE_FILE", "posted_items.json"))
POST_INTERVAL_MINUTES = int(os.getenv("POST_INTERVAL_MINUTES", "180"))
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
SSL_VERIFY = os.getenv("SSL_VERIFY", "true").lower() not in {"0", "false", "no"}

TOPICS = {
    "jobs": int(os.getenv("TOPIC_JOBS", "5")),
    "career": int(os.getenv("TOPIC_CAREER", "7")),
    "events": int(os.getenv("TOPIC_EVENTS", "6")),
    "channels": int(os.getenv("TOPIC_CHANNELS", "597")),
    "soft_skills": int(os.getenv("TOPIC_SOFT_SKILLS", "694")),
}

DEFAULT_TOPIC_CONFIG = {
    "jobs": {
        "title": "Jobs",
        "queries": [
            "English speaking jobs Turin",
            "junior developer jobs Turin",
            "international jobs Torino",
        ],
        "rss_feeds": [
            "https://remoteok.com/remote-python-jobs.rss",
        ],
        "allowed_domains": [
            "linkedin.com",
            "indeed.com",
            "glassdoor.com",
            "remoteok.com",
            "euraxess.ec.europa.eu",
            "eures.europa.eu",
        ],
    },
    "career": {
        "title": "Career",
        "queries": [
            "career advice international students Italy",
            "CV interview tips Europe English",
            "networking tips job seekers Turin",
        ],
        "rss_feeds": [
        ],
        "allowed_domains": [
            "hbr.org",
            "themuse.com",
            "indeed.com",
            "linkedin.com",
            "forbes.com",
            "eures.europa.eu",
        ],
        "youtube_queries": [
            "career advice international students",
            "CV interview tips English",
            "networking tips job seekers",
        ],
    },
    "events": {
        "title": "Events",
        "queries": [
            "Turin career fair English event",
            "Torino startup event English",
            "Turin networking event students",
        ],
        "rss_feeds": [],
        "allowed_domains": [
            "eventbrite.it",
            "meetup.com",
            "polito.it",
            "unito.it",
            "talentgarden.org",
            "torinotechmap.it",
        ],
    },
    "channels": {
        "title": "Channels",
        "queries": [],
        "rss_feeds": [],
        "allowed_domains": ["youtube.com", "youtu.be"],
        "youtube_queries": [
            "career advice international students",
            "job interview English practice",
            "learn Python beginners career",
        ],
    },
    "soft_skills": {
        "title": "Soft Skills",
        "queries": [
            "communication skills workplace article",
            "leadership teamwork soft skills students",
            "public speaking confidence professional article",
        ],
        "rss_feeds": [
        ],
        "allowed_domains": [
            "mindtools.com",
            "hbr.org",
            "coursera.org",
            "edx.org",
            "ted.com",
        ],
        "youtube_queries": [
            "communication skills workplace",
            "public speaking confidence",
            "teamwork leadership skills",
        ],
    },
}


@dataclass(frozen=True)
class ContentItem:
    title: str
    url: str
    source: str
    summary: str = ""
    published: str = ""

    @property
    def stable_id(self) -> str:
        return hashlib.sha256(self.url.strip().lower().encode("utf-8")).hexdigest()


class DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[ContentItem] = []
        self._inside_result = False
        self._current_href = ""
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "a" and attrs_dict.get("class", "").find("result__a") >= 0:
            self._inside_result = True
            self._current_href = attrs_dict.get("href", "") or ""
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._inside_result:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._inside_result:
            title = clean_text(" ".join(self._current_text))
            url = normalize_duckduckgo_url(self._current_href)
            if title and url:
                self.results.append(ContentItem(title=title, url=url, source=domain_name(url)))
            self._inside_result = False
            self._current_href = ""
            self._current_text = []


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = (
        value.replace("â", "-")
        .replace("â", "-")
        .replace("â", "'")
        .replace("â", '"')
        .replace("â", '"')
    )
    return re.sub(r"\s+", " ", value).strip()


def looks_garbled(text: str) -> bool:
    if not text:
        return True
    bad_markers = ("�", "â", "Ã", "å", "é¡", "ï¼", "ã")
    return any(marker in text for marker in bad_markers)


def latin_letter_ratio(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    latin_letters = [
        char
        for char in letters
        if ("A" <= char <= "Z")
        or ("a" <= char <= "z")
        or char in "ÀÁÂÄÈÉÊËÌÍÎÏÒÓÔÖÙÚÛÜàáâäèéêëìíîïòóôöùúûüÇç"
    ]
    return len(latin_letters) / len(letters)


def language_priority(item: ContentItem) -> int | None:
    text = clean_text(f"{item.title} {item.summary}")
    if looks_garbled(text) or latin_letter_ratio(text) < 0.85:
        return None

    words = set(re.findall(r"[a-zà-ÿ']+", text.lower()))
    english_markers = {
        "the",
        "and",
        "for",
        "with",
        "your",
        "job",
        "jobs",
        "career",
        "developer",
        "python",
        "interview",
        "skills",
        "english",
        "remote",
        "students",
        "work",
    }
    italian_markers = {
        "il",
        "lo",
        "la",
        "gli",
        "le",
        "un",
        "una",
        "per",
        "con",
        "di",
        "del",
        "della",
        "carriera",
        "lavoro",
        "torino",
        "italia",
    }
    english_score = len(words & english_markers)
    italian_score = len(words & italian_markers)

    if english_score >= max(1, italian_score):
        return 0
    if italian_score:
        return 1
    return 0


def prioritize_items(items: Iterable[ContentItem]) -> list[ContentItem]:
    prioritized: list[tuple[int, int, ContentItem]] = []
    for index, item in enumerate(dedupe_items(items)):
        priority = language_priority(item)
        if priority is None:
            logging.info("Rejected unreadable/non-English-Italian item: %s", item.title[:100])
            continue
        prioritized.append((priority, index, item))
    return [item for _, _, item in sorted(prioritized, key=lambda entry: (entry[0], entry[1]))]


def normalize_duckduckgo_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//duckduckgo.com/l/?uddg="):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and "uddg=" in parsed.query:
        params = parse_qs(parsed.query)
        return html.unescape(unquote(params.get("uddg", [""])[0]))
    return url


def domain_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def domain_allowed(url: str, allowed_domains: Iterable[str]) -> bool:
    if not allowed_domains:
        return True
    host = domain_name(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


def load_topic_config() -> dict:
    config_path = os.getenv("CONTENT_CONFIG")
    if not config_path:
        return DEFAULT_TOPIC_CONFIG

    with open(config_path, "r", encoding="utf-8") as config_file:
        loaded = json.load(config_file)
    merged = DEFAULT_TOPIC_CONFIG.copy()
    for key, value in loaded.items():
        merged[key] = {**merged.get(key, {}), **value}
    return merged


def load_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as state_file:
            data = json.load(state_file)
            return set(data.get("posted_ids", []))
    except (json.JSONDecodeError, OSError):
        logging.warning("Could not read %s. Starting with an empty post history.", STATE_FILE)
        return set()


def save_state(posted_ids: set[str]) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "posted_ids": sorted(posted_ids),
    }
    with STATE_FILE.open("w", encoding="utf-8") as state_file:
        json.dump(payload, state_file, indent=2)


def fetch_text(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "TelegramContentBot/1.0",
            "Accept": "text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS, context=ssl_context()) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def ssl_context() -> ssl.SSLContext:
    if not SSL_VERIFY:
        logging.warning("SSL verification is disabled. Use this only for local testing.")
        return ssl._create_unverified_context()

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def search_web(query: str, allowed_domains: Iterable[str], limit: int = 5) -> list[ContentItem]:
    search_url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    parser = DuckDuckGoParser()
    try:
        parser.feed(fetch_text(search_url))
        results = [item for item in parser.results if domain_allowed(item.url, allowed_domains)][:limit]
        if results:
            return results
    except Exception as exc:
        logging.warning("DuckDuckGo search failed for %s: %s", query, exc)
    return search_bing_news(query, allowed_domains, limit=limit)


def search_bing_news(query: str, allowed_domains: Iterable[str], limit: int = 5) -> list[ContentItem]:
    feed_url = f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss"
    return read_rss_feed(feed_url, allowed_domains, limit=limit)


def read_rss_feed(feed_url: str, allowed_domains: Iterable[str], limit: int = 5) -> list[ContentItem]:
    xml_text = fetch_text(feed_url)
    root = ElementTree.fromstring(xml_text)
    items = []

    channel_items = root.findall(".//item")
    atom_items = root.findall("{http://www.w3.org/2005/Atom}entry")

    for item in channel_items:
        title = clean_text(item.findtext("title", ""))
        url = clean_text(item.findtext("link", ""))
        summary = clean_text(item.findtext("description", ""))
        published = clean_text(item.findtext("pubDate", ""))
        if title and url and domain_allowed(url, allowed_domains):
            items.append(ContentItem(title=title, url=url, source=domain_name(url), summary=summary, published=published))

    for item in atom_items:
        title = clean_text(item.findtext("{http://www.w3.org/2005/Atom}title", ""))
        link = item.find("{http://www.w3.org/2005/Atom}link")
        url = link.attrib.get("href", "") if link is not None else ""
        summary = clean_text(item.findtext("{http://www.w3.org/2005/Atom}summary", ""))
        published = clean_text(item.findtext("{http://www.w3.org/2005/Atom}published", ""))
        if title and url and domain_allowed(url, allowed_domains):
            items.append(ContentItem(title=title, url=url, source=domain_name(url), summary=summary, published=published))

    return items[:limit]


def search_youtube(query: str, limit: int = 5) -> list[ContentItem]:
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        return scrape_youtube_search(query, limit=limit)

    api_url = (
        "https://www.googleapis.com/youtube/v3/search"
        f"?part=snippet&type=video&maxResults={limit}&order=date&q={quote_plus(query)}&key={api_key}"
    )
    payload = json.loads(fetch_text(api_url))
    results = []
    for item in payload.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        snippet = item.get("snippet", {})
        title = clean_text(snippet.get("title", ""))
        if video_id and title:
            results.append(
                ContentItem(
                    title=title,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    source=clean_text(snippet.get("channelTitle", "YouTube")),
                    summary=clean_text(snippet.get("description", "")),
                    published=clean_text(snippet.get("publishedAt", "")),
                )
            )
    return results


def scrape_youtube_search(query: str, limit: int = 5) -> list[ContentItem]:
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    page = fetch_text(search_url)
    initial_data = extract_yt_initial_data(page)
    if not initial_data:
        return []

    results = []
    for renderer in walk_json(initial_data, "videoRenderer"):
        video_id = renderer.get("videoId")
        title = text_from_runs(renderer.get("title", {}))
        owner = text_from_runs(renderer.get("ownerText", {})) or "YouTube"
        snippets = renderer.get("detailedMetadataSnippets") or [{}]
        description = text_from_runs(snippets[0].get("snippetText", {}))
        if video_id and title:
            results.append(
                ContentItem(
                    title=title,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    source=owner,
                    summary=description,
                )
            )
        if len(results) >= limit:
            break
    return dedupe_items(results)


def extract_yt_initial_data(page: str) -> dict:
    marker = "var ytInitialData = "
    start = page.find(marker)
    if start == -1:
        marker = "ytInitialData = "
        start = page.find(marker)
    if start == -1:
        return {}

    json_start = page.find("{", start + len(marker))
    if json_start == -1:
        return {}

    depth = 0
    in_string = False
    escaped = False
    for index in range(json_start, len(page)):
        char = page[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(page[json_start : index + 1])
                    except json.JSONDecodeError:
                        return {}
    return {}


def walk_json(value, target_key: str):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == target_key and isinstance(child, dict):
                yield child
            else:
                yield from walk_json(child, target_key)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child, target_key)


def text_from_runs(value: dict | list | str) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return clean_text(" ".join(text_from_runs(item) for item in value))
    if not isinstance(value, dict):
        return ""
    if "simpleText" in value:
        return clean_text(value["simpleText"])
    if "runs" in value:
        return clean_text(" ".join(run.get("text", "") for run in value["runs"]))
    return ""


def collect_candidates(topic_name: str, topic: dict) -> list[ContentItem]:
    allowed_domains = topic.get("allowed_domains", [])
    candidates: list[ContentItem] = []

    for feed_url in topic.get("rss_feeds", []):
        try:
            candidates.extend(read_rss_feed(feed_url, allowed_domains))
        except Exception as exc:
            logging.warning("RSS source failed for %s: %s", feed_url, exc)

    for query in topic.get("queries", []):
        try:
            candidates.extend(search_web(query, allowed_domains))
        except Exception as exc:
            logging.warning("Web search failed for %s: %s", query, exc)

    for query in topic.get("youtube_queries", []):
        try:
            candidates.extend(search_youtube(query))
        except Exception as exc:
            logging.warning("YouTube search failed for %s: %s", query, exc)

    prioritized = prioritize_items(candidates)
    logging.info(
        "Collected %s candidates for %s; kept %s English/Italian readable item(s).",
        len(candidates),
        topic_name,
        len(prioritized),
    )
    return prioritized


def dedupe_items(items: Iterable[ContentItem]) -> list[ContentItem]:
    seen = set()
    deduped = []
    for item in items:
        if item.stable_id in seen:
            continue
        seen.add(item.stable_id)
        deduped.append(item)
    return deduped


def format_message(topic: dict, item: ContentItem) -> str:
    title = item.title[:180]
    summary = item.summary[:220].strip()
    parts = [
        f"<b>{html.escape(topic.get('title', 'Recommended'))}</b>",
        f"<b>{html.escape(title)}</b>",
    ]
    if summary:
        parts.append(html.escape(summary))
    parts.append(f"Source: {html.escape(item.source)}")
    parts.append(html.escape(item.url))
    return "\n\n".join(parts)


async def post_item(bot: Bot, topic_name: str, topic: dict, item: ContentItem, dry_run: bool) -> None:
    message = format_message(topic, item)
    thread_id = TOPICS.get(topic_name)
    if not thread_id:
        logging.warning("Skipping %s because no Telegram topic id is configured.", topic_name)
        return

    if dry_run:
        print(f"\n--- DRY RUN: {topic_name} / thread {thread_id} ---\n{message}\n")
        return

    await bot.send_message(
        chat_id=CHAT_ID,
        message_thread_id=thread_id,
        text=message,
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


async def run_once(dry_run: bool = False) -> int:
    token = os.getenv("BOT_TOKEN")
    if not token and not dry_run:
        raise RuntimeError("BOT_TOKEN is missing. Add it to .env before posting to Telegram.")
    if CHAT_ID == 0 and not dry_run:
        raise RuntimeError("CHAT_ID is missing. Add your Telegram group chat id to .env before posting.")

    topic_config = load_topic_config()
    posted_ids = load_state()
    seen_this_run = set(posted_ids)
    bot = Bot(token=token) if token else None
    posted_count = 0

    for topic_name, topic in topic_config.items():
        if topic_name not in TOPICS:
            logging.info("Skipping unknown topic %s.", topic_name)
            continue

        candidates = collect_candidates(topic_name, topic)
        next_item = next((item for item in candidates if item.stable_id not in seen_this_run), None)
        if not next_item:
            logging.info("No fresh item found for %s.", topic_name)
            continue

        await post_item(bot, topic_name, topic, next_item, dry_run)
        seen_this_run.add(next_item.stable_id)
        if not dry_run:
            posted_ids.add(next_item.stable_id)
        posted_count += 1

        if posted_count >= MAX_POSTS_PER_RUN:
            break

    if not dry_run:
        save_state(posted_ids)
    return posted_count


async def run_forever(dry_run: bool = False) -> None:
    while True:
        try:
            count = await run_once(dry_run=dry_run)
            logging.info("Run completed. Posted %s item(s).", count)
        except Exception:
            logging.exception("Automation run failed.")

        await asyncio.sleep(POST_INTERVAL_MINUTES * 60)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find relevant content and post it to Telegram forum topics.")
    parser.add_argument("--once", action="store_true", help="Run one collection/posting cycle and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print posts without sending them to Telegram.")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"), help="Python logging level.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.once:
        posted_count = asyncio.run(run_once(dry_run=args.dry_run))
        logging.info("Finished one run with %s item(s).", posted_count)
        return
    asyncio.run(run_forever(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
