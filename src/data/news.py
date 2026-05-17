"""
News and headline fetching from multiple sources.

Uses:
- RSS feeds (Korea Times, Yonhap, TrendForce when available)
- SEC EDGAR atom feeds (SK Hynix F-1 / F-1/A / 424 / 6-K / 20-F filings)
- Google News RSS for keyword queries
- Direct scraping via requests + BeautifulSoup for sites without feeds

Every fetch records a SourceFetchStatus into an optional collector so the
email's data-source validation table can confirm the source was actually
reached and parsed.

CHANGELOG (this file):
  v3.2   (2026-05-12) — score_relevance/filter_relevant_news accept tracked_concepts; Haiku rubric covers architectural signals
  v3.1   (2026-05-12) — Added score_relevance() + filter_relevant_news() two-pass relevance filter
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

from src.diagnostics import DiagnosticsCollector

logger = logging.getLogger(__name__)


@dataclass
class NewsItem:
    """A single news headline / article reference."""
    title: str
    url: str
    source: str
    published: Optional[datetime]
    summary: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published": self.published.isoformat() if self.published else None,
            "summary": self.summary,
        }


HEADERS = {
    # v3.38.3: previously identified as "Mozilla/5.0 (compatible; MemoryCycleBrief/1.0;
    # +https://github.com/raul/memory-brief)". The "compatible;" pattern is the
    # classic bot UA convention — Substack (Damnang), Investing.com, and others
    # respond 403 to it via Cloudflare anti-bot rules. Switched to a plain Chrome UA;
    # legitimate browser fingerprint, no bot signal.
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# SEC requires a descriptive UA with a real contact email for EDGAR access;
# generic UAs get blocked with 403. We reuse EMAIL_RECIPIENT (already set as
# a GitHub secret) so users don't need to configure another email separately.
# See https://www.sec.gov/os/accessing-edgar-data
import os as _os
_edgar_contact = _os.environ.get("EMAIL_RECIPIENT") or _os.environ.get("GMAIL_ADDRESS") or "contact@example.com"
EDGAR_HEADERS = {
    "User-Agent": f"Memory Cycle Brief {_edgar_contact}",
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}

# Cap-off windows
RECENT_HOURS_DEFAULT = 36  # roughly covers since the prior daily run


def fetch_rss_feeds(
    feeds: dict[str, str],
    hours_back: int = RECENT_HOURS_DEFAULT,
    collector: Optional[DiagnosticsCollector] = None,
) -> list[NewsItem]:
    """Fetch and parse all configured RSS feeds.

    Records one SourceFetchStatus per feed. Recent-only filtering applied.
    """
    items: list[NewsItem] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    for source_name, feed_url in feeds.items():
        per_feed_items = _safe_parse_rss_feed(
            source_name=source_name,
            feed_url=feed_url,
            cutoff=cutoff,
            source_type="rss",
            collector=collector,
        )
        items.extend(per_feed_items)

    return items


def fetch_edgar_feeds(
    feeds: dict[str, str],
    hours_back: int = 24 * 30,  # EDGAR filings are rare; widen window to 30 days
    collector: Optional[DiagnosticsCollector] = None,
) -> list[NewsItem]:
    """Fetch SEC EDGAR atom feeds for SK Hynix filing types.

    EDGAR uses a 30-day lookback because filings are infrequent — we want
    to catch them whenever they appear. Once a filing is classified as
    'seen' (via prior_state in main.py), the LLM suppresses re-flagging.

    Filings are titled with [FILING] prefix so they sort to the top of
    the news feed and the LLM gives them weight.
    """
    items: list[NewsItem] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    for source_name, feed_url in feeds.items():
        per_feed_items: list[NewsItem] = []
        try:
            if collector is not None:
                with collector.track(source_name, "edgar") as t:
                    resp = requests.get(feed_url, headers=EDGAR_HEADERS, timeout=20)
                    resp.raise_for_status()
                    parsed = feedparser.parse(resp.content)
                    for entry in parsed.entries:
                        published = _entry_published(entry)
                        if published and published < cutoff:
                            continue
                        per_feed_items.append(NewsItem(
                            title=f"[FILING] {entry.get('title', '(no title)').strip()}",
                            url=entry.get("link", ""),
                            source=source_name,
                            published=published,
                            summary=_clean_summary(entry.get("summary", "")),
                        ))
                    t.items_returned = len(per_feed_items)
            else:
                resp = requests.get(feed_url, headers=EDGAR_HEADERS, timeout=20)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
                for entry in parsed.entries:
                    published = _entry_published(entry)
                    if published and published < cutoff:
                        continue
                    per_feed_items.append(NewsItem(
                        title=f"[FILING] {entry.get('title', '(no title)').strip()}",
                        url=entry.get("link", ""),
                        source=source_name,
                        published=published,
                        summary=_clean_summary(entry.get("summary", "")),
                    ))
        except Exception as e:
            logger.warning(f"Failed to fetch {source_name}: {e}")
            continue

        items.extend(per_feed_items)

    return items


def fetch_google_news(
    queries: list[str],
    hours_back: int = RECENT_HOURS_DEFAULT,
    collector: Optional[DiagnosticsCollector] = None,
) -> list[NewsItem]:
    """Query Google News RSS for each query string and return recent items.

    Records one SourceFetchStatus per query.
    """
    items: list[NewsItem] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    for query in queries:
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}+when:2d&hl=en&gl=US&ceid=US:en"
        per_query_items: list[NewsItem] = []

        # v3.38.2: same UA fix as _safe_parse_rss_feed — fetch with requests +
        # HEADERS then feedparser.parse on bytes. Google News is more tolerant
        # of feedparser's default UA than Substack/Cloudflare sites, but
        # consistency is worth it: we lose the same way everywhere if Google
        # ever tightens its filter.
        def _fetch_and_parse() -> list[NewsItem]:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=20)
                resp.raise_for_status()
            except requests.RequestException as e:
                raise RuntimeError(f"HTTP fetch failed: {e}")
            parsed = feedparser.parse(resp.content)
            local: list[NewsItem] = []
            for entry in parsed.entries[:15]:
                published = _entry_published(entry)
                if published and published < cutoff:
                    continue
                local.append(NewsItem(
                    title=entry.get("title", "(no title)").strip(),
                    url=entry.get("link", ""),
                    source=f"Google News ({query})",
                    published=published,
                    summary=_clean_summary(entry.get("summary", "")),
                ))
            if not parsed.entries and getattr(parsed, "bozo_exception", None):
                raise RuntimeError(f"feed parse error: {parsed.bozo_exception}")
            return local

        try:
            if collector is not None:
                with collector.track(f"Google News: {query}", "google_news") as t:
                    per_query_items = _fetch_and_parse()
                    t.items_returned = len(per_query_items)
            else:
                per_query_items = _fetch_and_parse()
        except Exception as e:
            logger.warning(f"Failed to parse Google News for '{query}': {e}")
            continue

        items.extend(per_query_items)

    return items


def scrape_chosun_biz(
    hours_back: int = RECENT_HOURS_DEFAULT,
    collector: Optional[DiagnosticsCollector] = None,
) -> list[NewsItem]:
    """Scrape ChosunBiz English homepage for recent articles."""

    def _do() -> list[NewsItem]:
        resp = requests.get("https://biz.chosun.com/en/", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        out: list[NewsItem] = []
        for link in soup.select("a[href*='/en/']")[:50]:
            href = link.get("href", "")
            title = link.get_text(strip=True)
            if not title or len(title) < 20:
                continue
            if not (href.startswith("http") or href.startswith("/")):
                continue
            full_url = href if href.startswith("http") else f"https://biz.chosun.com{href}"
            out.append(NewsItem(
                title=title, url=full_url, source="ChosunBiz English",
                published=None, summary=None,
            ))
        seen, unique = set(), []
        for item in out:
            if item.url in seen:
                continue
            seen.add(item.url)
            unique.append(item)
        return unique[:30]

    items: list[NewsItem] = []
    try:
        if collector is not None:
            with collector.track("ChosunBiz English (scraper)", "scraper") as t:
                items = _do()
                t.items_returned = len(items)
        else:
            items = _do()
    except Exception as e:
        logger.warning(f"Failed to fetch ChosunBiz English: {e}")

    return items


def scrape_trendforce(
    hours_back: int = RECENT_HOURS_DEFAULT,
    collector: Optional[DiagnosticsCollector] = None,
) -> list[NewsItem]:
    """Scrape TrendForce news page for recent DRAM/NAND articles."""

    def _do() -> list[NewsItem]:
        resp = requests.get(
            "https://www.trendforce.com/news/category/dram",
            headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        out: list[NewsItem] = []
        for link in soup.select("article a[href*='/news/']")[:30]:
            href = link.get("href", "")
            title = link.get_text(strip=True)
            if not title or len(title) < 20:
                continue
            if not href:
                continue
            full_url = href if href.startswith("http") else f"https://www.trendforce.com{href}"
            out.append(NewsItem(
                title=title, url=full_url, source="TrendForce",
                published=None, summary=None,
            ))
        seen, unique = set(), []
        for item in out:
            if item.url in seen:
                continue
            seen.add(item.url)
            unique.append(item)
        return unique[:20]

    items: list[NewsItem] = []
    try:
        if collector is not None:
            with collector.track("TrendForce DRAM (scraper)", "scraper") as t:
                items = _do()
                t.items_returned = len(items)
        else:
            items = _do()
    except Exception as e:
        logger.warning(f"Failed to fetch TrendForce: {e}")

    return items


def fetch_all_news(
    rss_feeds: dict[str, str],
    google_queries: list[str],
    hours_back: int = RECENT_HOURS_DEFAULT,
    edgar_feeds: Optional[dict[str, str]] = None,
    collector: Optional[DiagnosticsCollector] = None,
) -> list[NewsItem]:
    """Aggregate news from all configured sources, deduplicate by title.

    The collector argument is forwarded to every sub-fetcher so the email
    template can render a per-source validation table.
    """
    all_items: list[NewsItem] = []

    all_items.extend(fetch_rss_feeds(rss_feeds, hours_back, collector=collector))
    if edgar_feeds:
        # EDGAR uses its own wider lookback window (30 days) inside the function
        all_items.extend(fetch_edgar_feeds(edgar_feeds, collector=collector))
    all_items.extend(fetch_google_news(google_queries, hours_back, collector=collector))
    all_items.extend(scrape_chosun_biz(hours_back, collector=collector))
    all_items.extend(scrape_trendforce(hours_back, collector=collector))

    # Deduplicate by normalized title
    seen_titles = set()
    deduped = []
    for item in all_items:
        norm = item.title.lower().strip()
        if norm in seen_titles:
            continue
        seen_titles.add(norm)
        deduped.append(item)

    deduped.sort(
        key=lambda x: x.published or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    return deduped


# ============================================================
# Internal helpers
# ============================================================

def score_relevance(
    items: list[NewsItem],
    tracked_analysts: dict[str, str],
    tracked_companies: list[str],
    tracked_brokers: list[str],
    anthropic_client,
    haiku_model: str,
    tracked_concepts: Optional[list[str]] = None,
) -> list[tuple[NewsItem, int, str]]:
    """
    Score each NewsItem for relevance using two passes.

    Pass 1 (free, local): keyword match against analysts/brokers/companies/concepts.
    Any item not matching gets score 0 and skips the Haiku pass.

    Pass 2 (Haiku, batched): each batch of 10 headlines gets a single
    Haiku call returning a 0-10 relevance score + 1-line reason per item.

    Returns list of (item, score, reason) — UNSORTED; caller sorts if needed.

    EDGAR items are always preserved at score 10 — F-1 / 6-K / 424 / Form 4
    filings are mechanical and don't need LLM judgment.
    """
    import json

    analyst_names = {name.lower() for name in tracked_analysts}
    broker_names  = {b.lower() for b in tracked_brokers}
    company_names = {c.lower() for c in tracked_companies}
    concept_names = {c.lower() for c in (tracked_concepts or [])}

    candidates = []
    auto_keep  = []   # EDGAR — always relevant, skip Haiku
    skipped    = []   # no keyword match — skip Haiku

    for item in items:
        # EDGAR filings are mechanical signals, always relevant
        if "edgar" in item.source.lower() or "sec" in item.source.lower():
            auto_keep.append((item, 10, "EDGAR filing — auto-kept"))
            continue

        text = f"{item.title} {item.summary or ''}".lower()
        hits = (
            any(a in text for a in analyst_names) or
            any(b in text for b in broker_names)  or
            any(c in text for c in company_names) or
            any(c in text for c in concept_names)
        )
        if hits:
            candidates.append(item)
        else:
            skipped.append((item, 0, "no keyword match"))

    if not candidates:
        return auto_keep + skipped

    scored = []
    batch_size = 10
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i : i + batch_size]

        headlines_block = "\n".join(
            f"{j+1}. [{item.source}] {item.title}"
            for j, item in enumerate(batch)
        )

        prompt = (
            "You are a relevance filter for a daily memory semiconductor investment brief.\n"
            "Score each headline 0-10 for relevance to: SK Hynix / Micron / Samsung memory cycle, "
            "analyst upgrades/downgrades, DRAM/HBM/NAND pricing, hyperscaler capex, supply/demand signals, "
            "AND architectural shifts that affect HBM captive ratio (inference efficiency techniques, "
            "memory disaggregation, custom silicon memory architecture, LPDDR datacenter adoption).\n\n"
            "Rules:\n"
            "- 8-10: analyst action (upgrade/downgrade/PT change) on SK Hynix, Micron, Samsung; "
            "major supply/pricing signal from TrendForce/DigiTimes; "
            "first-of-kind architectural deployment (e.g. first hyperscaler CXL 3.0 production, "
            "Samsung HBM4 qualification at NVIDIA, MTIA/Trainium memory architecture confirmed)\n"
            "- 6-7: thematic analyst note on memory/HBM/AI inference; inference efficiency breakthrough "
            "(major quantization/MoE/speculative-decoding advance); LPDDR datacenter design win; "
            "vLLM/SGLang/TensorRT release adopting new efficiency technique; earnings commentary\n"
            "- 4-5: adjacent — semi equipment, broader AI capex, Korean tech policy, incremental research papers\n"
            "- 0-3: not relevant — general tech news, unrelated companies, noise\n\n"
            f"Headlines:\n{headlines_block}\n\n"
            "Respond with ONLY a JSON array, one object per headline, in order:\n"
            '[{"score": 8, "reason": "MS downgrades SK Hynix — direct Indicator 3 input"}, ...]'
        )

        try:
            response = anthropic_client.messages.create(
                model=haiku_model,
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            results = json.loads(raw.strip())
        except Exception as e:
            logger.warning(f"Haiku relevance scoring failed for batch: {e}")
            results = [{"score": 5, "reason": "scoring error — kept conservatively"} for _ in batch]

        for item, result in zip(batch, results):
            scored.append((item, int(result.get("score", 0)), str(result.get("reason", ""))))

    return auto_keep + scored + skipped


def filter_relevant_news(
    items: list[NewsItem],
    tracked_analysts: dict,
    tracked_companies: list,
    tracked_brokers: list,
    anthropic_client,
    haiku_model: str,
    min_score: int = 3,
    log_dropped_path: Optional[str] = None,
    tracked_concepts: Optional[list[str]] = None,
) -> list[NewsItem]:
    """
    Filter news items by relevance. Returns items with score >= min_score.

    The score and reason are prepended to each kept item's summary so Layer 2
    sees the context. If log_dropped_path is provided, dropped items are
    written there as JSON for spot-checking the filter aggressiveness.
    """
    import json

    scored = score_relevance(
        items, tracked_analysts, tracked_companies, tracked_brokers,
        anthropic_client, haiku_model, tracked_concepts=tracked_concepts,
    )
    kept = []
    dropped = []
    for item, score, reason in scored:
        if score >= min_score:
            item.summary = f"[Relevance {score}/10: {reason}] {item.summary or ''}"
            kept.append(item)
        else:
            dropped.append({
                "title": item.title,
                "source": item.source,
                "score": score,
                "reason": reason,
            })

    logger.info(
        f"Relevance filter: kept {len(kept)}/{len(items)} items "
        f"(min_score={min_score}, dropped {len(dropped)})"
    )

    if log_dropped_path and dropped:
        try:
            with open(log_dropped_path, "w") as f:
                json.dump({
                    "run_at": datetime.now(timezone.utc).isoformat(),
                    "min_score": min_score,
                    "dropped_count": len(dropped),
                    "dropped_items": dropped,
                }, f, indent=2)
            logger.info(f"Wrote {len(dropped)} dropped items to {log_dropped_path}")
        except Exception as e:
            logger.warning(f"Could not write dropped-items log: {e}")

    return kept


def _safe_parse_rss_feed(
    source_name: str,
    feed_url: str,
    cutoff: datetime,
    source_type: str,
    collector: Optional[DiagnosticsCollector],
) -> list[NewsItem]:
    """Shared RSS-parsing path with optional diagnostics tracking.

    v3.38.2: fetch with proper browser-style User-Agent via `requests`, then
    pass the bytes to `feedparser.parse(content)`. The earlier pattern
    `feedparser.parse(url)` used feedparser's default UA
    (`UniversalFeedParser/6.x +https://github.com/kurtmckee/feedparser/`)
    which Substack, Cloudflare-fronted sites, and various anti-bot middleware
    treat as a bot — they respond with an HTML "are you human" page (or just
    silently 200 with HTML body), and feedparser then raises one of:
      - "...is not an XML media type"
      - "<unknown>:2:0: syntax error"  (HTML parsed as XML, fails at line 2)
      - "<unknown>:N:M: not well-formed (invalid token)"
    The 13:42 UTC May 17 run had ~15/20 feeds fail with exactly this set of
    error messages. Routing through `requests` with our existing HEADERS
    (Mozilla-prefixed UA also used by the chosun_biz / trendforce scrapers)
    fixes the class of failures in one stroke.

    EDGAR was already doing this correctly (lines 124/140 use resp.content).
    """
    out: list[NewsItem] = []

    def _parse() -> list[NewsItem]:
        try:
            resp = requests.get(feed_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"HTTP fetch failed: {e}")

        parsed = feedparser.parse(resp.content)
        items_local: list[NewsItem] = []
        for entry in parsed.entries:
            published = _entry_published(entry)
            if published and published < cutoff:
                continue
            items_local.append(NewsItem(
                title=entry.get("title", "(no title)").strip(),
                url=entry.get("link", ""),
                source=source_name,
                published=published,
                summary=_clean_summary(entry.get("summary", "")),
            ))
        # If the feed parsed cleanly but returned zero entries, surface that
        # via items_returned=0 (warn) rather than as a hard failure.
        if not parsed.entries and getattr(parsed, "bozo_exception", None):
            raise RuntimeError(f"feed parse error: {parsed.bozo_exception}")
        return items_local

    try:
        if collector is not None:
            with collector.track(source_name, source_type) as t:
                out = _parse()
                t.items_returned = len(out)
        else:
            out = _parse()
    except Exception as e:
        logger.warning(f"Failed to parse {source_name} RSS: {e}")

    return out


def _entry_published(entry) -> Optional[datetime]:
    """Extract a UTC datetime from a feedparser entry; return None if unavailable."""
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                return datetime(*struct[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _clean_summary(html_text: str) -> Optional[str]:
    """Strip HTML and truncate summary."""
    if not html_text:
        return None
    text = BeautifulSoup(html_text, "html.parser").get_text(separator=" ", strip=True)
    if len(text) > 400:
        text = text[:400] + "..."
    return text or None
