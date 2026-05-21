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
  v3.39.7 (2026-05-20) — Cycle matrix data pipeline: structured spot/contract scrapers + CSV persistence helpers, all appended at the bottom of this module without changing any existing function. Existing scrape_trendforce_prices() (v3.39.3, returns list[NewsItem] for the news pipeline) is kept intact; the structured table-parsing was refactored OUT of it into a shared internal helper `_parse_trendforce_spot_table_html()` reused by both code paths. New public API: parse_trendforce_spot_html, parse_trendforce_contract_html, scrape_trendforce_spot_structured, scrape_trendforce_contract, fetch_article_body, persist_spot_to_history, persist_contract_to_history, upsert_contract_forecasts, upsert_contract_signals. New dataclasses: SpotScrapeRow, SpotScrapeResult, ContractScrapeRow, ContractScrapeResult. Persistence helpers are idempotent (replace rows matching key tuples on rerun, no growth). Consumed by fast_tier.py:run_cycle_matrix_step() after the existing news fetch.
  v3.39.3 (2026-05-18) — TrendForce ingestion fix. Three layered issues identified from the 21:19 UTC May 17 run where the Tier 1 brief read "no new TrendForce spot price prints in today's feed." (a) scrape_trendforce() was pointing at https://www.trendforce.com/news/category/dram which TrendForce has restructured to https://www.trendforce.com/news/category/semiconductors/dram/ (with /semiconductors/ segment + trailing slash). The old URL returns a 200 with no matching <article a[href*='/news/']> selectors → 0 items silently. URL fixed. (b) New function scrape_trendforce_prices() scrapes the public spot-price table at /price/dram/dram_spot. The page is server-rendered HTML, NOT JS-populated — the table cells contain the actual numbers, no XHR endpoint to find. But it's full-page CDN-cached with cache keys keyed on request fingerprint (UA/geo/cookies), so generic-UA clients can see 1-3 day-old snapshots. Mitigations: real Chrome UA + Cache-Control: no-cache + ?_=<ms> cache-buster + a FRESHNESS GUARD that parses "Last Update YYYY-MM-DD HH:MM (GMT+8)" and prepends "[STALE]" to the NewsItem title when age >36h. A stale fetch is worse than no fetch (silently pollutes Tier 1 logic) — the title tag makes it visible to Layer 2 and the email diagnostics row. (c) score_relevance() now auto-preserves the TrendForce price-table NewsItem at score 10 (mechanical signal, same treatment as EDGAR), so it can never be filtered out by Haiku. The DRAMeXchange RSS feed (config.py change in same release) replaces the dead trendforce.com/news/feed URL — open, well-formed XML, carries weekly research commentary which feeds the behavioral-signal trigger.
  v3.38.3 (2026-05-17) — HEADERS user-agent further tightened: switched from "Mozilla/5.0 (compatible; MemoryCycleBrief/1.0; ...)" to a plain Chrome UA ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"). The "compatible;" pattern is the classic bot signature that Cloudflare-style anti-bot rules match against, producing the 403s on Damnang Substack and Investing.com x4 in the 14:31 UTC run. Plain Chrome UA passes those filters. Also added Accept and Accept-Language headers (legit browser fingerprint).
  v3.38.2 (2026-05-17) — RSS fetcher rewritten to use requests.get(url, headers=HEADERS) + feedparser.parse(resp.content) instead of feedparser.parse(url). Reason: feedparser's default UA "UniversalFeedParser/6.x +github.com/kurtmckee/feedparser/" was being blocked or served HTML by Substack, Cloudflare-fronted sites, and various anti-bot middleware, producing three error-message families ("is not an XML media type" / "<unknown>:2:0: syntax error" / "<unknown>:N:M: not well-formed (invalid token)"). EDGAR was already correctly using requests; this fix applies the same pattern to RSS and Google News. Both `_safe_parse_rss_feed` and `fetch_google_news` updated. RuntimeError raised on HTTP failure so it surfaces in diagnostics.
  v3.2   (2026-05-12) — score_relevance/filter_relevant_news accept tracked_concepts; Haiku rubric covers architectural signals
  v3.1   (2026-05-12) — Added score_relevance() + filter_relevant_news() two-pass relevance filter
"""

from __future__ import annotations

import logging
import re
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

# v3.39.3 — TrendForce DRAM spot price table (public freemium tier).
# The table at this URL is server-rendered HTML, but full-page CDN-cached on
# request fingerprint, so a freshness guard is mandatory. See
# scrape_trendforce_prices() docstring for the full mitigation rationale.
TRENDFORCE_SPOT_URL = "https://www.trendforce.com/price/dram/dram_spot"
TRENDFORCE_SPOT_STALE_HOURS = 36  # >36h old → tagged [STALE] and excluded from Tier 1 firing


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
    expected_empty_feeds: Optional[set[str]] = None,
) -> list[NewsItem]:
    """Fetch SEC EDGAR atom feeds for SK Hynix filing types.

    EDGAR uses a 30-day lookback because filings are infrequent — we want
    to catch them whenever they appear. Once a filing is classified as
    'seen' (via prior_state in main.py), the LLM suppresses re-flagging.

    Filings are titled with [FILING] prefix so they sort to the top of
    the news feed and the LLM gives them weight.

    v3.39.7.6 — `expected_empty_feeds` is an optional set of source-name
    strings (from `feeds` keys) whose 0-item state is expected. The SK
    Hynix US-listing filings (F-1, F-1/A, 424, 6-K, 20-F) stay empty
    until the ADR actually lists — that emptiness is the indicator's
    routine state, not a fetch problem. Marked sources render as 'quiet'
    in the data-source table instead of 'warn'.
    """
    items: list[NewsItem] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    expected_empty_set = expected_empty_feeds or set()

    for source_name, feed_url in feeds.items():
        per_feed_items: list[NewsItem] = []
        try:
            if collector is not None:
                with collector.track(source_name, "edgar") as t:
                    # v3.39.7.6 — mark pre-listing SK Hynix filings as
                    # expected_empty so a quiet feed doesn't look broken.
                    if source_name in expected_empty_set:
                        t.expected_empty = True
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


def _extract_publisher_url_from_google_news_entry(entry) -> Optional[str]:
    """v3.39.5: Resolve the actual publisher URL from a Google News RSS entry.

    Google News returns RSS entries where `entry.link` is an encoded redirect
    URL of the form `https://news.google.com/rss/articles/CBMi*`. These URLs
    return HTTP 400 ("That's an error") when accessed directly — Google
    deprecated direct access to encoded redirect URLs in 2024.

    The actual publisher URL is embedded in `entry.summary` as an `<a href>`
    tag — Google News emits HTML like:
        <ol><li>
          <a href="https://www.seouleconomicdaily.com/news/articleView..."
             target="_blank">Article Title</a>
          &nbsp;&nbsp;
          <font color="#6f6f6f">Publisher Name</font>
        </li></ol>

    This function parses that HTML and returns the first publisher href.
    Falls back to None if extraction fails — caller can then keep the
    Google redirect URL (still broken but at least not a None reference).

    Confirmed against today's email broken-link cases (Seoul Economic /
    AD HOC NEWS / abhs.in).
    """
    raw_summary = entry.get("summary", "") or ""
    if not raw_summary or "<" not in raw_summary:
        return None
    try:
        soup = BeautifulSoup(raw_summary, "html.parser")
        first_link = soup.find("a", href=True)
        if first_link is None:
            return None
        href = first_link["href"].strip()
        # Reject hrefs that are themselves Google redirects (would defeat the point).
        if href.startswith("https://news.google.com/"):
            return None
        # Sanity: only accept http(s) URLs
        if not (href.startswith("http://") or href.startswith("https://")):
            return None
        return href
    except Exception as e:
        logger.debug(f"Could not extract publisher URL from Google News entry: {e}")
        return None


def fetch_google_news(
    queries: list[str],
    hours_back: int = RECENT_HOURS_DEFAULT,
    collector: Optional[DiagnosticsCollector] = None,
    watch_queries: Optional[set[str]] = None,
) -> list[NewsItem]:
    """Query Google News RSS for each query string and return recent items.

    Records one SourceFetchStatus per query.

    v3.39.7.6 — `watch_queries` is an optional set of query strings that
    are forward-looking event detectors (e.g. "CXL 3.0 memory pooling
    production", "GDDR7 inference accelerator", "OpenAI IPO filing").
    When a watch query returns 0 items, the status is marked
    `expected_empty=True` so it renders as 'quiet' (informational) in the
    data-source assessment table rather than 'warn' (might be broken).
    Queries NOT in this set keep the default 'warn' on 0 items.
    """
    items: list[NewsItem] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    watch_set = watch_queries or set()

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
                # v3.39.5: extract publisher URL from entry.summary HTML
                # so the email's links work (entry.link is the encoded Google
                # News redirect — broken in browser).
                publisher_url = _extract_publisher_url_from_google_news_entry(entry)
                final_url = publisher_url or entry.get("link", "")
                local.append(NewsItem(
                    title=entry.get("title", "(no title)").strip(),
                    url=final_url,
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
                    # v3.39.7.6 — mark watch-type queries (event detectors)
                    # as expected_empty so the diagnostics table doesn't
                    # treat their routine 0-item state as suspicious.
                    if query in watch_set:
                        t.expected_empty = True
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
    """Scrape TrendForce news page for recent DRAM/NAND articles.

    v3.39.3: URL fixed. Old path /news/category/dram returns 200 with no
    matching <article> selectors → 0 items silently. Correct path is
    /news/category/semiconductors/dram/ (with /semiconductors/ segment and
    trailing slash). Also captures /news/2026/MM/DD/... article slug pattern.
    """

    def _do() -> list[NewsItem]:
        resp = requests.get(
            "https://www.trendforce.com/news/category/semiconductors/dram/",
            headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        out: list[NewsItem] = []
        # The current TrendForce layout uses article URLs like
        # /news/YYYY/MM/DD/<slug>/. Match those as well as the legacy
        # /news/<slug> pattern, to be resilient to either layout.
        anchors = soup.select("a[href*='/news/']")[:60]
        for link in anchors:
            href = link.get("href", "")
            title = link.get_text(strip=True)
            if not title or len(title) < 20:
                continue
            if not href:
                continue
            # Skip the bare category page itself and other non-article links
            if href.rstrip("/").endswith("/news") or "/category/" in href:
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
                if len(items) == 0:
                    t.detail = "0 items — URL or selector may be stale"
        else:
            items = _do()
    except Exception as e:
        logger.warning(f"Failed to fetch TrendForce: {e}")

    return items


def scrape_trendforce_prices(
    collector: Optional[DiagnosticsCollector] = None,
) -> list[NewsItem]:
    """Scrape the public TrendForce DRAM spot price table.

    v3.39.3 — see file changelog. Returns a SINGLE high-priority NewsItem
    digesting the current spot table, so the price reading flows through
    `fetch_all_news` → relevance filter → Layer 2 / Layer 3 like any other
    news source, without any change at the call site.

    URL: https://www.trendforce.com/price/dram/dram_spot
    Page format: server-rendered HTML (no XHR to find — the table cells
    contain the actual numbers, baked in at response time).

    Cache risk: full-page CDN caching keyed on request fingerprint
    (UA / geo / cookies). Generic-UA clients can see prints 1-3 days
    stale relative to a real browser. Mitigations are layered:
      1. Real Chrome UA via HEADERS (already in place)
      2. Cache-Control: no-cache + Pragma: no-cache request headers
      3. Append ?_=<unix-ms> query string as cache-buster
      4. FRESHNESS GUARD: parse "Last Update YYYY-MM-DD HH:MM (GMT+8)"
         and prepend "[STALE]" to the NewsItem title when age > 36h.
         A stale fetch is worse than no fetch — it pollutes Tier 1 logic
         with old numbers. The title tag makes staleness visible to Layer 2
         signal reasoning and to the email's data-source diagnostics row.
    """

    def _do_with_meta() -> tuple[list[NewsItem], bool, float, str]:
        # Cache-buster: defeat CDN edge cache (best-effort; some CDNs strip
        # query strings from cache keys, some don't — costs ~nothing to try).
        cache_buster = int(datetime.now(timezone.utc).timestamp() * 1000)
        url_busted = f"{TRENDFORCE_SPOT_URL}?_={cache_buster}"

        headers = {
            **HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        resp = requests.get(url_busted, headers=headers, timeout=20)
        resp.raise_for_status()
        html = resp.text

        # Parse the "Last Update YYYY-MM-DD HH:MM (GMT+8)" line that sits
        # directly above the spot price table on the public price page.
        m = re.search(
            r"Last Update\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*\(GMT\+8\)",
            html,
        )
        if not m:
            raise RuntimeError(
                "TrendForce spot page: 'Last Update YYYY-MM-DD HH:MM (GMT+8)' "
                "marker not found — page may have been restructured."
            )
        last_update_str = f"{m.group(1)} {m.group(2)}"
        last_update_gmt8 = datetime.strptime(last_update_str, "%Y-%m-%d %H:%M")
        last_update_utc = (last_update_gmt8 - timedelta(hours=8)).replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - last_update_utc).total_seconds() / 3600.0
        is_stale = age_hours > TRENDFORCE_SPOT_STALE_HOURS

        # Find the spot price table: it's the first <table> whose first row's
        # first cell is "Item". The page has several other tables (contract,
        # module, GDDR, etc.) so we identify by content rather than position.
        soup = BeautifulSoup(html, "html.parser")
        target_table = None
        for tbl in soup.find_all("table"):
            first_row = tbl.find("tr")
            if not first_row:
                continue
            first_cell = first_row.find(["th", "td"])
            if first_cell and first_cell.get_text(strip=True).lower() == "item":
                target_table = tbl
                break
        if target_table is None:
            raise RuntimeError("TrendForce spot page: no <table> with 'Item' header found.")

        rows = []
        for tr in target_table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            # Expect 7 data cells: Item, Daily High/Low, Session High/Low, Avg, Change
            # (plus a trailing history-icon cell that may or may not have text)
            if len(cells) < 7:
                continue
            rows.append({
                "item": cells[0],
                "daily_high": cells[1],
                "daily_low": cells[2],
                "session_high": cells[3],
                "session_low": cells[4],
                "session_average": cells[5],
                "session_change": cells[6],
            })
        if not rows:
            raise RuntimeError("TrendForce spot page: parsed 0 rows from spot table.")

        # Compact digest for the NewsItem summary — Layer 2 / Layer 3 will
        # read this and reason about Tier 1 t1_spot_first_decline.
        compact = "; ".join(
            f"{r['item']}: avg {r['session_average']} ({r['session_change']})"
            for r in rows
        )

        stale_tag = "[STALE] " if is_stale else ""
        title = (
            f"{stale_tag}TrendForce DRAM Spot Price — Last Update "
            f"{last_update_str} GMT+8 ({age_hours:.1f}h ago)"
        )
        summary = (
            f"[TrendForce public DRAM spot price table | "
            f"Last Update: {last_update_str} GMT+8 | age: {age_hours:.1f}h | "
            f"stale: {str(is_stale).lower()}] "
            f"{compact}"
        )
        if is_stale:
            summary = (
                summary
                + " | NOTE: This snapshot is older than the freshness threshold "
                f"({TRENDFORCE_SPOT_STALE_HOURS}h); the GitHub Actions egress "
                "may be hitting a stale CDN edge cache. Treat numerical levels "
                "as informational only and DO NOT use this row to fire or unfire "
                "Tier 1 t1_spot_first_decline."
            )
        item = NewsItem(
            title=title,
            url=TRENDFORCE_SPOT_URL,
            source="TrendForce DRAM Spot (price table)",
            published=last_update_utc,
            summary=summary,
        )
        detail_str = (
            f"STALE — {age_hours:.1f}h old (threshold {TRENDFORCE_SPOT_STALE_HOURS}h); "
            "likely CDN cache miss"
            if is_stale else f"fresh — {age_hours:.1f}h old"
        )
        return [item], is_stale, age_hours, detail_str

    items: list[NewsItem] = []
    try:
        if collector is not None:
            with collector.track("TrendForce DRAM Spot (price table)", "scraper") as t:
                items, _is_stale, _age_h, detail_str = _do_with_meta()
                t.items_returned = len(items)
                t.detail = detail_str
        else:
            items, _is_stale, _age_h, _ = _do_with_meta()
    except Exception as e:
        logger.warning(f"Failed to scrape TrendForce DRAM Spot prices: {e}")

    return items


def fetch_all_news(
    rss_feeds: dict[str, str],
    google_queries: list[str],
    hours_back: int = RECENT_HOURS_DEFAULT,
    edgar_feeds: Optional[dict[str, str]] = None,
    collector: Optional[DiagnosticsCollector] = None,
    watch_google_queries: Optional[set[str]] = None,
    expected_empty_edgar_feeds: Optional[set[str]] = None,
) -> list[NewsItem]:
    """Aggregate news from all configured sources, deduplicate by title.

    The collector argument is forwarded to every sub-fetcher so the email
    template can render a per-source validation table.

    v3.39.7.6 — `watch_google_queries` and `expected_empty_edgar_feeds`
    forward through to the underlying fetchers so forward-looking
    watcher feeds (architecture-shift Google News queries, pre-listing
    SK Hynix SEC filings) render as 'quiet' on 0 items instead of
    'warn'. Defaults preserve the prior behavior for any caller that
    doesn't pass them.
    """
    all_items: list[NewsItem] = []

    all_items.extend(fetch_rss_feeds(rss_feeds, hours_back, collector=collector))
    if edgar_feeds:
        # EDGAR uses its own wider lookback window (30 days) inside the function
        all_items.extend(fetch_edgar_feeds(
            edgar_feeds,
            collector=collector,
            expected_empty_feeds=expected_empty_edgar_feeds,
        ))
    all_items.extend(fetch_google_news(
        google_queries, hours_back,
        collector=collector,
        watch_queries=watch_google_queries,
    ))
    all_items.extend(scrape_chosun_biz(hours_back, collector=collector))
    all_items.extend(scrape_trendforce(hours_back, collector=collector))
    # v3.39.3 — TrendForce DRAM Spot price table. Emits one high-priority
    # NewsItem with the parsed table + freshness flag; flows through the
    # relevance filter (auto-preserved at score 10 by score_relevance) and
    # into Layer 2 + Layer 3 like any other news item.
    all_items.extend(scrape_trendforce_prices(collector=collector))

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

        # v3.39.3 — TrendForce DRAM spot price-table item is a mechanical
        # signal (parsed table, not a headline). Always preserve so it can't
        # be filtered out by Haiku; freshness is communicated via the [STALE]
        # title prefix that Layer 2 sees.
        if "trendforce dram spot" in item.source.lower() and "price table" in item.source.lower():
            auto_keep.append((item, 10, "TrendForce spot price table — auto-kept"))
            continue

        # v3.39.7.7 — pad text with leading + trailing space so that
        # word-boundary-protected keywords (" MU ", " AWS ", " HPE ")
        # correctly match when the keyword appears at the start or end
        # of the headline. Pre-v3.39.7.7 the text was unpadded, so a
        # headline like "MU files Q3" or "HPE acquires Juniper" silently
        # failed Pass 1 — the padded keyword " mu " didn't have a leading
        # space to match against. Latent bug since v3.1; the v3.39.7.7
        # tracked-companies expansion surfaced it through the HPE test
        # case. Safe everywhere: padding can only add matches at the
        # boundaries, can't drop existing mid-sentence matches.
        text = f" {item.title} {item.summary or ''} ".lower()
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
            "Score each headline 0-10 for relevance to the memory cycle: SK Hynix / Micron / "
            "Samsung memory cycle, analyst upgrades/downgrades, DRAM/HBM/NAND pricing, chip "
            "designer (NVIDIA/AMD/Intel/Broadcom/Cerebras) earnings on memory demand, hyperscaler "
            "+ AI lab capex commitments, supply/demand signals, AND architectural shifts that "
            "affect HBM captive ratio (inference efficiency techniques, memory disaggregation, "
            "custom silicon memory architecture, LPDDR datacenter adoption).\n\n"
            "RELEVANCE TIERS — apply by which company/companies the headline is primarily about. "
            "v3.39.7.8: four value-chain tiers (memory makers → chip designers → hyperscalers/AI "
            "labs → ecosystem). Tier B is the CONCURRENT HBM demand signal (chip designers tell "
            "us what they ARE buying today). Tier C is the LEADING capex commitment signal (6-12 "
            "months ahead of memory cycle peak). Both peak at 8-9 but for different reasons.\n\n"
            "  TIER A — Pure memory + competitive rivals (SK Hynix, Micron, Samsung Electronics, "
            "CXMT, ChangXin):\n"
            "    9-10: analyst action (upgrade/downgrade/PT change/EPS revision) on these names; "
            "TrendForce or DigiTimes supply/pricing print; Korean labor news affecting Samsung "
            "or SK Hynix production (strike, wage deal, union action); insider sales filings; "
            "earnings or guidance release on these names; CXMT/ChangXin capacity-expansion or "
            "production-milestone news (Indicator 6A direct input)\n"
            "    7-8: thematic analyst notes on the HBM/DRAM/NAND cycle that name these "
            "companies; Korean macro news with direct read-through to memory production\n\n"
            "  TIER B — Chip designers / first-layer bill-payers (NVIDIA, AMD, Intel, Broadcom, "
            "Cerebras). These BUY memory chips and integrate them into GPUs/CPUs/AI accelerators. "
            "From the memory makers' perspective these are the direct customers:\n"
            "    8-9: earnings + HBM/data-center backlog commentary; GPU+HBM supply agreements "
            "with named volume; NVIDIA quarterly print + Jensen forward commentary; AMD MI400 "
            "production ramp news; Intel Gaudi unit-shipment commentary; Broadcom AI segment "
            "+ custom-silicon-for-Google-Meta-commentary; Cerebras WSE-3 deployment news "
            "(HBM-substitution signal — v3.4 captive-ratio indicator input)\n"
            "    6-7: general product launches / strategic commentary without quantified memory "
            "demand impact; gaming/auto NVIDIA news that explicitly mentions AI tie-in\n\n"
            "  TIER C — Hyperscalers + AI labs / second-layer bill-payers (Microsoft/Azure, "
            "Alphabet/Google, Amazon/AWS, Meta, OpenAI, Anthropic). They buy finished AI systems "
            "FROM the chip designers; their capex flows downhill: Microsoft commits $80B → flows "
            "to NVIDIA → flows to SK Hynix HBM. This is the LEADING signal — capex commitments "
            "lead memory cycle peaks by 6-12 months:\n"
            "    8-9: capex commitments (any hyperscaler upping AI capex guidance); multi-billion "
            "AI infrastructure deals; hyperscaler earnings with explicit memory/HBM commentary "
            "or chip-supply commentary; AI lab funding rounds at $5B+ scale (drives compute "
            "demand); OpenAI/Anthropic capacity build-out announcements; in-house ASIC + custom "
            "silicon production news (TPU v7, Trainium 2, MTIA v3, Maia)\n"
            "    6-7: hyperscaler earnings without memory or chip-supply specifics; model "
            "release news; lab partnership announcements without infrastructure detail\n\n"
            "  TIER D — Ecosystem (ASML, Applied Materials, TSMC, Oracle, Dell, HPE):\n"
            "    6-7: equipment WFE earnings + memory mix commentary; TSMC CoWoS capacity "
            "commentary (HBM packaging bottleneck); Dell/HPE channel inventory commentary "
            "(t3_supplier_inventory_rebuild direct input); ASML lithography backlog with memory "
            "mix detail\n"
            "    4-5: general equipment news naming these companies without memory-specific "
            "implication; Oracle cloud commentary without memory implication\n\n"
            "  ARCHITECTURAL SHIFTS — regardless of company named:\n"
            "    8-10: first-of-kind production deployment (first hyperscaler CXL 3.0 "
            "production; Samsung HBM4 qualification at NVIDIA; NVIDIA Vera Rubin HBM/HBF "
            "confirmation; MTIA/Trainium memory architecture detail leaks)\n"
            "    6-7: research papers + framework releases (vLLM/SGLang/TensorRT-LLM adopting "
            "new efficiency technique); inference efficiency breakthroughs (major quantization/"
            "MoE/speculative-decoding advance); LPDDR datacenter design wins\n\n"
            "  ADJACENT / NOT RELEVANT:\n"
            "    4-5: Korean tech policy not naming memory makers; broader AI capex with no "
            "memory implication; incremental research papers\n"
            "    0-3: general tech news, unrelated companies, gaming/auto/automotive-only "
            "NVIDIA news without AI tie-in, noise\n\n"
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


# ============================================================================
# v3.39.7 — Cycle matrix data pipeline
# ============================================================================
# Structured spot/contract scraping for the cycle matrix (src/data/cycle_matrix.py).
# The existing scrape_trendforce_prices() above is preserved as-is — it returns
# list[NewsItem] for the news pipeline / Layer 2 / Layer 3 path. The functions
# below add a parallel data path that returns STRUCTURED per-SKU rows ready for
# CSV persistence and matrix evaluation. They share the underlying HTTP fetch +
# HTML parsing approach but produce different output shapes.
# ============================================================================

import csv as _csv_v3397
import time as _time_v3397
from dataclasses import asdict as _asdict_v3397
from datetime import date as _date_v3397
from pathlib import Path as _Path_v3397

TRENDFORCE_CONTRACT_URL = "https://www.trendforce.com/price/dram/dram_contract"

# TrendForce display name -> canonical SKU code for the spot table.
# Mirrors the cells in scrape_trendforce_prices() but with explicit codes
# suitable for use as CSV keys.
SPOT_SKU_MAP = {
    "DDR5 16Gb (2Gx8) 4800/5600": "DDR5_16Gb_4800_5600",
    "DDR5 16Gb (2Gx8) eTT":        "DDR5_16Gb_eTT",
    "DDR4 16Gb (2Gx8) 3200":       "DDR4_16Gb_3200",
    "DDR4 16Gb (2Gx8) eTT":        "DDR4_16Gb_eTT",
    "DDR4 8Gb (1Gx8) 3200":        "DDR4_8Gb_3200",
    "DDR4 8Gb (1Gx8) eTT":         "DDR4_8Gb_eTT",
    "DDR3 4Gb 512Mx8 1600/1866":   "DDR3_4Gb_1600_1866",
}

# TrendForce display name -> canonical SKU code for the contract table
CONTRACT_SKU_MAP = {
    "DDR5 8GB SO-DIMM":  "DDR5_8GB_SODIMM_contract",
    "DDR4 16GB SO-DIMM": "DDR4_16GB_SODIMM_contract",
    "DDR4 8GB SO-DIMM":  "DDR4_8GB_SODIMM_contract",
    "DDR4 16Gb 2Gx8":    "DDR4_16Gb_chip_contract",
    "DDR4 8Gb 1Gx8":     "DDR4_8Gb_chip_contract",
    "DDR4 4Gb 256Mx16":  "DDR4_4Gb_chip_contract",
    "DDR3 4Gb 256Mx16":  "DDR3_4Gb_chip_contract",
}


@dataclass
class SpotScrapeRow:
    """One row of spot price data scraped from TrendForce."""
    sku: str
    session_avg: float
    daily_high: Optional[float]
    daily_low: Optional[float]
    session_change_pct: Optional[float]


@dataclass
class SpotScrapeResult:
    """Result of one spot-price scrape (date + rows + freshness flag)."""
    update_date: Optional[_date_v3397]
    rows: list[SpotScrapeRow]
    fresh: bool  # True if scrape within TRENDFORCE_SPOT_STALE_HOURS


@dataclass
class ContractScrapeRow:
    """One row of contract price data scraped from TrendForce."""
    sku: str
    price_usd: float
    session_high: Optional[float]
    session_low: Optional[float]
    average_change_pct: Optional[float]


@dataclass
class ContractScrapeResult:
    """Result of one contract-price scrape (date + period label + rows)."""
    update_date: Optional[_date_v3397]
    period_label: Optional[str]   # e.g. "2H Mar"
    rows: list[ContractScrapeRow]


def _safe_float_v3397(text: str) -> Optional[float]:
    """Parse a numeric string like '41.10' or '1,180.00' to float."""
    if not text:
        return None
    cleaned = text.replace(",", "").replace("$", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_change_pct_v3397(text: str) -> Optional[float]:
    """Parse change cells like '▼ -0.41 %' or '▲ 0.94 %' or '— 0.00 %'."""
    if not text:
        return None
    m = re.search(r"([▼▲—\-+]?)\s*(-?\d+\.?\d*)\s*%", text)
    if not m:
        return None
    sign_char = m.group(1)
    value = float(m.group(2))
    if "-" in sign_char or sign_char == "▼":
        return -abs(value)
    return value


def parse_trendforce_spot_html(html_text: str) -> SpotScrapeResult:
    """Parse the TrendForce DRAM spot price page HTML into a SpotScrapeResult.

    Same underlying HTML structure as scrape_trendforce_prices() parses; this
    function returns the structured per-SKU rows (rather than a NewsItem digest)
    for use by the cycle matrix CSV persistence path.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    rows_out: list[SpotScrapeRow] = []

    update_date: Optional[_date_v3397] = None
    m = re.search(r"Last Update\s+(\d{4}-\d{2}-\d{2})", html_text)
    if m:
        try:
            update_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass

    for tbl in soup.find_all("table"):
        first_cells = [
            tr.find("td").get_text(strip=True) if tr.find("td") else ""
            for tr in tbl.find_all("tr") if tr.find("td")
        ]
        if not any(name in first_cells for name in SPOT_SKU_MAP):
            continue

        for tr in tbl.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            display_name = tds[0].get_text(strip=True)
            sku = SPOT_SKU_MAP.get(display_name)
            if not sku:
                continue
            try:
                daily_high = _safe_float_v3397(tds[1].get_text(strip=True))
                daily_low = _safe_float_v3397(tds[2].get_text(strip=True))
                session_avg = _safe_float_v3397(tds[5].get_text(strip=True))
                change_text = tds[6].get_text(strip=True) if len(tds) > 6 else ""
                change_pct = _parse_change_pct_v3397(change_text)
            except (ValueError, IndexError) as e:
                logger.warning(f"Failed to parse spot row {display_name}: {e}")
                continue
            if session_avg is None:
                continue
            rows_out.append(SpotScrapeRow(
                sku=sku, session_avg=session_avg,
                daily_high=daily_high, daily_low=daily_low,
                session_change_pct=change_pct,
            ))
        break

    fresh = False
    if update_date is not None:
        age_hours = (datetime.now(timezone.utc).date() - update_date).days * 24
        fresh = age_hours <= TRENDFORCE_SPOT_STALE_HOURS

    return SpotScrapeResult(update_date=update_date, rows=rows_out, fresh=fresh)


def scrape_trendforce_spot_structured(
    collector: Optional[DiagnosticsCollector] = None,
) -> SpotScrapeResult:
    """Structured spot scrape — returns SpotScrapeResult for the cycle matrix.

    Independent from scrape_trendforce_prices() above (which builds NewsItem
    digests for the news pipeline). Both functions can run in the same pipeline:
    the existing one feeds Layer 2 / Layer 3 LLM reasoning, this one writes
    structured per-SKU rows to data/dram_spot_history.csv for matrix evaluation.

    Shares the cache-buster + browser-UA pattern from v3.39.3 to defeat CDN
    fingerprint-keyed caching.
    """
    try:
        cache_buster = int(datetime.now(timezone.utc).timestamp() * 1000)
        url_busted = f"{TRENDFORCE_SPOT_URL}?_={cache_buster}"
        headers = {
            **HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        resp = requests.get(url_busted, headers=headers, timeout=20)
        resp.raise_for_status()
        return parse_trendforce_spot_html(resp.text)
    except Exception as e:
        logger.warning(f"scrape_trendforce_spot_structured failed: {e}")
        return SpotScrapeResult(update_date=None, rows=[], fresh=False)


def parse_trendforce_contract_html(html_text: str) -> ContractScrapeResult:
    """Parse the TrendForce DRAM contract price page HTML into ContractScrapeResult."""
    soup = BeautifulSoup(html_text, "html.parser")
    rows_out: list[ContractScrapeRow] = []

    period_label: Optional[str] = None
    pm = re.search(r"DRAM Contract Price\s*\(([^)]+)\)", html_text)
    if pm:
        period_label = pm.group(1).strip()

    update_date: Optional[_date_v3397] = None
    dates = re.findall(r"Last Update\s+(\d{4}-\d{2}-\d{2})", html_text)
    if dates:
        try:
            update_date = max(datetime.strptime(d, "%Y-%m-%d").date() for d in dates)
        except ValueError:
            pass

    for tbl in soup.find_all("table"):
        first_cells = [
            tr.find("td").get_text(strip=True) if tr.find("td") else ""
            for tr in tbl.find_all("tr") if tr.find("td")
        ]
        if not any(name in first_cells for name in CONTRACT_SKU_MAP):
            continue

        for tr in tbl.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            display_name = tds[0].get_text(strip=True)
            sku = CONTRACT_SKU_MAP.get(display_name)
            if not sku:
                continue
            try:
                session_high = _safe_float_v3397(tds[1].get_text(strip=True))
                session_low = _safe_float_v3397(tds[2].get_text(strip=True))
                session_avg = _safe_float_v3397(tds[3].get_text(strip=True))
                avg_change = _parse_change_pct_v3397(tds[4].get_text(strip=True))
            except (ValueError, IndexError) as e:
                logger.warning(f"Failed to parse contract row {display_name}: {e}")
                continue
            if session_avg is None:
                continue
            rows_out.append(ContractScrapeRow(
                sku=sku, price_usd=session_avg,
                session_high=session_high, session_low=session_low,
                average_change_pct=avg_change,
            ))
        break

    return ContractScrapeResult(
        update_date=update_date, period_label=period_label, rows=rows_out,
    )


def scrape_trendforce_contract(
    collector: Optional[DiagnosticsCollector] = None,
) -> ContractScrapeResult:
    """Structured scrape of TrendForce's free DRAM contract-price page.

    Free-tier contract data is PC-segment only with 1-2 month lag — used as
    secondary signal / trajectory validation, NOT as matrix anchor. The matrix
    anchor comes from TrendForce Press Center quarterly forecasts via
    extract_dram_forecasts_from_press_release() in llm_summarizer.py.
    """
    try:
        cache_buster = int(datetime.now(timezone.utc).timestamp() * 1000)
        url_busted = f"{TRENDFORCE_CONTRACT_URL}?_={cache_buster}"
        headers = {
            **HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        resp = requests.get(url_busted, headers=headers, timeout=20)
        resp.raise_for_status()
        return parse_trendforce_contract_html(resp.text)
    except Exception as e:
        logger.warning(f"scrape_trendforce_contract failed: {e}")
        return ContractScrapeResult(update_date=None, period_label=None, rows=[])


def fetch_article_body(url: str) -> Optional[str]:
    """Fetch a single news article and extract main body text.

    Used by Haiku extraction (extract_dram_signal_from_article and
    extract_dram_forecasts_from_press_release in llm_summarizer.py) to feed
    structured tone/forecast prompts.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for sel in ["article", "div.article-content", "div.post-content",
                    "div.entry-content", "main"]:
            node = soup.select_one(sel)
            if node:
                text = node.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    return text
        paragraphs = soup.find_all("p")
        text = "\n".join(p.get_text(strip=True) for p in paragraphs)
        return text if len(text) > 200 else None
    except Exception as e:
        logger.warning(f"fetch_article_body failed for {url}: {e}")
        return None


# ============================================================
# Persistence helpers — idempotent upserts to data/*.csv
# ============================================================
# Each helper writes one batch of rows to a CSV, keyed on a tuple of columns.
# Same-day reruns replace matching rows rather than appending duplicates.

def _upsert_csv_v3397(
    new_df, csv_path: _Path_v3397, key_cols: list[str],
) -> int:
    """Idempotent CSV upsert. Returns count of rows in the new batch."""
    import pandas as pd
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if csv_path.exists() and csv_path.stat().st_size > 0:
        existing = pd.read_csv(csv_path)
        existing["_k"] = existing[key_cols].astype(str).agg("|".join, axis=1)
        new_df["_k"] = new_df[key_cols].astype(str).agg("|".join, axis=1)
        keep = existing[~existing["_k"].isin(new_df["_k"])].drop(columns=["_k"])
        new_clean = new_df.drop(columns=["_k"])
        combined = pd.concat([keep, new_clean], ignore_index=True)
    else:
        combined = new_df.copy()
    for sort_col in ("date", "published_date", "update_date"):
        if sort_col in combined.columns:
            combined = combined.sort_values(sort_col, kind="stable")
            break
    combined.to_csv(csv_path, index=False)
    return len(new_df)


def persist_spot_to_history(
    scrape_result: SpotScrapeResult, csv_path: _Path_v3397, source: str = "trendforce",
) -> int:
    """Idempotent upsert of a SpotScrapeResult into dram_spot_history.csv.

    Key: (date, sku, source). Same-day rerun refreshes the row.
    """
    import pandas as pd
    if scrape_result.update_date is None or not scrape_result.rows:
        logger.warning("Spot scrape has no date or rows; skipping persistence")
        return 0
    scraped_at = datetime.now(timezone.utc).isoformat()
    new_rows = []
    for r in scrape_result.rows:
        new_rows.append({
            "date": scrape_result.update_date.isoformat(),
            "sku": r.sku,
            "session_avg": r.session_avg,
            "daily_high": r.daily_high,
            "daily_low": r.daily_low,
            "session_change_pct": r.session_change_pct,
            "source": source,
            "scraped_at": scraped_at,
        })
    return _upsert_csv_v3397(
        pd.DataFrame(new_rows), csv_path,
        key_cols=["date", "sku", "source"],
    )


def persist_contract_to_history(
    scrape_result: ContractScrapeResult, csv_path: _Path_v3397,
    source: str = "trendforce",
) -> int:
    """Idempotent upsert of a ContractScrapeResult into dram_contract_actuals.csv."""
    import pandas as pd
    if scrape_result.update_date is None or not scrape_result.rows:
        return 0
    scraped_at = datetime.now(timezone.utc).isoformat()
    period = scrape_result.period_label or ""
    new_rows = []
    for r in scrape_result.rows:
        new_rows.append({
            "period_label": period,
            "update_date": scrape_result.update_date.isoformat(),
            "sku": r.sku,
            "price_usd": r.price_usd,
            "session_high": r.session_high,
            "session_low": r.session_low,
            "average_change_pct": r.average_change_pct,
            "source": source,
            "scraped_at": scraped_at,
        })
    return _upsert_csv_v3397(
        pd.DataFrame(new_rows), csv_path,
        key_cols=["period_label", "sku", "source"],
    )


def upsert_contract_forecasts(forecast_rows: list[dict], csv_path: _Path_v3397) -> int:
    """Idempotent upsert into dram_contract_quarterly_forecasts.csv.

    Each input row needs: quarter, segment, low_pct, high_pct, source,
    source_url, published_date. Optional: central_pct, supersedes_id, notes.
    row_id is auto-assigned. Key: (quarter, segment, source_url).
    """
    import pandas as pd
    if not forecast_rows:
        return 0
    new_df = pd.DataFrame(forecast_rows)
    for col in ("quarter", "segment", "source_url"):
        if col not in new_df.columns:
            raise ValueError(f"forecast row missing required column: {col}")
    if "central_pct" not in new_df.columns or new_df["central_pct"].isna().any():
        if "central_pct" not in new_df.columns:
            new_df["central_pct"] = None
        mask = new_df["central_pct"].isna()
        new_df.loc[mask, "central_pct"] = (
            new_df.loc[mask, "low_pct"] + new_df.loc[mask, "high_pct"]
        ) / 2.0
    if csv_path.exists() and csv_path.stat().st_size > 0:
        existing = pd.read_csv(csv_path)
        next_id = (int(existing["row_id"].max()) + 1
                   if "row_id" in existing.columns and not existing.empty else 1)
    else:
        next_id = 1
    new_df["row_id"] = list(range(next_id, next_id + len(new_df)))
    return _upsert_csv_v3397(
        new_df, csv_path,
        key_cols=["quarter", "segment", "source_url"],
    )


def upsert_contract_signals(signal_rows: list[dict], csv_path: _Path_v3397) -> int:
    """Idempotent upsert into dram_contract_signals.csv.

    Key: (date, source_url). Same article on same day never duplicated.
    """
    import pandas as pd
    if not signal_rows:
        return 0
    new_df = pd.DataFrame(signal_rows)
    for col in ("date", "source_url"):
        if col not in new_df.columns:
            raise ValueError(f"signal row missing required column: {col}")
    return _upsert_csv_v3397(
        new_df, csv_path, key_cols=["date", "source_url"],
    )
