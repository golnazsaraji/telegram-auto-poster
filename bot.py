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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "8"))
RESULTS_PER_SOURCE = int(os.getenv("RESULTS_PER_SOURCE", "10"))
JOB_MAX_AGE_DAYS = int(os.getenv("JOB_MAX_AGE_DAYS", "30"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
SSL_VERIFY = os.getenv("SSL_VERIFY", "true").lower() not in {"0", "false", "no"}
USE_DUCKDUCKGO = os.getenv("USE_DUCKDUCKGO", "false").lower() in {"1", "true", "yes"}

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
        "posts_per_run": int(os.getenv("JOBS_PER_RUN", "5")),
        "job_location": "Torino, Piedmont, Italy",
        "job_fields": [
            "software developer",
            "data analyst",
            "artificial intelligence",
            "mechanical engineer",
            "marketing",
            "sales",
            "finance",
            "product manager",
            "designer",
            "human resources",
            "customer support",
            "logistics",
            "research engineer",
        ],
        "job_engines": ["linkedin", "indeed"],
        "queries": [],
        "rss_feeds": [],
        "allowed_domains": [
            "linkedin.com",
            "indeed.com",
            "glassdoor.com",
            "infojobs.it",
            "monster.it",
            "randstad.it",
            "adecco.it",
            "manpower.it",
            "euraxess.ec.europa.eu",
            "eures.europa.eu",
        ],
        "required_terms": ["turin", "torino"],
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
    for index, item in enumerate(filter_readable_items(items)):
        priority = language_priority(item)
        prioritized.append((priority, index, item))
    return [item for _, _, item in sorted(prioritized, key=lambda entry: (entry[0], entry[1]))]


def filter_readable_items(items: Iterable[ContentItem]) -> list[ContentItem]:
    readable = []
    for item in dedupe_items(items):
        if language_priority(item) is None:
            logging.info("Rejected unreadable/non-English-Italian item: %s", item.title[:100])
            continue
        readable.append(item)
    return readable


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


def fetch_text(url: str, timeout: int | None = None) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout or HTTP_TIMEOUT_SECONDS, context=ssl_context()) as response:
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
    if not USE_DUCKDUCKGO:
        return search_bing_news(query, allowed_domains, limit=limit)

    search_url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    parser = DuckDuckGoParser()
    try:
        parser.feed(fetch_text(search_url, timeout=5))
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


def parse_datetime(value: str) -> datetime | None:
    value = clean_text(value)
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None

    if parsed is None:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_relative_age(value: str, now: datetime | None = None) -> datetime | None:
    text = clean_text(value).lower()
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    if any(marker in text for marker in ("today", "just posted", "just now", "now")):
        return now
    if "yesterday" in text:
        return now - timedelta(days=1)

    match = re.search(
        r"(\d+)\+?\s*(minute|hour|day|week|month)s?\s*ago|posted\s*(\d+)\+?\s*(minute|hour|day|week|month)s?",
        text,
    )
    if not match:
        return None

    amount = int(match.group(1) or match.group(3))
    unit = match.group(2) or match.group(4)
    if unit == "minute":
        delta = timedelta(minutes=amount)
    elif unit == "hour":
        delta = timedelta(hours=amount)
    elif unit == "day":
        delta = timedelta(days=amount)
    elif unit == "week":
        delta = timedelta(weeks=amount)
    else:
        delta = timedelta(days=amount * 30)
    return now - delta


def job_date_from_fragment(fragment: str) -> str:
    patterns = [
        r'<time[^>]+datetime="([^"]+)"',
        r'"datePosted"\s*:\s*"([^"]+)"',
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'"listedAt"\s*:\s*(\d{10,13})',
        r'"pubDate"\s*:\s*(\d{10,13})',
        r'"formattedRelativeTime"\s*:\s*"((?:\\.|[^"])*)"',
        r'"relativeTime"\s*:\s*"((?:\\.|[^"])*)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, fragment)
        if not match:
            continue
        value = decode_json_string(match.group(1))
        if value.isdigit():
            timestamp = int(value)
            if timestamp > 9_999_999_999:
                timestamp //= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        return value
    return ""


def is_recent_job(item: ContentItem, max_age_days: int = JOB_MAX_AGE_DAYS) -> bool:
    now = datetime.now(timezone.utc)
    published_at = parse_datetime(item.published) or parse_relative_age(item.published, now=now)
    if not published_at:
        logging.info("Rejected job without a verifiable posting date: %s", item.title[:100])
        return False
    cutoff = now - timedelta(days=max_age_days)
    if published_at < cutoff:
        logging.info("Rejected job older than %s day(s): %s", max_age_days, item.title[:100])
        return False
    return True


def collect_job_engine_candidates(topic: dict, limit: int = 5) -> list[ContentItem]:
    fields = topic.get("job_fields", [])
    location = topic.get("job_location", "Torino, Piedmont, Italy")
    engines = set(topic.get("job_engines", []))
    field_results: list[list[ContentItem]] = []

    for field in fields:
        candidates: list[ContentItem] = []
        if "linkedin" in engines:
            try:
                candidates.extend(search_linkedin_jobs(field, location, limit=limit))
            except Exception as exc:
                logging.warning("LinkedIn job search failed for %s: %s", field, exc)
        if "indeed" in engines:
            try:
                candidates.extend(search_indeed_jobs(field, "Torino, Piemonte", limit=limit))
            except Exception as exc:
                logging.warning("Indeed job search failed for %s: %s", field, exc)
        if candidates:
            field_results.append(dedupe_items(candidates))

    return interleave_items(field_results)


def interleave_items(item_groups: Iterable[list[ContentItem]]) -> list[ContentItem]:
    groups = [group for group in item_groups if group]
    interleaved = []
    max_length = max((len(group) for group in groups), default=0)
    for index in range(max_length):
        for group in groups:
            if index < len(group):
                interleaved.append(group[index])
    return dedupe_items(interleaved)


def search_linkedin_jobs(field: str, location: str, limit: int = 5) -> list[ContentItem]:
    recent_window_seconds = JOB_MAX_AGE_DAYS * 24 * 60 * 60
    search_url = (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote_plus(field)}&location={quote_plus(location)}"
        f"&f_TPR=r{recent_window_seconds}&sortBy=DD"
    )
    page = fetch_text(search_url, timeout=10)
    results = []
    pattern = re.compile(r'<a[^>]+href="([^"]+/jobs/view/[^"]+)"[^>]*>(.*?)</a>', re.S)

    for match in pattern.finditer(page):
        url, raw_title = match.groups()
        title = clean_text(raw_title)
        if not title:
            continue
        fragment = page[max(0, match.start() - 3000) : match.end() + 6000]
        published = job_date_from_fragment(fragment)
        normalized_url = normalize_job_url(html.unescape(url))
        item = ContentItem(
            title=title,
            url=normalized_url,
            source="linkedin.com",
            summary=f"{field.title()} opportunity in Torino/Turin.",
            published=published,
        )
        if not is_recent_job(item):
            continue
        results.append(item)
        if len(results) >= limit:
            break

    return dedupe_items(results)


def search_indeed_jobs(field: str, location: str, limit: int = 5) -> list[ContentItem]:
    search_url = (
        f"https://it.indeed.com/jobs?q={quote_plus(field)}&l={quote_plus(location)}"
        f"&sort=date&fromage={JOB_MAX_AGE_DAYS}"
    )
    page = fetch_text(search_url, timeout=10)
    results = []

    for match in re.finditer(r'"jobTitle":"((?:\\.|[^"])*)"', page):
        window = page[max(0, match.start() - 3000) : match.start() + 6000]
        key_match = re.search(r'"jobkey":"([^"]+)"', window)
        if not key_match:
            continue

        title = clean_text(decode_json_string(match.group(1)))
        location_match = re.search(r'"formattedLocation":"((?:\\.|[^"])*)"', window)
        subtitle_match = re.search(r'"subtitle":"((?:\\.|[^"])*)"', window)
        job_location = decode_json_string(location_match.group(1)) if location_match else "Torino"
        subtitle = decode_json_string(subtitle_match.group(1)) if subtitle_match else job_location
        if title and item_matches_terms(ContentItem(title, "", "", summary=job_location), ["torino", "turin"]):
            published = job_date_from_fragment(window)
            item = ContentItem(
                title=title,
                url=f"https://it.indeed.com/viewjob?jk={key_match.group(1)}",
                source="indeed.com",
                summary=clean_text(subtitle),
                published=published,
            )
            if not is_recent_job(item):
                continue
            results.append(item)
        if len(dedupe_items(results)) >= limit:
            break

    return dedupe_items(results)[:limit]


def decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def normalize_job_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def collect_candidates(topic_name: str, topic: dict) -> list[ContentItem]:
    allowed_domains = topic.get("allowed_domains", [])
    required_terms = [term.lower() for term in topic.get("required_terms", [])]
    candidates: list[ContentItem] = []

    if topic_name == "jobs":
        try:
            candidates.extend(collect_job_engine_candidates(topic, limit=RESULTS_PER_SOURCE))
        except Exception as exc:
            logging.warning("Direct job engine search failed for %s: %s", topic_name, exc)

    for feed_url in topic.get("rss_feeds", []):
        try:
            candidates.extend(read_rss_feed(feed_url, allowed_domains, limit=RESULTS_PER_SOURCE))
        except Exception as exc:
            logging.warning("RSS source failed for %s: %s", feed_url, exc)

    for query in topic.get("queries", []):
        try:
            candidates.extend(search_web(query, allowed_domains, limit=RESULTS_PER_SOURCE))
        except Exception as exc:
            logging.warning("Web search failed for %s: %s", query, exc)

    for query in topic.get("youtube_queries", []):
        try:
            candidates.extend(search_youtube(query, limit=RESULTS_PER_SOURCE))
        except Exception as exc:
            logging.warning("YouTube search failed for %s: %s", query, exc)

    if topic_name == "jobs":
        candidates = [item for item in candidates if is_recent_job(item)]

    if required_terms:
        candidates = [item for item in candidates if item_matches_terms(item, required_terms)]

    prioritized = filter_readable_items(candidates) if topic_name == "jobs" else prioritize_items(candidates)
    logging.info(
        "Collected %s candidates for %s; kept %s English/Italian readable item(s).",
        len(candidates),
        topic_name,
        len(prioritized),
    )
    return prioritized


def item_matches_terms(item: ContentItem, required_terms: Iterable[str]) -> bool:
    text = f"{item.title} {item.summary} {item.url}".lower()
    return any(term in text for term in required_terms)


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
        fresh_items = [item for item in candidates if item.stable_id not in seen_this_run]
        if not fresh_items:
            logging.info("No fresh item found for %s.", topic_name)
            continue

        topic_limit = max(1, int(topic.get("posts_per_run", 1)))
        for item in fresh_items[:topic_limit]:
            await post_item(bot, topic_name, topic, item, dry_run)
            seen_this_run.add(item.stable_id)
            if not dry_run:
                posted_ids.add(item.stable_id)
            posted_count += 1

            if posted_count >= MAX_POSTS_PER_RUN:
                break

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
