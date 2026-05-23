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


# v3.4.8 — Strict architecture-relevance filter for lab blog titles.
#
# v3.4.7 production confirmed that the lab blog scrapers' unfiltered
# output pushed Haiku triage past its output token limit (response
# truncated at ~7500 tokens, json_repair recovered partial entries,
# BrokerAction construction crashed on missing required fields). The
# root cause wasn't the parser — it was that we ingested every post
# from anthropic.com/news / ai.meta.com/blog / deepmind.google/blog
# including hiring posts, partnership announcements, applied-research
# blog posts, policy talks — none of which affect memory-cycle math
# but all of which Haiku has to read and triage.
#
# This filter keeps ONLY items whose title looks like a model release
# or specific architecture/efficiency advancement. Filter is applied
# at parse time so non-relevant items never enter the news pool.
#
# Two paths to pass the filter:
#   1. Title contains a frontier-lab model name AND either a release
#      verb (introducing/launching/etc.) or a version number (5, 4.6,
#      2.5, etc.)
#   2. Title contains an unambiguous architecture/efficiency term
#      (MoE, KV cache, HBM, context window, etc.)
#
# False positives (item passes but is noise) are okay — Haiku triage
# will deprioritize them. False negatives (item filtered but should
# have passed) are the failure mode to watch; if we see DHCR missing
# a known model release, this filter is too strict.

_MODEL_NAMES = re.compile(
    r"\b("
    r"claude|llama|gemini|sonnet|haiku|opus"
    r"|mistral|mixtral|deepseek|qwen|grok|kimi"
    r"|gpt[\s-]?\d|o[1-9](?:[\s-]?(?:mini|pro))?"
    r"|phi[\s-]?\d|gemma|nemotron"
    r")\b",
    re.IGNORECASE,
)

_RELEASE_VERBS = re.compile(
    r"\b("
    r"introducing|introduces?|introduced"
    r"|launching|launches?|launched"
    r"|announcing|announces?|announced"
    r"|releasing|releases?|released"
    r"|unveils?|unveiled|debuts?|debuted"
    r"|technical\s+report|model\s+card"
    r"|now\s+available|now\s+open|generally\s+available"
    r"|new\s+(?:model|generation|version|frontier)"
    r")\b",
    re.IGNORECASE,
)

# Version pattern: matches "5", "4.6", "2.5.1", "V3", "v4" — common version
# notations after a model name. The leading V/v is optional so we catch both
# "Claude 5" and "DeepSeek V3" / "Llama v4".
_VERSION_PATTERN = re.compile(r"\b[Vv]?\d+(?:\.\d+){0,2}\b")

_ARCH_TERMS = re.compile(
    r"\b("
    r"moe|mixture[\s-]of[\s-]experts"
    r"|kv[\s-]cache|hbm[0-9]*[ep]?"
    r"|context\s+window|long[\s-]context|extended[\s-]context"
    r"|sparse[\s-]attention|flash[\s-]?attention"
    r"|\d+\s*[bm]\s+parameters?"  # "70B parameters", "8M parameters"
    r"|quantization|distillation"
    r"|inference\s+(?:speed|efficiency|cost|throughput|latency)"
    r"|reasoning\s+(?:model|engine|mode)"
    r"|tokens?\s+per\s+second|t\/s\b"
    r"|throughput\s+optimization"
    r")\b",
    re.IGNORECASE,
)


def _is_architecture_relevant(title: str) -> bool:
    """Strict whitelist for lab blog titles (v3.4.8).

    Returns True only for posts that look like model releases or
    specific architecture/efficiency advancements. Filters out
    partnership / policy / hiring / applied-research posts.

    Examples that PASS:
      - "Introducing Claude 5"
      - "Llama 4 technical report"
      - "Gemini 2.5 Pro with 1M context window"
      - "Reducing KV cache memory with sparse attention"
      - "Mixtral 8x22B: a new MoE architecture"

    Examples that FAIL:
      - "Widening the conversation on frontier AI"  (Anthropic policy)
      - "KPMG integrates Claude across its core business"  (partnership)
      - "Anthropic acquires Stainless"  (acquisition, no architecture)
      - "Building responsible AI"  (policy)
      - "How we train Claude to help with hard problems"  (no release/arch)
    """
    if not title:
        return False
    has_model = bool(_MODEL_NAMES.search(title))
    has_release_verb = bool(_RELEASE_VERBS.search(title))
    has_version = bool(_VERSION_PATTERN.search(title))
    has_arch = bool(_ARCH_TERMS.search(title))

    # Path 1: model name + release context (verb or version)
    if has_model and (has_release_verb or has_version):
        return True
    # Path 2: unambiguous architecture/efficiency term
    if has_arch:
        return True
    return False


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
        # v3.4.8 — strict architecture-relevance filter. Filters
        # acquisitions, partnerships, policy posts, applied-AI work,
        # hiring. Keeps model releases + architecture-specific posts.
        if not _is_architecture_relevant(title):
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
        # v3.4.8 — strict architecture-relevance filter.
        if not _is_architecture_relevant(title):
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
        # v3.4.8 — strict architecture-relevance filter.
        if not _is_architecture_relevant(title):
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
    # v3.4.8 — cap items per source to bound Haiku triage input
    # volume. Belt-and-braces against a scraper unexpectedly returning
    # a long back-catalog (e.g. a lab publishing 20 posts in one week).
    # The recency filter in fetch_lab_blog_scrapes ALSO bounds this,
    # but a per-source explicit cap is the simpler invariant.
    max_items: int = 5


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
    # v3.4.8 — HuggingFace Daily Papers REMOVED from active scrapers.
    # The parser remains in this module (parse_huggingface_daily_papers)
    # so it can be re-enabled by adding a LabBlogConfig back, but in
    # normal operation we don't fetch from it.
    #
    # Rationale: HF Daily Papers is a manually-curated subset of arxiv
    # submissions. We already ingest arxiv cs.LG and cs.CL via the
    # arxiv RSS feeds (pre-v3.4.7). The overlap is ~80-90% — HF Papers
    # adds curation quality but not new coverage. Meanwhile, it
    # contributed ~10-15 items per run to Haiku triage, most of which
    # are not memory-cycle relevant (general ML research, computer
    # vision, multimodal models without memory architecture claims).
    # The 2026-05-23 fast-tier crash was caused by Haiku output
    # truncation from cumulative news volume; dropping HF Papers
    # removes the largest single contributor of low-relevance items.
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
    """Internal: fetch HTML, parse, filter to items newer than cutoff,
    cap at cfg.max_items (v3.4.8)."""
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
    # v3.4.8 — sort newest-first, then cap at max_items. Belt-and-braces
    # against a back-catalog day; on a normal day the recency filter
    # has already trimmed to 0-3 items.
    recent.sort(key=lambda it: it.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    if len(recent) > cfg.max_items:
        logger.info(
            f"{cfg.name}: capping {len(recent)} → {cfg.max_items} most-recent items"
        )
        recent = recent[:cfg.max_items]
    return recent
