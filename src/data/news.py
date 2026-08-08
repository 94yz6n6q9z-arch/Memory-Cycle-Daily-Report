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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

# v3.41.4 — guarded import: dateutil is the lenient final fallback for feed dates
# (see _parse_date_string). Guarded so the module still loads if the dependency
# is somehow absent — in that case we simply fall back to RFC822 + ISO8601 only.
try:
    from dateutil import parser as _dateutil_parser
except ImportError:  # pragma: no cover
    _dateutil_parser = None

from src.diagnostics import DiagnosticsCollector

logger = logging.getLogger(__name__)

# v3.4.7 — curl-cffi for Cloudflare TLS-fingerprint bypass.
# yfinance 1.x pulled curl_cffi in as a transitive dep so it's already
# available in production (confirmed via the v3.4.6.2 fast-tier install
# log: `curl_cffi-0.15.0`). We use it for RSS fetches + lab blog scrapes
# because Python `requests` has a JA3 fingerprint that Cloudflare's
# advanced bot rules recognize regardless of User-Agent. curl-cffi
# impersonates Chrome's actual TLS handshake.
#
# Wrapped in try/except so the codebase still imports if curl_cffi
# isn't available locally (e.g. tests in a minimal venv).
try:
    from curl_cffi import requests as _cffi_requests  # type: ignore
    from curl_cffi.requests import exceptions as _cffi_exceptions  # type: ignore
    _HAS_CURL_CFFI = True
    # Exception-type unions so callers can catch BOTH stdlib requests
    # exceptions and curl_cffi ones with a single `except` clause.
    # curl_cffi.requests.exceptions mirrors requests' hierarchy
    # (HTTPError, RequestException, ConnectionError, etc.) but the
    # classes themselves are separate identities.
    _HTTPError_Types = (requests.HTTPError, _cffi_exceptions.HTTPError)
    _RequestException_Types = (requests.RequestException, _cffi_exceptions.RequestException)
except ImportError:
    _cffi_requests = None
    _cffi_exceptions = None
    _HAS_CURL_CFFI = False
    _HTTPError_Types = (requests.HTTPError,)
    _RequestException_Types = (requests.RequestException,)


# v3.4.7.1 — startup logging + module-level CLIENT_NAME for diagnostics.
# Surfaces in the fast_tier runner log so we can VERIFY curl_cffi is
# actually being used in production rather than silently fallen back to
# stdlib requests. Without this signal, a binary-incompat or import
# error on the GHA runner is invisible until someone goes digging — and
# the v3.4.8 restore decision (Damnang Substack + Investing.com x4)
# requires knowing whether the TLS bypass actually works.
def _log_http_client_status() -> None:
    if _HAS_CURL_CFFI:
        try:
            import curl_cffi
            ver = getattr(curl_cffi, "__version__", "unknown")
        except Exception:
            ver = "unknown"
        logger.info(
            f"HTTP client: curl_cffi {ver} (Chrome120 TLS impersonation enabled "
            f"— Cloudflare JA3/JA4 bypass active)"
        )
    else:
        logger.warning(
            "HTTP client: curl_cffi UNAVAILABLE — falling back to stdlib requests. "
            "Cloudflare TLS-fingerprint-blocked sources will FAIL with the "
            "v3.4.6.1 hint. Install: pip install curl-cffi>=0.7.0"
        )


# Per-feed client tag: appended to SourceFetchStatus.detail so the email's
# 'Detail / error' column shows `client=curl_cffi` per row. Makes it
# possible to read off the email which client served each feed and
# verify TLS impersonation took effect.
CLIENT_NAME = "curl_cffi" if _HAS_CURL_CFFI else "stdlib"

# Log once at import time.
_log_http_client_status()


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


def smart_get(url: str, headers: Optional[dict] = None, timeout: int = 20):
    """v3.4.7 — HTTP GET that uses curl_cffi (Chrome TLS impersonation) when
    available, falling back to stdlib `requests` otherwise.

    Returns a response object exposing the same attributes the existing
    code reads: `.status_code`, `.headers`, `.text`, `.content`,
    `.raise_for_status()`. Both libraries' Response objects match.

    Why this matters: pre-v3.4.7, every HTTP request used Python's
    `requests` library, whose TLS fingerprint (JA3/JA4) Cloudflare's
    advanced bot rules recognize and challenge even with a perfect
    User-Agent string. Sources dropped in v3.38.4 for this reason
    (Damnang Substack, Investing.com x4) become reachable once
    curl_cffi mimics Chrome's actual handshake. The v3.4.6.1 classifier
    will still surface CF-block hints if the underlying impersonation
    is somehow detected, so we keep the diagnostic safety net.
    """
    if _HAS_CURL_CFFI:
        # Chrome 120 impersonation is the latest stable preset in 0.15.0
        return _cffi_requests.get(
            url,
            headers=headers or HEADERS,
            timeout=timeout,
            impersonate="chrome120",
        )
    return requests.get(url, headers=headers or HEADERS, timeout=timeout)

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


# v3.46.1 — Cloudflare-fronted feeds (Benzinga) block python-requests by TLS
# fingerprint regardless of UA (JA3/JA4). curl-cffi impersonates a real
# Chrome handshake. Optional dependency: absent -> behavior unchanged.
_CLOUDFLARE_HOSTS = ("benzinga.com",)


def _fetch_bytes_cf_aware(feed_url: str, timeout: int = 20):
    """requests.get for normal hosts; curl-cffi Chrome impersonation for
    known Cloudflare hosts when available. Returns (content, transport)."""
    if any(h in feed_url for h in _CLOUDFLARE_HOSTS):
        try:
            from curl_cffi import requests as _cfr
            r = _cfr.get(feed_url, impersonate="chrome", timeout=timeout)
            r.raise_for_status()
            return r.content, "curl-cffi"
        except ImportError:
            logger.info(f"curl-cffi unavailable — falling back to requests for {feed_url}")
        except Exception as e:
            logger.warning(f"curl-cffi fetch failed for {feed_url} ({e}) — falling back to requests")
    r = requests.get(feed_url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.content, "requests"


def fetch_rss_feeds(
    feeds: dict[str, str],
    hours_back: int = RECENT_HOURS_DEFAULT,
    collector: Optional[DiagnosticsCollector] = None,
) -> list[NewsItem]:
    """Fetch and parse all configured RSS feeds.

    Records one SourceFetchStatus per feed. Recent-only filtering applied.
    v3.4.6: arxiv feeds get the frontier-paper pre-filter applied at fetch
    time (~95%+ drop rate) to keep arxiv's high volume from drowning the
    relevance scorer.
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
        # v3.4.6 — arxiv pre-filter
        if source_name.lower().startswith("arxiv"):
            kept = arxiv_frontier_paper_filter(per_feed_items)
            if collector is not None and len(kept) != len(per_feed_items):
                logger.info(
                    f"arxiv pre-filter ({source_name}): kept {len(kept)}/{len(per_feed_items)} "
                    f"items matching frontier-model patterns"
                )
            per_feed_items = kept
        items.extend(per_feed_items)

    return items


# v3.4.6 — arxiv frontier-paper pre-filter
# arxiv cs.LG + cs.CL combine to ~200-300 papers/day. Most are not relevant
# to DHCR's frontier-model tracking (specialized methods papers, application
# papers, theory). We want only frontier-model RELEASE papers + major
# architecture innovation papers. Title-keyword filter is sufficient because
# arxiv titles are descriptive.

FRONTIER_LAB_NAMES = (
    "llama", "deepseek", "qwen", "mistral", "gemma", "gemini", "yi",
    "phi-", "phi3", "phi4", "falcon", "command-r", "claude", "gpt-",
    "grok", "kimi", "yi-", "olmo", "internlm", "stablelm", "exaone",
    "minimax", "doubao", "ernie", "hunyuan",
)
FRONTIER_ARCH_KEYWORDS = (
    "mixture of experts", " moe ", "(moe)", "mla ", " mla,", "(mla)",
    "fp8", "fp4", "int4", "int2", "ternary", "1.58-bit", "kv cache",
    "kv-cache", "kv compression", "speculative decoding", "long context",
    "linear attention", "mamba", "rwkv",
)
FRONTIER_RELEASE_KEYWORDS = (
    "technical report", "open weights", "open-source", "open source model",
    "frontier model", "state-of-the-art", "scaling law",
)
PARAM_COUNT_PATTERNS = (
    "7b", "8b", "13b", "14b", "32b", "34b", "70b", "72b", "405b", "8x7b",
    "8x22b", "120b", "175b", "236b", "405b", "671b", "1t ", " 1t ",
    " 1.5b", " 3b ", " 4b ",
)


def arxiv_frontier_paper_filter(items: list) -> list:
    """Return only items whose title matches frontier-model release patterns.

    Conservative: false negatives (missed frontier paper) are recoverable when
    the same release gets tech-press coverage; false positives (random arxiv
    paper) just waste a single Layer 1 call (cheap). Tuned for ~5% retention
    in cs.LG, ~10% in cs.CL.
    """
    kept = []
    for item in items:
        title = (getattr(item, "title", "") or "").lower()
        if not title:
            continue
        # Lab/model-family name in title (strongest signal — a known frontier
        # lab releasing something)
        if any(lab in title for lab in FRONTIER_LAB_NAMES):
            kept.append(item)
            continue
        # Architecture-family keyword + parameter-count → likely a release
        has_arch = any(k in title for k in FRONTIER_ARCH_KEYWORDS)
        has_params = any(p in title for p in PARAM_COUNT_PATTERNS)
        if has_arch and has_params:
            kept.append(item)
            continue
        # Release-language keyword
        if any(k in title for k in FRONTIER_RELEASE_KEYWORDS):
            kept.append(item)
            continue
    return kept


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
                    content, _transport = _fetch_bytes_cf_aware(feed_url)
                    parsed = feedparser.parse(content)
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
            # v3.46.1 — Google News intermittently 503s under burst load
            # (78/queries failed on the Aug-8 force-run, a second same-day
            # run from GHA IPs). 503 is throttling, not breakage: retry
            # twice with backoff+jitter before declaring the query failed.
            last_err = None
            for _attempt in range(3):
                try:
                    resp = requests.get(url, headers=HEADERS, timeout=20)
                    resp.raise_for_status()
                    break
                except requests.RequestException as e:
                    last_err = e
                    code = getattr(getattr(e, "response", None), "status_code", None)
                    if code == 503 and _attempt < 2:
                        import random as _rnd
                        _t = 4 * (_attempt + 1) + _rnd.uniform(0, 3)
                        logger.info(f"Google News 503 for '{query}' — retry {_attempt+1}/2 in {_t:.0f}s")
                        time.sleep(_t)
                        continue
                    raise RuntimeError(f"HTTP fetch failed: {e}")
            else:
                raise RuntimeError(f"HTTP fetch failed: {last_err}")
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



def scrape_trendforce_presscenter(
    hours_back: int = RECENT_HOURS_DEFAULT,
    collector: Optional[DiagnosticsCollector] = None,
) -> list[NewsItem]:
    """v3.46.1 — TrendForce Press Center listing as a discovery source.

    The quarterly price-forecast press releases (with the QoQ ranges the
    contract anchor needs) live under /presscenter/news/... and stopped
    reliably appearing on the /news/category listing — the Jul-3 3Q26
    forecast release was never discovered, and the anchor went 130 days
    stale while the data sat public. Same wrapper/status pattern as
    scrape_trendforce; items carry published=None (listing shows no dates)
    and flow through the same relevance + Haiku extraction gates.
    """
    def _do() -> list[NewsItem]:
        resp = requests.get(
            "https://www.trendforce.com/presscenter/news",
            headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        out: list[NewsItem] = []
        seen: set = set()
        for link in soup.select("a[href*='/presscenter/news/']")[:60]:
            href = link.get("href", "")
            title = link.get_text(strip=True)
            if not title or len(title) < 25 or not href:
                continue
            full_url = href if href.startswith("http") else f"https://www.trendforce.com{href}"
            if full_url.rstrip("/").endswith("/presscenter/news") or full_url in seen:
                continue
            seen.add(full_url)
            out.append(NewsItem(title=title, url=full_url,
                                source="TrendForce Press Center",
                                published=None, summary=None))
        return out[:20]

    items: list[NewsItem] = []
    try:
        if collector is not None:
            with collector.track("TrendForce Press Center", "scraper") as t:
                items = _do()
                t.items_returned = len(items)
                if len(items) == 0:
                    t.detail = "0 release links — listing markup may have changed"
        else:
            items = _do()
        logger.info(f"TrendForce Press Center: {len(items)} release link(s)")
    except Exception as e:
        logger.warning(f"TrendForce Press Center fetch failed: {e}")
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
    all_items.extend(scrape_trendforce_presscenter(hours_back, collector=collector))
    # v3.39.3 — TrendForce DRAM Spot price table. Emits one high-priority
    # NewsItem with the parsed table + freshness flag; flows through the
    # relevance filter (auto-preserved at score 10 by score_relevance) and
    # into Layer 2 + Layer 3 like any other news item.
    all_items.extend(scrape_trendforce_prices(collector=collector))

    # v3.4.7 — Lab blog HTML scrapers (Anthropic / Meta AI / DeepMind).
    # Replaces the 6 RSS feeds dropped in v3.4.6.3 with direct HTML
    # parsing. Items emitted with source='anthropic.com' / 'ai.meta.com'
    # / 'deepmind.google' so _classify_source_for_tier routes them to
    # Tier A in the arch aggregator. Lazy import to avoid circular
    # dependency (lab_blog_scrapers imports from this module).
    try:
        from src.data.lab_blog_scrapers import fetch_lab_blog_scrapes
        all_items.extend(fetch_lab_blog_scrapes(
            hours_back=hours_back, collector=collector,
        ))
    except Exception as e:
        logger.warning(f"Lab blog scrapes orchestrator failed: {e}")

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


# v3.4.6.1 — Cloudflare blocking detection helpers.
# Used inside _safe_parse_rss_feed's HTTPError handler to append an
# actionable hint to the diagnostics output. Without this, a 403 from
# Cloudflare (TLS-fingerprint block) is indistinguishable in the email
# from a legit 403 (dead URL) — operator has to manually probe each
# failed feed to know whether curl-cffi would unblock it.

_CLOUDFLARE_BODY_MARKERS = (
    "just a moment",          # Cloudflare interstitial title
    "checking your browser",  # legacy IUAM page
    "challenge-platform",
    "cf-chl-",                # challenge-token URL fragment
    "/cdn-cgi/",              # Cloudflare-served challenge assets
    "ray id:",                # cf-ray exposed inline
    "enable javascript and cookies to continue",
    "performance & security by cloudflare",
)


def _has_cloudflare_headers(headers) -> bool:
    """True if response headers carry Cloudflare's characteristic markers."""
    if not headers:
        return False
    lowered = {k.lower(): str(v).lower() for k, v in headers.items()}
    if "cf-ray" in lowered:
        return True
    if "cf-mitigated" in lowered:
        return True
    if lowered.get("server", "") == "cloudflare":
        return True
    return False


def _is_cloudflare_challenge_body(text: Optional[str]) -> bool:
    """True if body looks like a Cloudflare challenge / IUAM interstitial."""
    if not text:
        return False
    snippet = text[:5000].lower()
    # Require at least two markers to avoid false positives on legit pages
    # that mention Cloudflare in passing.
    hits = sum(1 for m in _CLOUDFLARE_BODY_MARKERS if m in snippet)
    return hits >= 2


def _classify_blocking_response(resp) -> str:
    """Return an actionable hint string if `resp` shows Cloudflare blocking,
    empty string otherwise. Tagged onto the RuntimeError message so it
    surfaces in the email's data-source validation table 'Detail / error'
    column without any plumbing changes.
    """
    if resp is None:
        return ""
    status = getattr(resp, "status_code", None)
    headers = getattr(resp, "headers", {})
    body = getattr(resp, "text", "")
    cf_headers = _has_cloudflare_headers(headers)
    cf_body = _is_cloudflare_challenge_body(body)
    if status in (403, 429, 503) and (cf_headers or cf_body):
        return "[Cloudflare TLS-fingerprint block — needs curl-cffi (v3.4.7 candidate)]"
    if status == 403 and cf_headers:
        return "[Cloudflare-fronted 403 — possible TLS-fingerprint block]"
    return ""


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
            # v3.4.7 — smart_get uses curl_cffi (Chrome TLS impersonation)
            # when available; stdlib requests otherwise. Bypasses Cloudflare
            # JA3 fingerprinting for Damnang / Investing.com / SemiAnalysis
            # without breaking sites that work fine over stdlib.
            resp = smart_get(feed_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except _HTTPError_Types as e:
            # v3.4.6.1 — Cloudflare bot-detection / TLS-fingerprint block hint.
            # 403/429/503 with Cloudflare headers or challenge-page body is the
            # signature of TLS-fingerprint blocking — generic requests.HTTPError
            # alone doesn't tell you whether to retry a URL or whether the
            # source genuinely doesn't exist. The hint makes the diagnostic
            # actionable in the email's data-source validation table.
            # v3.4.7 — _HTTPError_Types is a tuple of both stdlib requests
            # HTTPError and curl_cffi HTTPError, so this handler works for
            # either underlying HTTP client.
            hint = _classify_blocking_response(getattr(e, "response", None)) if getattr(e, "response", None) is not None else ""
            suffix = f" {hint}" if hint else ""
            raise RuntimeError(f"HTTP fetch failed: {e}{suffix}")
        except _RequestException_Types as e:
            raise RuntimeError(f"HTTP fetch failed: {e}")

        # v3.4.6.1 — Cloudflare challenge-page returned with HTTP 200.
        # Some Cloudflare rules return the "Just a moment..." HTML
        # interstitial with a 200 status code (the page rewrites in JS once
        # the browser passes the challenge). feedparser would otherwise
        # raise a generic "not an XML media type" bozo error; we recognize
        # it as Cloudflare and tag the failure.
        if _is_cloudflare_challenge_body(resp.text):
            raise RuntimeError(
                "HTTP 200 but Cloudflare challenge page returned "
                "[Cloudflare TLS-fingerprint block — needs curl-cffi (v3.4.7 candidate)]"
            )

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
                # v3.4.7.1 — Per-feed client tag. Appended to detail so the
                # email's Detail/error column shows `client=curl_cffi` (or
                # `client=stdlib`) per RSS row. Makes the v3.4.8 restore
                # decision evidence-based: if Damnang + Investing.com x4
                # come back FAIL with client=curl_cffi, Cloudflare has
                # tightened beyond TLS fingerprint; drop again. If they
                # come back OK with client=curl_cffi, restore confirmed.
                t.detail = f"client={CLIENT_NAME}"
        else:
            out = _parse()
    except Exception as e:
        logger.warning(f"Failed to parse {source_name} RSS: {e}")

    return out


def _parse_date_string(raw: str) -> Optional[datetime]:
    """Parse an RSS/Atom date string to a UTC datetime.

    Tries, in order: RFC822/RFC2822 (the standard RSS <pubDate>, via email.utils),
    ISO8601/Atom (datetime.fromisoformat), then python-dateutil's lenient parser
    as a final fallback for exotic formats that the stricter stdlib parsers (and
    feedparser itself) reject. Naive results are assumed UTC. Returns None only
    when every parser fails."""
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    # RFC822 / RFC2822 — e.g. "Tue, 20 May 2026 09:30:00 +0900"
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    # ISO8601 / Atom — e.g. "2026-05-20T09:30:00+09:00" or trailing "Z" or date-only
    iso = (raw[:-1] + "+00:00") if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    # python-dateutil — lenient catch-all for whatever the feed actually emits.
    # fuzzy=False so it won't extract a date out of arbitrary prose (must be a
    # date string). default fills only missing sub-fields (irrelevant for a
    # full pubDate).
    if _dateutil_parser is not None:
        try:
            dt = _dateutil_parser.parse(raw)
            if dt is not None:
                return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError, TypeError):
            pass
    return None


def _entry_published(entry) -> Optional[datetime]:
    """Extract a UTC datetime from a feedparser entry; return None if unavailable.

    v3.41.3: fall back to parsing the RAW date string when feedparser did not
    pre-parse it into *_parsed. feedparser only populates published_parsed /
    updated_parsed for date formats it recognizes; Korea Herald (which carries
    the Samsung labor / strike news) and some Google News locales emit formats
    it skips, leaving the item UNDATED. Undated items sort to datetime.min in
    fetch_all_news → bottom of the list → starved by the recency sort + the
    120-item triage cap. That is why the May 20 Samsung-strike-RESOLUTION story
    (a major SK-Hynix-positive catalyst) never reached Sonnet even after its
    queries were added in v3.39.7.5: the items were fetched but undated, so the
    resolving item never sorted above the older 'strike nears' items. With a
    real date, the newest (resolving) item sorts to the top and is prioritized
    into triage."""
    # 1. feedparser's pre-parsed struct_time (fast path, unchanged)
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                return datetime(*struct[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    # 2. raw date-string fallback (RFC822 → ISO8601 → dateutil, all in _parse_date_string)
    unparsed = None
    for key in ("published", "updated", "created", "pubDate", "date", "dc_date"):
        raw = entry.get(key)
        dt = _parse_date_string(raw)
        if dt is not None:
            return dt
        if unparsed is None and isinstance(raw, str) and raw.strip():
            unparsed = (key, raw.strip())
    # 3. v3.41.4 observability: a date string WAS present but nothing could parse
    #    it (even dateutil). Surface the exact format so a parser can be added —
    #    this is the signal that confirms whether a given source is still landing
    #    UNDATED. Genuinely dateless entries (no date field at all) stay silent.
    if unparsed is not None:
        logger.warning(
            f"_entry_published: UNPARSEABLE date {unparsed[0]}={unparsed[1]!r} "
            f"(title: {str(entry.get('title') or '?')[:80]}) — item lands undated; "
            f"add a date format if this recurs"
        )
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


# ============================================================
# v3.4.13 — LPDDR contract direction scraper
# ============================================================
#
# TrendForce publishes LPDDR contract DIRECTION + magnitude in their free
# press releases (numerical $/GB ASP levels are in paid RP* reports, but
# QoQ % moves and direction commentary are free). Example past releases:
#   - 1Q26: "LPDDR4X and LPDDR5X expected to grow ~90% QoQ"
#   - 2Q26: "Narrowed price hike forecasts for LPDDR"
#
# This module fetches the same news category page that scrape_trendforce()
# uses, then for each recent article fetches the body and regex-extracts
# LPDDR commentary. Latest extracted direction wins (most recent release).
#
# Critical observability design: the scrape returns ONE of four statuses
# so "no relevant data" is distinguishable from "the scraper broke":
#
#   "data_extracted"                  → normal success path
#   "no_lpddr_content_in_releases"    → genuinely no LPDDR commentary today
#   "parser_no_match"                 → THE BUG MODE — keyword found but
#                                       regex didn't match. Sample text
#                                       captured for fast fix.
#   "fetch_failed"                    → HTTP/Cloudflare error
#
# A hard sentinel tracks consecutive "parser_no_match" runs in
# state/lpddr_scraper_consecutive_parser_no_match.json. After 3 in a row,
# the diagnostic row escalates from ⚠️ to ❌ so a regex bug doesn't sit
# at "warning" indefinitely.


@dataclass
class LpddrSignal:
    """A single LPDDR direction extraction from one press release."""
    release_date: Optional[str]       # ISO date if extractable from URL/page
    direction: str                    # "rising" | "flat" | "falling"
    pct_qoq: Optional[float]          # signed % if specified, else None
    quote: str                        # verbatim snippet (<= 30 words)
    source_url: str
    source_title: str


@dataclass
class LpddrScrapeResult:
    """Outcome of one LPDDR scrape with explicit observability status.

    Caller distinguishes "no data" from "broken scraper" via .status.
    """
    status: str  # see module docstring for the four allowed values
    pages_checked: int
    pages_with_lpddr_keywords: int
    rows_extracted: int
    signals: list[LpddrSignal]
    sample_text_when_unparsed: str    # first 300 chars of LPDDR-mentioning
                                       # text the parser couldn't match — for
                                       # fast debug after a structure change
    error: Optional[str] = None

    @property
    def latest_signal(self) -> Optional[LpddrSignal]:
        """Most recent LpddrSignal by release_date (None if no signals)."""
        if not self.signals:
            return None
        with_date = [s for s in self.signals if s.release_date]
        if with_date:
            return sorted(with_date, key=lambda s: s.release_date, reverse=True)[0]
        return self.signals[0]


# Regex patterns for direction extraction. Order matters: try most-specific
# patterns first (with explicit numeric magnitude), then directional
# keywords. The patterns are designed to be tolerant of TrendForce's prose
# style, where LPDDR mentions are interspersed with DDR4/DDR5 in the same
# paragraph.
LPDDR_KEYWORD_RE_v3413 = re.compile(r"\bLPDDR\d?[A-Z]*\b", re.IGNORECASE)

# Rising patterns — "grow X%", "rise X%", "surge X%", "expected to grow",
# "QoQ increase", "price hike of X%"
LPDDR_RISING_PATTERNS_v3413 = [
    re.compile(
        r"LPDDR[\dA-Z]*\s+(?:[\w,]+\s+){0,8}?(?:expected to\s+)?(?:grow|grew|rise|rose|surge|surged|increase|increased|climb|climbed|jump|jumped|soar|soared|double|doubled|triple|tripled|hike|hiked|up)\s+(?:by\s+)?(?:approximately\s+|around\s+|~\s*)?(\d+(?:\.\d+)?)(?:\s*(?:[-\u2013]|to)\s*\d+(?:\.\d+)?)?\s*%",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:price\s+hike|price\s+increase|QoQ\s+(?:growth|rise|increase))\s+(?:of\s+|by\s+|approximately\s+|~\s*)?(\d+(?:\.\d+)?)(?:\s*(?:[-\u2013]|to)\s*\d+(?:\.\d+)?)?\s*%[^.]{0,50}?LPDDR",
        re.IGNORECASE,
    ),
    # Directional only ("expected to grow") with no magnitude — assign None
    re.compile(
        r"LPDDR[\dA-Z]*\s+(?:[\w,]+\s+){0,8}?(?:expected to\s+)?(?:grow|grew|rise|rose|surge|surged|increase|increased|climb|climbed|jump|jumped|soar|soared|double|doubled|triple|tripled)",
        re.IGNORECASE,
    ),
]

# Falling patterns — "decline X%", "drop X%", "fall X%", "correction",
# "roll over", "weakness"
LPDDR_FALLING_PATTERNS_v3413 = [
    re.compile(
        r"LPDDR[\dA-Z]*\s+(?:[\w,]+\s+){0,8}?(?:expected to\s+)?(?:decline|declined|fall|fell|drop|dropped|correct|corrected|decrease|decreased|down|slip|slipped|slid|tumble|tumbled)\s+(?:by\s+)?(?:approximately\s+|around\s+|~\s*)?(\d+(?:\.\d+)?)(?:\s*(?:[-\u2013]|to)\s*\d+(?:\.\d+)?)?\s*%",
        re.IGNORECASE,
    ),
    re.compile(
        r"LPDDR[\dA-Z]*\s+(?:[\w,]+\s+){0,8}?(?:roll(?:ing)?\s+over|correction|weakness|softening)",
        re.IGNORECASE,
    ),
]

# Flat / narrowing patterns
LPDDR_FLAT_PATTERNS_v3413 = [
    re.compile(
        r"LPDDR[\dA-Z]*\s+(?:[\w,]+\s+){0,8}?(?:narrow|stable|flat|unchanged|level off|level\s+out|plateau)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:narrow|narrowing|moderating|moderated)\s+(?:[\w,]+\s+){0,5}?(?:LPDDR|price hike)",
        re.IGNORECASE,
    ),
]


def _extract_lpddr_signal_from_body(
    body_text: str, source_url: str, source_title: str, release_date: Optional[str] = None,
) -> tuple[Optional[LpddrSignal], Optional[str]]:
    """Try to extract LPDDR direction from a press-release body.

    Returns (LpddrSignal, None) on success; (None, sample_text) when the
    LPDDR keyword IS present but no direction pattern matched (the bug
    mode — sample_text shows what we missed); (None, None) when LPDDR
    isn't mentioned in the body at all.
    """
    if not body_text:
        return None, None

    # Quick keyword check first — avoid regex passes on irrelevant articles
    if not LPDDR_KEYWORD_RE_v3413.search(body_text):
        return None, None

    # Evaluation order matters: try MOST SPECIFIC first, FALL THROUGH to
    # the most generic. "Narrowing growth" should resolve to FLAT, not
    # RISING. So order is: flat → falling → rising.
    #
    # Flat patterns (narrow/stable/moderating) are the most specific:
    # they always indicate decelerating-but-positive momentum, never
    # absolute decline or growth. Try these first.
    for pat in LPDDR_FLAT_PATTERNS_v3413:
        m = pat.search(body_text)
        if m:
            quote = _extract_quote_around_match_v3413(body_text, m)
            return LpddrSignal(
                release_date=release_date,
                direction="flat",
                pct_qoq=None,
                quote=quote,
                source_url=source_url,
                source_title=source_title,
            ), None

    # Falling patterns next (decline/drop/correction/roll-over). Specific
    # negative-direction keywords that trump generic "growth" mentions.
    for pat in LPDDR_FALLING_PATTERNS_v3413:
        m = pat.search(body_text)
        if m:
            pct = None
            try:
                pct = -abs(float(m.group(1)))
            except (IndexError, ValueError):
                pct = None
            quote = _extract_quote_around_match_v3413(body_text, m)
            return LpddrSignal(
                release_date=release_date,
                direction="falling",
                pct_qoq=pct,
                quote=quote,
                source_url=source_url,
                source_title=source_title,
            ), None

    # Rising patterns last — these have the most generic vocabulary
    # ("grow", "rise") that can appear in non-direction contexts.
    for pat in LPDDR_RISING_PATTERNS_v3413:
        m = pat.search(body_text)
        if m:
            pct = None
            try:
                pct = float(m.group(1))
            except (IndexError, ValueError):
                pct = None
            quote = _extract_quote_around_match_v3413(body_text, m)
            return LpddrSignal(
                release_date=release_date,
                direction="rising",
                pct_qoq=pct,
                quote=quote,
                source_url=source_url,
                source_title=source_title,
            ), None

    # LPDDR keyword present but no direction pattern matched — bug mode.
    # Capture sample text around the first LPDDR mention for debug.
    keyword_m = LPDDR_KEYWORD_RE_v3413.search(body_text)
    if keyword_m:
        start = max(0, keyword_m.start() - 100)
        end = min(len(body_text), keyword_m.end() + 200)
        sample = body_text[start:end].replace("\n", " ").strip()
        return None, sample[:300]

    return None, None


def _extract_quote_around_match_v3413(body_text: str, match) -> str:
    """Build a verbatim quote (<= 30 words) centred on the regex match."""
    start = max(0, match.start() - 50)
    end = min(len(body_text), match.end() + 100)
    snippet = body_text[start:end].replace("\n", " ").strip()
    words = snippet.split()
    if len(words) > 30:
        # Try to centre on the match
        # Find word index closest to match midpoint
        match_mid_char = (match.start() + match.end()) // 2
        running = 0
        match_word_idx = 0
        for i, w in enumerate(words):
            running += len(w) + 1
            if start + running >= match_mid_char:
                match_word_idx = i
                break
        left = max(0, match_word_idx - 15)
        right = min(len(words), left + 30)
        words = words[left:right]
    return " ".join(words)


def _extract_release_date_from_url_v3413(url: str) -> Optional[str]:
    """TrendForce URLs follow /news/YYYY/MM/DD/<slug>/. Extract the date.

    Falls back to None on any URL that doesn't match — caller treats None
    as "use article fetch time".
    """
    m = re.search(r"/(\d{4})/(\d{1,2})/(\d{1,2})/", url)
    if not m:
        return None
    try:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    except (ValueError, TypeError):
        return None


LPDDR_PARSER_NO_MATCH_SENTINEL_FILE = "state/lpddr_scraper_consecutive_parser_no_match.json"
# v3.41.8: raised 3 -> 75. TrendForce states LPDDR contract DIRECTIONS only in
# its ~quarterly memory-price-outlook releases (1Q in early Feb, 2Q in late
# Mar, etc.); between them, recent DRAM articles mention LPDDR with no price
# direction, which is a legitimate parser_no_match, not a bug. A quarter is
# ~63 weekday runs, so a threshold of 3 false-alarmed for ~6 weeks straight.
# 75 only trips if the gap exceeds a full quarter — i.e. a new outlook came and
# went WITHOUT being parsed, which would indicate a genuine source/phrasing
# change. The next outlook resets the sentinel when its direction is extracted.
LPDDR_PARSER_NO_MATCH_SENTINEL_THRESHOLD = 75


def _load_lpddr_sentinel_v3413() -> int:
    """Load the running count of consecutive parser_no_match runs."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(LPDDR_PARSER_NO_MATCH_SENTINEL_FILE)
    if not p.exists():
        return 0
    try:
        with open(p) as f:
            data = _json.load(f)
        return int(data.get("consecutive_parser_no_match", 0)) if isinstance(data, dict) else 0
    except (OSError, ValueError):
        return 0


def _save_lpddr_sentinel_v3413(count: int) -> None:
    """Persist the running count. Atomic via tmp + rename."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(LPDDR_PARSER_NO_MATCH_SENTINEL_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            _json.dump({"consecutive_parser_no_match": int(count)}, f)
        tmp.replace(p)
    except OSError as e:
        logger.warning(f"Could not write {p}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def scrape_trendforce_lpddr_contract(
    max_articles: int = 20,
    collector: Optional[DiagnosticsCollector] = None,
) -> LpddrScrapeResult:
    """Scrape recent TrendForce press releases for LPDDR direction commentary.

    Reuses the v3.39.3 article-list path (/news/category/semiconductors/dram/)
    and the v3.39.7 fetch_article_body() helper. Returns a single
    LpddrScrapeResult with the structured status the caller uses to
    distinguish "no LPDDR commentary this run" from "scraper broke".

    The sentinel for consecutive parser_no_match runs is updated as a side
    effect. After LPDDR_PARSER_NO_MATCH_SENTINEL_THRESHOLD consecutive
    parser_no_match runs, the diagnostic detail string escalates to "BUG
    SUSPECTED" so the run summary surfaces it as an error not a warning.
    """
    result = LpddrScrapeResult(
        status="no_lpddr_content_in_releases",
        pages_checked=0,
        pages_with_lpddr_keywords=0,
        rows_extracted=0,
        signals=[],
        sample_text_when_unparsed="",
    )

    def _do() -> LpddrScrapeResult:
        # Step 1: get the article list from the category page
        try:
            resp = requests.get(
                "https://www.trendforce.com/news/category/semiconductors/dram/",
                headers=HEADERS, timeout=15,
            )
            resp.raise_for_status()
        except Exception as e:
            result.status = "fetch_failed"
            result.error = f"category page: {e}"
            return result

        soup = BeautifulSoup(resp.text, "html.parser")
        anchors = soup.select("a[href*='/news/']")
        seen_urls: set[str] = set()
        article_urls: list[tuple[str, str]] = []  # (url, title)
        for link in anchors:
            href = link.get("href", "")
            title = link.get_text(strip=True)
            if not title or len(title) < 20 or not href:
                continue
            if href.rstrip("/").endswith("/news") or "/category/" in href:
                continue
            full_url = href if href.startswith("http") else f"https://www.trendforce.com{href}"
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            article_urls.append((full_url, title))
            if len(article_urls) >= max_articles:
                break

        result.pages_checked = len(article_urls)
        if not article_urls:
            result.status = "fetch_failed"
            result.error = "category page returned 0 article links"
            return result

        # Step 2: fetch each article body and extract LPDDR signal
        first_unparsed_sample: Optional[str] = None
        for url, title in article_urls:
            body = fetch_article_body(url)
            if not body:
                continue
            release_date = _extract_release_date_from_url_v3413(url)
            signal, unparsed_sample = _extract_lpddr_signal_from_body(
                body, url, title, release_date=release_date,
            )
            if signal:
                result.pages_with_lpddr_keywords += 1
                result.signals.append(signal)
            elif unparsed_sample:
                # Keyword present but no direction matched
                result.pages_with_lpddr_keywords += 1
                if first_unparsed_sample is None:
                    first_unparsed_sample = unparsed_sample

        result.rows_extracted = len(result.signals)

        # Determine overall status
        if result.signals:
            result.status = "data_extracted"
        elif result.pages_with_lpddr_keywords > 0:
            result.status = "parser_no_match"
            result.sample_text_when_unparsed = first_unparsed_sample or ""
        else:
            result.status = "no_lpddr_content_in_releases"

        return result

    # Wrap in collector context if provided (gives the diagnostics row)
    try:
        if collector is not None:
            with collector.track("TrendForce LPDDR contract", "scraper") as t:
                r = _do()
                t.items_returned = r.rows_extracted
                # Diagnostic detail string varies by status (informational only —
                # the run still completes; downstream consumers read .status)
                if r.status == "data_extracted":
                    latest = r.latest_signal
                    if latest:
                        t.detail = (
                            f"{r.rows_extracted} signal(s), latest: "
                            f"{latest.direction}"
                            + (f" {latest.pct_qoq:+.0f}%" if latest.pct_qoq is not None else "")
                        )
                elif r.status == "no_lpddr_content_in_releases":
                    t.detail = f"{r.pages_checked} pages checked, 0 mentioned LPDDR"
                elif r.status == "parser_no_match":
                    sentinel_count = _load_lpddr_sentinel_v3413() + 1
                    _save_lpddr_sentinel_v3413(sentinel_count)
                    if sentinel_count >= LPDDR_PARSER_NO_MATCH_SENTINEL_THRESHOLD:
                        t.detail = (
                            f"STALE: no LPDDR contract-direction extracted in "
                            f"{sentinel_count} runs (> a quarter). "
                            f"{r.pages_with_lpddr_keywords}/{r.pages_checked} recent pages "
                            f"mention LPDDR but state no price direction. TrendForce publishes "
                            f"LPDDR directions ~quarterly, so a gap this long suggests its outlook "
                            f"phrasing changed or LPDDR pricing moved behind the members-only "
                            f"contract-price page — worth a manual check. Sample: "
                            f"'{r.sample_text_when_unparsed[:120]}...'"
                        )
                        # Surface as ERROR via the collector only after a gap
                        # longer than TrendForce's ~quarterly outlook cadence —
                        # at that point a genuine source/phrasing change is the
                        # likely cause (a normal between-outlook gap resets well
                        # before this). This is NOT a per-run regex failure.
                        raise RuntimeError(
                            f"LPDDR scraper: no contract-direction extracted in "
                            f"{sentinel_count} runs (> a quarter) — verify TrendForce's "
                            f"outlook phrasing / source. Last sample: "
                            f"{r.sample_text_when_unparsed[:200]}"
                        )
                    else:
                        t.detail = (
                            f"no LPDDR direction this run "
                            f"({sentinel_count}/{LPDDR_PARSER_NO_MATCH_SENTINEL_THRESHOLD} "
                            f"since last extraction): {r.pages_with_lpddr_keywords}/"
                            f"{r.pages_checked} recent pages mention LPDDR without a price "
                            f"direction — normal between TrendForce's ~quarterly outlooks. "
                            f"Sample: '{r.sample_text_when_unparsed[:120]}...'"
                        )
                elif r.status == "fetch_failed":
                    t.detail = f"fetch_failed: {r.error or 'unknown'}"
                    raise RuntimeError(t.detail)
                # Reset sentinel on any non-no-match outcome (successful
                # extraction OR genuine "no LPDDR" both indicate the scraper
                # is working)
                if r.status in ("data_extracted", "no_lpddr_content_in_releases"):
                    if _load_lpddr_sentinel_v3413() > 0:
                        _save_lpddr_sentinel_v3413(0)
            return r
        else:
            return _do()
    except Exception as e:
        logger.warning(f"LPDDR scrape failed: {e}", exc_info=True)
        result.status = result.status if result.status != "no_lpddr_content_in_releases" else "fetch_failed"
        if result.error is None:
            result.error = str(e)[:200]
        return result


# Persistence — append-only CSV, idempotent on (release_date, source_url) key
LPDDR_CONTRACT_CSV = _Path_v3397("data/lpddr_contract_history.csv")


def persist_lpddr_contract_to_history(result: LpddrScrapeResult) -> int:
    """Persist LpddrScrapeResult signals to data/lpddr_contract_history.csv.

    Idempotent: same release_date + source_url replaces existing row rather
    than appending duplicates. Returns count of rows persisted.
    """
    if not result.signals:
        return 0
    import pandas as pd
    today_iso = _date_v3397.today().isoformat()
    rows = []
    for sig in result.signals:
        rows.append({
            "release_date": sig.release_date or today_iso,
            "extracted_at": today_iso,
            "direction": sig.direction,
            "pct_qoq": sig.pct_qoq if sig.pct_qoq is not None else "",
            "source_url": sig.source_url,
            "source_title": sig.source_title,
            "quote": sig.quote,
        })
    new_df = pd.DataFrame(rows)
    return _upsert_csv_v3397(
        new_df, LPDDR_CONTRACT_CSV, key_cols=["release_date", "source_url"],
    )
