"""Lab blog HTML scrapers (v3.4.7) — replaces the dropped RSS feeds.

v3.4.6.3 dropped 6 lab-blog RSS feeds after production confirmed they
either lack native RSS (Meta AI Blog, DeepMind, DeepSeek, Anthropic
News, Qwen) or require auth (HuggingFace Papers). The Google News
queries added in v3.4.6.3 cover the SIGNAL via tech press, but the
arch aggregator's Tier-A source-tier classification (the highest
authority for architecture-spec extraction) requires the lab to be the
source of origin. This module recovers the Tier-A path by scraping the
lab blog HTML directly and emitting NewsItem records that route to the
correct Tier-A label via `_classify_source_for_tier()`.

Scope (v3.4.7 ship):
  - Anthropic News (anthropic.com/news)
  - Meta AI Blog (ai.meta.com/blog)
  - Google DeepMind Blog (deepmind.google/blog)

Out of scope for v3.4.7 (no public HTML structure confirmed yet):
  - DeepSeek (api-docs.deepseek.com/news structure not stable)
  - Qwen (content migrated to qwen.ai/research; structure TBD)
  - HuggingFace Papers (requires auth; not viable)

Design: each scraper is a pure function `(html: str) -> list[NewsItem]`
that takes pre-fetched HTML and returns parsed items. The orchestrator
`fetch_lab_blog_scrapes()` handles the fetch (via smart_get for
Cloudflare resilience), diagnostic recording, and recency filtering.

Why HTML scrapers instead of RSSHub:
  - No external service dependency (RSSHub public instances are
    rate-limited and have variable uptime)
  - No new pip dependency (curl_cffi already pulled in by yfinance)
  - Per-lab parser code is small (~30-50 lines each) and the HTML
    structure for these three sites has been stable for 1y+
  - When structure DOES change, the regex will start returning 0 items
    and the diagnostic table's "WARN 0 items" will surface it loudly
    rather than failing silently
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from bs4 import BeautifulSoup

from src.data.news import NewsItem, RECENT_HOURS_DEFAULT, HEADERS, smart_get
from src.diagnostics import DiagnosticsCollector

logger = logging.getLogger(__name__)


# ============================================================
# Per-lab HTML parsers
# ============================================================

# Anthropic News: anthropic.com/news
# Structure observed 2026-05-22:
#   <a href="https://www.anthropic.com/news/{slug}">
#     {date e.g. "May 19, 2026"}
#     {category e.g. "Announcements"}
#     {title text}
#   </a>
# Sample anchor inner text (concatenated):
#   "May 19, 2026Announcements Widening the conversation on frontier AI"
_ANTHROPIC_NEWS_LINK_PATTERN = re.compile(
    r"^"
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(?P<day>\d{1,2}),\s+"
    r"(?P<year>\d{4})"
    r"(?P<category>Announcements|Product|Policy|Interpretability|Research|Societal Impacts)?"
    r"\s*"
    r"(?P<title>.+?)"
    r"$"
)
_MONTH_NUM = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def parse_anthropic_news(html: str) -> list[NewsItem]:
    """Parse anthropic.com/news HTML. Returns NewsItems with source='anthropic.com'.

    Items become Tier A in the arch aggregator via
    _classify_source_for_tier (anthropic → anthropic.com).
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[NewsItem] = []
    seen_urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/news/" not in href:
            continue
        # Normalize to absolute URL
        if href.startswith("/"):
            href = "https://www.anthropic.com" + href
        if href in seen_urls:
            continue
        # Skip the "Newsroom" / footer / share links
        if href.rstrip("/").endswith("/news") or href.rstrip("/").endswith("/news#"):
            continue
        text = " ".join(a.get_text(separator=" ").split())  # collapse whitespace
        if not text or len(text) < 10:
            continue
        m = _ANTHROPIC_NEWS_LINK_PATTERN.match(text)
        if not m:
            continue
        try:
            pub = datetime(
                int(m.group("year")),
                _MONTH_NUM[m.group("month")[:3]],
                int(m.group("day")),
                tzinfo=timezone.utc,
            )
        except (ValueError, KeyError):
            continue
        title = m.group("title").strip()
        if not title or len(title) < 5:
            continue
        out.append(NewsItem(
            title=title,
            url=href,
            source="anthropic.com",
            published=pub,
            summary=None,
        ))
        seen_urls.add(href)
    return out


# Meta AI Blog: ai.meta.com/blog
# Structure observed 2026-05-22:
#   "Blog" → category line → "#### {Title}" → "{Month DD, YYYY}" → "Learn More"
# The blog page is server-rendered (React Server Components). Anchors to
# individual posts look like:
#   <a href="https://ai.meta.com/blog/{slug}/">{Title}</a>
# Date pattern "Month DD, YYYY" appears near each anchor; we capture
# (anchor, nearest preceding/following date) pairs.

_META_BLOG_DATE_PATTERN = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})\b"
)
_META_FULL_MONTH_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def parse_meta_ai_blog(html: str) -> list[NewsItem]:
    """Parse ai.meta.com/blog HTML.

    Items become Tier A via _classify_source_for_tier (ai.meta.com →
    ai.meta.com). Source attribution: 'ai.meta.com'.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[NewsItem] = []
    seen_urls = set()

    # Iterate over anchor elements pointing at individual blog posts
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Must look like a blog post slug, not the "Learn More" navigation
        if "/blog/" not in href:
            continue
        if href.rstrip("/").endswith("/blog"):
            continue
        if href in seen_urls:
            continue
        # Heuristic: title is the link text
        title = " ".join(a.get_text(separator=" ").split())
        if not title or len(title) < 5:
            continue
        # Look for a date near this anchor — check siblings and parent
        date_text = None
        for candidate in (a, a.parent, a.parent.parent if a.parent else None):
            if candidate is None:
                continue
            block_text = candidate.get_text(separator=" ", strip=True) if hasattr(candidate, "get_text") else ""
            m = _META_BLOG_DATE_PATTERN.search(block_text)
            if m:
                date_text = m
                break
        if not date_text:
            continue
        try:
            pub = datetime(
                int(date_text.group("year")),
                _META_FULL_MONTH_NUM[date_text.group("month")],
                int(date_text.group("day")),
                tzinfo=timezone.utc,
            )
        except (ValueError, KeyError):
            continue
        # Avoid picking up titles like "FEATURED" or generic UI strings
        if title.upper() in ("FEATURED", "BLOG", "LEARN MORE", "RESEARCH",
                              "COMPUTER VISION", "OPEN SOURCE", "ALL CATEGORIES"):
            continue
        out.append(NewsItem(
            title=title,
            url=href,
            source="ai.meta.com",
            published=pub,
            summary=None,
        ))
        seen_urls.add(href)
    return out


# Google DeepMind Blog: deepmind.google/blog
# Structure observed 2026-05-22:
#   <a href="https://deepmind.google/blog/{slug}/"> ### Title ... <date "May 2026"> ... </a>
# Date is month + year only (no day). Use day=1 as approximation.

_DEEPMIND_DATE_PATTERN = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(?P<year>\d{4})\b"
)


def parse_deepmind_blog(html: str) -> list[NewsItem]:
    """Parse deepmind.google/blog HTML.

    Items become Tier A via _classify_source_for_tier (deepmind →
    deepmind.google). Source attribution: 'deepmind.google'.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[NewsItem] = []
    seen_urls = set()

    # Each blog item has an <h3> with the title, wrapped in an <a> to /blog/{slug}/
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "deepmind.google/blog/" not in href and not href.startswith("/blog/"):
            continue
        if href.rstrip("/").endswith("/blog"):
            continue
        # Some external links from deepmind point to blog.google subdomains
        # — those are also legitimate DeepMind announcements
        if "blog.google" in href and "deepmind" not in href:
            continue
        if href.startswith("/"):
            href = "https://deepmind.google" + href
        if href in seen_urls:
            continue
        # Find title: the anchor contains a heading or link text
        title = ""
        h = a.find(["h2", "h3", "h4"])
        if h:
            title = h.get_text(separator=" ", strip=True)
        if not title:
            title = " ".join(a.get_text(separator=" ").split())
        if not title or len(title) < 5:
            continue
        # Find date near anchor
        date_text = None
        for candidate in (a, a.parent, a.parent.parent if a.parent else None):
            if candidate is None:
                continue
            block_text = candidate.get_text(separator=" ", strip=True) if hasattr(candidate, "get_text") else ""
            m = _DEEPMIND_DATE_PATTERN.search(block_text)
            if m:
                date_text = m
                break
        if not date_text:
            continue
        try:
            pub = datetime(
                int(date_text.group("year")),
                _META_FULL_MONTH_NUM[date_text.group("month")],
                1,  # day not in HTML; approximate
                tzinfo=timezone.utc,
            )
        except (ValueError, KeyError):
            continue
        # Filter category-only or UI strings
        if title.upper() in ("LEARN MORE", "NEWS", "RESEARCH", "BREAKTHROUGHS",
                              "MODELS", "SCIENCE", "RESPONSIBILITY & SAFETY",
                              "EXPLORE RESEARCH", "EXPLORE MODELS"):
            continue
        out.append(NewsItem(
            title=title,
            url=href,
            source="deepmind.google",
            published=pub,
            summary=None,
        ))
        seen_urls.add(href)
    return out


# HuggingFace Daily Papers JSON API (no auth required, despite the
# /papers/feed.xml endpoint requiring auth)
#
# Confirmed public endpoint per multiple HF community projects:
#   https://huggingface.co/api/daily_papers
#
# Returns a JSON array; each entry has shape:
#   {
#     "paper": {
#       "id": "2508.18124",         # arxiv ID
#       "title": "CMPhysBench: ...",
#       "authors": [{"name": "..."}],
#       "summary": "...",
#       "publishedAt": "2026-05-22T..."
#     },
#     "upvotes": 15,
#     ...
#   }
#
# We emit one NewsItem per paper. source='huggingface.co/papers' so the
# digester's _classify_source_for_tier routes it to 'huggingface_papers'
# → Tier A in the DHCR arch aggregator (matches the source label added
# in v3.4.6 specifically for this feed).

def parse_huggingface_daily_papers(json_text: str) -> list[NewsItem]:
    """Parse https://huggingface.co/api/daily_papers JSON response.

    Returns NewsItems with source='huggingface.co/papers' → routes to
    huggingface_papers tier label → Tier A in DHCR arch aggregator.
    """
    try:
        data = json.loads(json_text)
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning(f"HF daily_papers JSON parse failed: {e}")
        return []
    if not isinstance(data, list):
        return []

    out: list[NewsItem] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        paper = entry.get("paper", {})
        if not isinstance(paper, dict):
            continue
        paper_id = paper.get("id", "").strip()
        title = (paper.get("title") or "").strip()
        if not paper_id or not title:
            continue
        # publishedAt is typically "2026-05-22T..." (ISO 8601)
        pub = None
        pub_str = paper.get("publishedAt") or entry.get("publishedAt")
        if pub_str:
            try:
                # Strip trailing 'Z' for fromisoformat compatibility (older Pythons)
                clean = pub_str.replace("Z", "+00:00") if pub_str.endswith("Z") else pub_str
                pub = datetime.fromisoformat(clean)
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pub = None
        # Summary truncated for the NewsItem.summary field
        summary = (paper.get("summary") or "")[:400] or None
        out.append(NewsItem(
            title=title,
            url=f"https://huggingface.co/papers/{paper_id}",
            source="huggingface.co/papers",
            published=pub,
            summary=summary,
        ))
    return out


# ============================================================
# Lab blog registry + orchestrator
# ============================================================

@dataclass
class LabBlogConfig:
    name: str          # Diagnostics-table display name
    url: str           # URL to fetch
    parser: Callable[[str], list[NewsItem]]


LAB_BLOG_SCRAPERS: list[LabBlogConfig] = [
    LabBlogConfig(
        name="Anthropic News (scraper)",
        url="https://www.anthropic.com/news",
        parser=parse_anthropic_news,
    ),
    LabBlogConfig(
        name="Meta AI Blog (scraper)",
        url="https://ai.meta.com/blog/",
        parser=parse_meta_ai_blog,
    ),
    LabBlogConfig(
        name="Google DeepMind Blog (scraper)",
        url="https://deepmind.google/blog/",
        parser=parse_deepmind_blog,
    ),
    # v3.4.7.1 — HuggingFace Daily Papers via public JSON API.
    # Replaces the v3.4.6 RSS attempt at /papers/feed.xml which
    # required auth. The /api/daily_papers endpoint is unauthenticated
    # and returns structured JSON, which is more reliable than HTML
    # scraping.
    LabBlogConfig(
        name="HuggingFace Daily Papers (json)",
        url="https://huggingface.co/api/daily_papers",
        parser=parse_huggingface_daily_papers,
    ),
]


def fetch_lab_blog_scrapes(
    hours_back: int = RECENT_HOURS_DEFAULT,
    collector: Optional[DiagnosticsCollector] = None,
    configs: Optional[list[LabBlogConfig]] = None,
) -> list[NewsItem]:
    """Fetch and parse all configured lab blogs.

    Records one SourceFetchStatus per blog. Recent-only filtering applied
    (same hours_back semantics as fetch_rss_feeds).
    """
    items: list[NewsItem] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    configs = configs or LAB_BLOG_SCRAPERS

    for cfg in configs:
        if collector is not None:
            try:
                with collector.track(cfg.name, "scraper") as t:
                    per_blog_items = _fetch_and_parse_one(cfg, cutoff)
                    t.items_returned = len(per_blog_items)
                items.extend(per_blog_items)
            except Exception as e:
                logger.warning(f"Lab blog scrape failed for {cfg.name}: {e}")
        else:
            try:
                items.extend(_fetch_and_parse_one(cfg, cutoff))
            except Exception as e:
                logger.warning(f"Lab blog scrape failed for {cfg.name}: {e}")

    return items


def _fetch_and_parse_one(cfg: LabBlogConfig, cutoff: datetime) -> list[NewsItem]:
    """Internal: fetch HTML, parse, filter to items newer than cutoff."""
    # Use Accept: text/html (overriding the default RSS Accept header)
    headers = dict(HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    resp = smart_get(cfg.url, headers=headers, timeout=20)
    resp.raise_for_status()
    raw_items = cfg.parser(resp.text)
    # Filter to recent items only (the lab blog HTML usually shows ~30
    # latest posts spanning months; we only want recent ones for the
    # daily brief signal)
    recent = [it for it in raw_items if it.published and it.published >= cutoff]
    # If the lab blog parser returned items but ALL are older than the
    # cutoff (e.g. a slow news week), emit nothing rather than the full
    # back-catalog. The 0-items case will be visible as WARN in the
    # diagnostics table.
    return recent
