"""
Earnings transcript sources — free transcript coverage (with paid proxy
fallback for the names that need it).

Three sources, in v3.33's NEW sequential-cascade order:

  1. Motley Fool — PRIMARY for US-listed names (~13 names: NVDA, AVGO, AMAT,
     MSFT, GOOGL, META, AMZN, ORCL, DELL, HPE, ASML, TSM, MU). Scrape-able
     directly from datacenter IPs (no proxy needed), per-ticker URL pattern
     `/quote/{exchange}/{ticker}/` which lists transcripts for that company.
     Does NOT cover the Korean listings (SK Hynix, Samsung).

  2. Direct IR-site scrapers — Micron (full transcript), SK Hynix + Samsung
     (materials_only — slides + webcast, no text transcript; `transcript_capable=False`
     in IR_SOURCES caps them).

  3. Yahoo Finance — FALLBACK ONLY, runs AFTER Motley Fool + IR, and ONLY for
     companies still missing a full transcript at that point. Carries full call
     text (incl. Korean+English Q&A) for every name in the basket, crucially
     SK Hynix + Samsung which no other free source covers. Yahoo blocks
     datacenter IPs with 404 — accessed through a Bright Data residential proxy
     when `RESIDENTIAL_PROXY_URL` env is set.

EDGAR 8-K earnings exhibits — handled in news.py / digester, not here.

WHY MOTLEY FOOL FIRST (v3.33 architectural change)
---------------------------------------------------
v3.32 had Yahoo as primary, which broke when Yahoo started returning 404 to
datacenter IPs from GitHub Actions runners. The fix isn't "always proxy
everything" — it's "only proxy what we can't get for free". Motley Fool covers
~85% of the basket free of cost and free of proxy bandwidth. Yahoo is reserved
for what Motley Fool can't reach (the two Korean memory makers) and as a
belt-and-suspenders fallback when Motley Fool publication is delayed.

Concrete cost difference at our daily volume: ~135 MB/month if Yahoo runs for
every ticker daily (v3.32 plan) vs ~10-15 MB/month if Yahoo only fires for
SK Hynix + Samsung + the occasional US-name gap (v3.33 plan). Even at our
trivial scale this principle matters because it preserves trial-tier headroom.

THE THREE-STATE AVAILABILITY FLAG
---------------------------------
The whole point of this pipeline is getting the *commentary* right. So every
transcript source returns one of three states per company per event:

  "full_transcript" — got the actual earnings-call text; commentary intact
  "materials_only"  — got the earnings release and/or slides, but NO call
                      commentary
  "nothing"         — source had nothing for this event

The digester and the email both surface this explicitly. When a source has
only a slide deck, the brief says so plainly — absence of commentary is never
silently confused with "nothing notable was said".

THE URL-SEEN CACHE (v3.33)
--------------------------
`state/cache/transcripts_seen.json` records URLs we've already digested
(by source_url). When the same transcript URL appears again the next day (it
will — Motley Fool quarter pages persist for ~3 months), the scraper skips
the body fetch and returns a stub with availability="already_processed".
The deep_tier orchestrator filters those out before invoking the Haiku
digester. Saves ~$2-3/year in Haiku tokens, eliminates repeated digest
churn in `state/digests/`, and (for Yahoo specifically) avoids re-burning
residential proxy bandwidth on transcripts already captured.

FRAGILITY NOTE
--------------
Scrapers are the most fragile part of any pipeline — when a site redesigns,
the selector breaks. Every scraper here is written DEFENSIVELY: a broken
selector degrades to "materials_only" or "nothing" and logs a warning. It
never raises into the deep-tier orchestrator. Same maintenance posture as the
Naver scraper (expect 1-2 fixes/year).

CHANGELOG (this file):
  v3.33  (2026-05-16) — Sequential cascade: Motley Fool primary → IR fill-ins → Yahoo fallback for gaps. Motley Fool scraper rewritten to use per-ticker /quote/{exchange}/{ticker}/ page (was global recent-index which rolled off in 24-48h, leaving no fallback). Yahoo routed through Bright Data residential proxy when RESIDENTIAL_PROXY_URL is set, with realistic Chrome headers, per-request session rotation, redirect-following, 3s inter-ticker delay, and one retry-on-429 with 10s backoff. transcripts_seen.json cache skips re-fetching/re-digesting URLs we've already processed.
  v3.32  (2026-05-14) — Added fetch_yahoo_finance_transcripts() as PRIMARY source; fetch_all_transcripts()/fetch_transcripts_with_retry() take yahoo_tickers; IR scraper honours transcript_capable (caps SK Hynix/Samsung at materials_only)
  v3.31  (2026-05-14) — NEW FILE — IR-site + Motley Fool scrapers; replaces gated Quartr API; three-state availability flag
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("memory_cycle_brief.transcript_sources")

REQUEST_TIMEOUT_SEC = 30

# Realistic browser User-Agent — used for ALL sources, not just Yahoo. Even
# for Motley Fool (which serves us fine from datacenter IPs), a real-browser UA
# is harmless and protects against future bot-detection tightening.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

# Browser-realistic header bundle — added on top of USER_AGENT when we use the
# residential proxy. These aren't needed for Motley Fool/IR (direct fetches),
# but Yahoo's bot detection cross-checks them.
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}

# Minimum character count for text to count as a "full" transcript rather than
# just a headline / blurb. Earnings calls run 20-60K chars; a release is ~2-8K.
FULL_TRANSCRIPT_MIN_CHARS = 9000
MATERIALS_MIN_CHARS = 400

# State cache for already-digested transcript URLs.
SEEN_CACHE_PATH = Path("state/cache/transcripts_seen.json")


# ============================================================
# Shared result shape — every transcript source returns this
# ============================================================

@dataclass
class TranscriptResult:
    """One company / one event. The `availability` flag is the contract every
    transcript source must honour."""
    company: str
    ticker: str
    event_date_iso: str
    event_type: str                       # "earnings_call" | "conference" | "cmd" | "unknown"
    availability: str                     # "full_transcript" | "materials_only" | "nothing"
    text: str = ""                        # transcript text, or release/slide text if materials_only
    char_count: int = 0
    source_name: str = ""                 # "SK Hynix IR" | "Motley Fool" | etc.
    source_url: Optional[str] = None
    note: Optional[str] = None            # human-readable explanation, esp. for materials_only / nothing

    def __post_init__(self):
        self.char_count = len(self.text or "")

    def to_dict(self) -> dict:
        return asdict(self)


def _classify_availability(text: str) -> str:
    """Map a blob of scraped text to the three-state flag by length heuristic."""
    n = len(text or "")
    if n >= FULL_TRANSCRIPT_MIN_CHARS:
        return "full_transcript"
    if n >= MATERIALS_MIN_CHARS:
        return "materials_only"
    return "nothing"


def _http_get(url: str, use_proxy: bool = False, session_id: Optional[str] = None,
              follow_redirects: bool = True) -> Optional[str]:
    """Defensive GET — returns page text or None. Never raises.

    Parameters:
        use_proxy: When True, route through RESIDENTIAL_PROXY_URL (if set in env)
                   and apply realistic browser headers. Used for Yahoo Finance
                   which blocks datacenter IPs.
        session_id: When provided AND use_proxy is True, append `-session-{id}`
                   to the proxy username to force a fresh residential IP. Used
                   for per-ticker rotation to avoid Yahoo's per-IP rate limit.
        follow_redirects: True by default; Yahoo serves 301 redirects to
                   canonicalize URLs (trailing slash etc.) and we need to follow.
    """
    headers = {"User-Agent": USER_AGENT}
    proxies = None

    if use_proxy:
        base_proxy = os.environ.get("RESIDENTIAL_PROXY_URL")
        if base_proxy:
            # Inject session_id into the proxy URL's userinfo so Bright Data
            # gives us a fresh residential IP per request. Format expected:
            # http://USER:PASS@host:port  ->  http://USER-session-{id}:PASS@host:port
            # Bright Data treats `-session-{anything}` in the username as the
            # sticky-session token; using a random per-request token gives us
            # rotation.
            if session_id:
                # Find the ':' before the password to insert before
                try:
                    scheme_split = base_proxy.split("://", 1)
                    if len(scheme_split) == 2:
                        scheme, rest = scheme_split
                        user_at_host = rest.split("@", 1)
                        if len(user_at_host) == 2:
                            userpass, host_part = user_at_host
                            user, _, password = userpass.partition(":")
                            user = f"{user}-session-{session_id}"
                            base_proxy = f"{scheme}://{user}:{password}@{host_part}"
                except Exception as e:
                    logger.warning(f"Could not inject session into proxy URL ({e}); using as-is")
            proxies = {"http": base_proxy, "https": base_proxy}
            # Browser-realistic headers — only when going through the proxy.
            # Direct fetches to Motley Fool / IR don't need these.
            headers.update(BROWSER_HEADERS)
        else:
            logger.warning(f"use_proxy=True but RESIDENTIAL_PROXY_URL not set; falling back to direct fetch for {url}")

    try:
        r = requests.get(
            url, timeout=REQUEST_TIMEOUT_SEC,
            headers=headers, proxies=proxies,
            allow_redirects=follow_redirects,
            verify=(not use_proxy),   # Bright Data's residential proxy intercepts HTTPS with its own cert; cert verification fails without their CA installed. Disabling for proxy mode is the documented pattern (same as curl's -k flag).
        )
        # Single retry-on-429 for proxy requests (Yahoo's per-IP rate limit)
        if use_proxy and r.status_code == 429:
            logger.info(f"Got 429 on {url}; sleeping 10s and retrying with fresh session")
            time.sleep(10)
            # Force a NEW session token for the retry to get a different IP
            new_session = str(random.randint(100000, 999999))
            return _http_get(url, use_proxy=True, session_id=new_session, follow_redirects=follow_redirects)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"HTTP GET failed for {url}: {e}")
        return None


# ============================================================
# URL-seen cache — avoid re-digesting transcripts we've already processed
# ============================================================

def _load_seen_urls() -> set[str]:
    """Load the set of transcript URLs already digested. Returns empty set on
    any failure (cache is best-effort, never blocking)."""
    try:
        if SEEN_CACHE_PATH.exists():
            data = json.loads(SEEN_CACHE_PATH.read_text())
            return set(data.get("urls", []))
    except Exception as e:
        logger.warning(f"Failed to load transcripts_seen cache: {e}")
    return set()


def _save_seen_urls(urls: set[str]) -> None:
    """Persist the seen-URL set. Best-effort; failures are logged not raised."""
    try:
        SEEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SEEN_CACHE_PATH.write_text(json.dumps({
            "urls": sorted(urls),
            "last_updated_iso": datetime.now().isoformat(),
        }, indent=2))
    except Exception as e:
        logger.warning(f"Failed to save transcripts_seen cache: {e}")


def _strip_html(html: str) -> str:
    """Very light HTML -> text. Avoids a hard bs4 dependency for the fallback path;
    bs4 is used in the structured scrapers below where it's worth it."""
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ============================================================
# Source 1 — Direct IR-site scrapers (SK Hynix, Samsung, Micron)
# ============================================================

def _scrape_ir_site(company: str, cfg: dict, days_back: int) -> list[TranscriptResult]:
    """Scrape one company's IR site for recent earnings events.

    Defensive by construction: if the page structure can't be parsed, returns a
    single `materials_only` or `nothing` result with an explanatory note rather
    than raising. The `transcript_reliability` hint in config tells us how hard
    to flag — "low" reliability companies get a louder note when only materials
    are found.
    """
    ir_url = cfg.get("ir_url")
    ticker = cfg.get("ticker", "")
    reliability = cfg.get("transcript_reliability", "unknown")
    transcript_capable = cfg.get("transcript_capable", True)  # v3.32 — False = cap at materials_only
    results: list[TranscriptResult] = []

    if not ir_url:
        return [TranscriptResult(
            company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
            event_type="unknown", availability="nothing",
            source_name=f"{company} IR",
            note="No IR URL configured.",
        )]

    html = _http_get(ir_url)
    if html is None:
        return [TranscriptResult(
            company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
            event_type="unknown", availability="nothing",
            source_name=f"{company} IR", source_url=ir_url,
            note="IR page fetch failed (network or site down).",
        )]

    # Try to find transcript/earnings-related links on the IR page.
    # We look for anchors whose text or href hints at earnings calls / transcripts /
    # results presentations. This is intentionally broad — IR sites vary a lot.
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        for a in soup.find_all("a", href=True):
            label = (a.get_text() or "").strip().lower()
            href = a["href"].lower()
            if any(kw in label or kw in href for kw in (
                "transcript", "earnings call", "conference call", "results call",
                "earnings release", "quarterly results", "ir presentation",
                "earnings presentation",
            )):
                full_href = a["href"]
                if full_href.startswith("/"):
                    # crude base-join
                    m = re.match(r"(https?://[^/]+)", ir_url)
                    if m:
                        full_href = m.group(1) + full_href
                candidates.append((label, full_href))

        if not candidates:
            # Page loaded but nothing earnings-like found. Could be a site
            # redesign, or just no recent event. Degrade gracefully.
            note = (
                f"IR page loaded but no earnings/transcript links matched. "
                f"{'Site may have been redesigned — selectors need review. ' if html else ''}"
                f"Reliability hint: {reliability}."
            )
            return [TranscriptResult(
                company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
                event_type="unknown", availability="nothing",
                source_name=f"{company} IR", source_url=ir_url, note=note,
            )]

        # Pull the most promising candidates (cap at 4 to bound work)
        for label, href in candidates[:4]:
            page = _http_get(href)
            if page is None:
                results.append(TranscriptResult(
                    company=company, ticker=ticker,
                    event_date_iso=date.today().isoformat(),
                    event_type="earnings_call", availability="nothing",
                    source_name=f"{company} IR", source_url=href,
                    note=f"Linked earnings page fetch failed: {label}",
                ))
                continue

            text = _strip_html(page)
            availability = _classify_availability(text)

            # v3.32 — SK Hynix / Samsung IR sites publish slides + webcast only,
            # never a text transcript. Cap them so the IR scraper can never
            # masquerade as a full-transcript source. Yahoo Finance is the
            # actual transcript source for these names.
            capped = False
            if not transcript_capable and availability == "full_transcript":
                availability = "materials_only"
                capped = True

            # Build an explanatory note, louder for low-reliability companies
            note = None
            if availability == "materials_only":
                if capped:
                    note = (
                        f"IR site is materials-only by design ({company} publishes "
                        f"slides + webcast, no text transcript). Full call commentary "
                        f"comes from Yahoo Finance, not here."
                    )
                else:
                    note = (
                        f"Earnings materials captured but FULL CALL COMMENTARY NOT AVAILABLE "
                        f"in text form ({len(text)} chars — below transcript threshold). "
                    )
                    if reliability == "low":
                        note += (
                            "Expected for this company — Korean-hosted call, English "
                            "transcript often posted days late or only as webcast. "
                            "Yahoo Finance carries the full call text for this name."
                        )
            elif availability == "nothing":
                note = f"Linked page had no usable text: {label}"

            results.append(TranscriptResult(
                company=company, ticker=ticker,
                event_date_iso=date.today().isoformat(),  # IR pages rarely expose clean dates; digester refines
                event_type="earnings_call",
                availability=availability,
                text=text if availability != "nothing" else "",
                source_name=f"{company} IR",
                source_url=href,
                note=note,
            ))

    except ImportError:
        # bs4 not available — fall back to the regex stripper on the IR page itself
        logger.warning("bs4 not available; using regex fallback for IR scrape")
        text = _strip_html(html)
        availability = _classify_availability(text)
        # v3.32 — honour transcript_capable in the fallback path too
        if not transcript_capable and availability == "full_transcript":
            availability = "materials_only"
        if availability == "full_transcript":
            # unlikely from an index page, but accept it
            results.append(TranscriptResult(
                company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
                event_type="earnings_call", availability="full_transcript",
                text=text, source_name=f"{company} IR", source_url=ir_url,
            ))
        else:
            results.append(TranscriptResult(
                company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
                event_type="unknown", availability="materials_only" if availability == "materials_only" else "nothing",
                text=text if availability == "materials_only" else "",
                source_name=f"{company} IR", source_url=ir_url,
                note="bs4 unavailable — only index-page text captured, no per-event transcript.",
            ))
    except Exception as e:
        logger.warning(f"IR scrape parse failed for {company}: {e}")
        results.append(TranscriptResult(
            company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
            event_type="unknown", availability="nothing",
            source_name=f"{company} IR", source_url=ir_url,
            note=f"IR scrape parse error: {e}",
        ))

    return results


def fetch_ir_transcripts(ir_sources: dict, days_back: int = 10) -> list[TranscriptResult]:
    """Scrape all configured IR sites. Never raises — each company is isolated."""
    all_results: list[TranscriptResult] = []
    for company, cfg in ir_sources.items():
        try:
            company_results = _scrape_ir_site(company, cfg, days_back)
            all_results.extend(company_results)
            states = [r.availability for r in company_results]
            logger.info(f"IR scrape {company}: {states}")
        except Exception as e:
            logger.warning(f"IR scrape failed entirely for {company}: {e}")
            all_results.append(TranscriptResult(
                company=company, ticker=cfg.get("ticker", ""),
                event_date_iso=date.today().isoformat(),
                event_type="unknown", availability="nothing",
                source_name=f"{company} IR",
                note=f"Scraper raised: {e}",
            ))
    return all_results


# ============================================================
# Source 2 — Motley Fool transcripts (US-listed, English calls)
# ============================================================

# v3.33: per-ticker page (persistent — lists all transcripts for the company).
# Replaced the global recent-transcripts index used through v3.32, which only
# showed the last ~20 transcripts site-wide and rolled off in 24-48h — by which
# time anything from our basket (mega-caps reporting once/quarter) was gone.
MOTLEY_FOOL_QUOTE_PAGE = "https://www.fool.com/quote/{exchange}/{ticker}/"

# Regex matching Motley Fool transcript URLs. Two variants exist:
#   /earnings/call-transcripts/YYYY/MM/DD/{slug}
#   /{article_id}/earnings/call-transcripts/YYYY/MM/DD/{slug}
# Both lead to the same content; we match either and extract the date.
MOTLEY_FOOL_TRANSCRIPT_PATTERN = re.compile(
    r"/(?:\d+/)?earnings/call-transcripts/(\d{4})/(\d{2})/(\d{2})/[^\"\s'?#]+"
)


def _scrape_motley_fool_ticker(
    company: str, exchange: str, ticker: str, days_back: int,
    seen_urls: set[str],
) -> TranscriptResult:
    """Look for the most recent Motley Fool transcript for one ticker.

    v3.33: hits the per-ticker quote page (persistent — lists all transcripts
    for the company), parses anchors for the call-transcripts URL pattern,
    sorts by URL-embedded date, and takes the newest.

    Cache: if the matched URL is already in seen_urls, returns an
    already_processed stub WITHOUT re-fetching the body or running the
    digester again on the same content. Cleared automatically when a NEW
    quarter's transcript appears.

    Defensive: any failure -> a `nothing` result with a note.
    """
    quote_url = MOTLEY_FOOL_QUOTE_PAGE.format(exchange=exchange.lower(), ticker=ticker.lower())
    page_html = _http_get(quote_url)
    if page_html is None:
        return TranscriptResult(
            company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
            event_type="earnings_call", availability="nothing",
            source_name="Motley Fool", source_url=quote_url,
            note=f"Motley Fool quote page fetch failed for {ticker}.",
        )

    # Find all transcript links + the date encoded in their URL
    transcript_links = []  # list of (date_tuple, full_url)
    for m in MOTLEY_FOOL_TRANSCRIPT_PATTERN.finditer(page_html):
        href = m.group(0)
        full_url = href if href.startswith("http") else f"https://www.fool.com{href}"
        # De-dupe — the page renders the same link twice (with and without article ID)
        if any(full_url == u for _, u in transcript_links):
            continue
        date_tuple = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        transcript_links.append((date_tuple, full_url))

    if not transcript_links:
        return TranscriptResult(
            company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
            event_type="earnings_call", availability="nothing",
            source_name="Motley Fool", source_url=quote_url,
            note=(
                f"No transcript links found on Motley Fool quote page for {ticker}. "
                f"Either no recent transcripts indexed, or Motley Fool changed the "
                f"quote page structure — selector may need review."
            ),
        )

    # Newest transcript first
    transcript_links.sort(key=lambda x: x[0], reverse=True)
    newest_date_tuple, match_href = transcript_links[0]
    newest_date_iso = f"{newest_date_tuple[0]:04d}-{newest_date_tuple[1]:02d}-{newest_date_tuple[2]:02d}"

    # Cache check — skip body fetch + digest if we've already processed this URL
    if match_href in seen_urls:
        return TranscriptResult(
            company=company, ticker=ticker,
            event_date_iso=newest_date_iso,
            event_type="earnings_call",
            availability="already_processed",
            source_name="Motley Fool",
            source_url=match_href,
            note="Already digested in a previous deep-tier run (cache hit).",
        )

    # Not seen before → fetch the transcript body
    page = _http_get(match_href)
    if page is None:
        return TranscriptResult(
            company=company, ticker=ticker, event_date_iso=newest_date_iso,
            event_type="earnings_call", availability="nothing",
            source_name="Motley Fool", source_url=match_href,
            note="Transcript page fetch failed.",
        )

    try:
        from bs4 import BeautifulSoup
        psoup = BeautifulSoup(page, "html.parser")
        body = psoup.find("article") or psoup.find("div", class_=re.compile(r"article|content|body", re.I))
        text = _strip_html(str(body)) if body else _strip_html(page)
    except ImportError:
        logger.warning("bs4 not available; Motley Fool scrape using regex stripper")
        text = _strip_html(page)
    except Exception as e:
        logger.warning(f"Motley Fool parse failed for {company} ({ticker}): {e}")
        return TranscriptResult(
            company=company, ticker=ticker, event_date_iso=newest_date_iso,
            event_type="earnings_call", availability="nothing",
            source_name="Motley Fool", source_url=match_href,
            note=f"Motley Fool parse error: {e}",
        )

    availability = _classify_availability(text)
    note = None
    if availability == "materials_only":
        note = (
            f"Motley Fool page captured but below full-transcript length "
            f"({len(text)} chars) — may be a preview/stub rather than the "
            f"complete call. Commentary may be incomplete."
        )
    elif availability == "nothing":
        note = "Motley Fool transcript page had no usable text."

    return TranscriptResult(
        company=company, ticker=ticker,
        event_date_iso=newest_date_iso,
        event_type="earnings_call",
        availability=availability,
        text=text if availability != "nothing" else "",
        source_name="Motley Fool",
        source_url=match_href,
        note=note,
    )


def fetch_motley_fool_transcripts(
    mf_tickers: dict, days_back: int = 10,
    seen_urls: Optional[set[str]] = None,
) -> list[TranscriptResult]:
    """Scrape Motley Fool for all configured US tickers. Never raises.

    mf_tickers: maps company display name -> {"exchange": "nasdaq"|"nyse", "ticker": ...}
    seen_urls: optional cache of already-digested URLs; matching results return
               as `already_processed` stubs (no body fetch, no Haiku call)
    """
    if seen_urls is None:
        seen_urls = set()
    results: list[TranscriptResult] = []
    for company, cfg in mf_tickers.items():
        # Tolerate both old flat shape and new {exchange, ticker} shape
        if isinstance(cfg, str):
            exchange, ticker = "nasdaq", cfg
        else:
            exchange = cfg.get("exchange", "nasdaq")
            ticker = cfg.get("ticker", "")
        try:
            result = _scrape_motley_fool_ticker(company, exchange, ticker, days_back, seen_urls)
            results.append(result)
            logger.info(f"Motley Fool scrape {company}: {result.availability}")
        except Exception as e:
            logger.warning(f"Motley Fool scrape failed entirely for {company}: {e}")
            results.append(TranscriptResult(
                company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
                event_type="earnings_call", availability="nothing",
                source_name="Motley Fool",
                note=f"Scraper raised: {e}",
            ))
    return results


# ============================================================
# Source 3 — Yahoo Finance transcripts (PRIMARY — all names, incl. KRX)
# ============================================================

YAHOO_EARNINGS_INDEX = "https://finance.yahoo.com/quote/{ticker}/earnings-calls/"


def _scrape_yahoo_ticker(company: str, ticker: str, days_back: int,
                          seen_urls: set[str]) -> TranscriptResult:
    """Look for a recent Yahoo Finance earnings-call transcript for one ticker.

    v3.33: routes through residential proxy (Bright Data via RESIDENTIAL_PROXY_URL
    env). Yahoo blocks datacenter IPs with 404 — empirical validation 2026-05-16
    showed residential proxy + realistic Chrome headers + per-request session
    rotation + redirect-following returns 200. _http_get handles all of those
    when use_proxy=True.

    Cache: if the matched transcript URL is already in seen_urls, returns an
    already_processed stub WITHOUT fetching the body — preserves Bright Data
    bandwidth on transcripts we've already captured.

    Defensive: any failure -> a `nothing` result with a note.
    """
    # Per-request session token forces a fresh residential IP for this ticker —
    # avoids Yahoo's per-IP rate limit accumulating across the 15-ticker loop.
    session_id = str(random.randint(100000, 999999))

    index_url = YAHOO_EARNINGS_INDEX.format(ticker=ticker)
    index_html = _http_get(index_url, use_proxy=True, session_id=session_id)
    if index_html is None:
        return TranscriptResult(
            company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
            event_type="earnings_call", availability="nothing",
            source_name="Yahoo Finance", source_url=index_url,
            note="Yahoo Finance earnings-calls index fetch failed (proxy unavailable, blocked, or rate-limited).",
        )

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(index_html, "html.parser")
        # Transcript links: href contains '/earnings/' and 'earnings_call'.
        # The index is ordered newest-first, so the first match is the latest call.
        match_href = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/earnings/" in href and "earnings_call" in href:
                match_href = href if href.startswith("http") else f"https://finance.yahoo.com{href}"
                break

        if not match_href:
            return TranscriptResult(
                company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
                event_type="earnings_call", availability="nothing",
                source_name="Yahoo Finance", source_url=index_url,
                note=(
                    f"No transcript link found on Yahoo earnings-calls index for "
                    f"{company} ({ticker}). Either no recent call, or Yahoo changed "
                    f"the index structure — selectors need review."
                ),
            )

        # Cache check — skip body fetch + Haiku digester if already processed.
        # Bandwidth saver: avoids re-burning proxy GB on stale transcripts.
        if match_href in seen_urls:
            return TranscriptResult(
                company=company, ticker=ticker,
                event_date_iso=date.today().isoformat(),
                event_type="earnings_call",
                availability="already_processed",
                source_name="Yahoo Finance",
                source_url=match_href,
                note="Already digested in a previous deep-tier run (cache hit).",
            )

        # Fresh session for the actual transcript fetch — Yahoo's rate limit is
        # per-IP, so rotating between index-fetch and transcript-fetch reduces
        # the chance of accumulating 429-eligible signal on a single IP.
        session_id_2 = str(random.randint(100000, 999999))
        page = _http_get(match_href, use_proxy=True, session_id=session_id_2)
        if page is None:
            return TranscriptResult(
                company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
                event_type="earnings_call", availability="nothing",
                source_name="Yahoo Finance", source_url=match_href,
                note="Yahoo transcript page fetch failed (proxy or rate-limit).",
            )

        psoup = BeautifulSoup(page, "html.parser")
        body = psoup.find("article") or psoup.find("div", class_=re.compile(r"body|content|caas", re.I))
        text = _strip_html(str(body)) if body else _strip_html(page)
        availability = _classify_availability(text)

        note = None
        if availability == "materials_only":
            note = (
                f"Yahoo page captured but below full-transcript length "
                f"({len(text)} chars). Likely a historical call behind Yahoo's "
                f"subscription gate, or a stub — latest call is normally free and full."
            )
        elif availability == "nothing":
            note = "Yahoo transcript page had no usable text."

        return TranscriptResult(
            company=company, ticker=ticker,
            event_date_iso=date.today().isoformat(),  # digester refines from page content
            event_type="earnings_call",
            availability=availability,
            text=text if availability != "nothing" else "",
            source_name="Yahoo Finance",
            source_url=match_href,
            note=note,
        )

    except ImportError:
        logger.warning("bs4 not available; Yahoo Finance scrape degraded")
        return TranscriptResult(
            company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
            event_type="earnings_call", availability="nothing",
            source_name="Yahoo Finance", source_url=index_url,
            note="bs4 unavailable — cannot parse Yahoo earnings-calls index.",
        )
    except Exception as e:
        logger.warning(f"Yahoo Finance scrape failed for {company} ({ticker}): {e}")
        return TranscriptResult(
            company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
            event_type="earnings_call", availability="nothing",
            source_name="Yahoo Finance", source_url=index_url,
            note=f"Yahoo Finance scrape error: {e}",
        )


# v3.33: 3-second delay between Yahoo ticker requests. Stays well under
# Yahoo's per-host rate limit even if multiple tickers happen to draw the
# same residential IP from Bright Data's rotation pool.
YAHOO_INTER_REQUEST_DELAY_SEC = 3.0


def fetch_yahoo_finance_transcripts(
    yahoo_tickers: dict, days_back: int = 10,
    seen_urls: Optional[set[str]] = None,
) -> list[TranscriptResult]:
    """Scrape Yahoo Finance for all configured tickers. Never raises.

    yahoo_tickers: company display name -> canonical Yahoo ticker. Use ONE
        ticker per company; Yahoo syndicates the same transcript across many
        listing variants (e.g. SK Hynix as 000660.KS, HY9H.F, HY9H.DU, ...) —
        the canonical map prevents double-counting.
    seen_urls: optional cache of already-digested URLs.

    v3.33: 3-second inter-ticker delay; uses residential proxy (handled in
    _scrape_yahoo_ticker).
    """
    if seen_urls is None:
        seen_urls = set()
    results: list[TranscriptResult] = []
    items = list(yahoo_tickers.items())
    for idx, (company, ticker) in enumerate(items):
        try:
            result = _scrape_yahoo_ticker(company, ticker, days_back, seen_urls)
            results.append(result)
            logger.info(f"Yahoo scrape {company} ({ticker}): {result.availability}")
        except Exception as e:
            logger.warning(f"Yahoo Finance scrape failed entirely for {company}: {e}")
            results.append(TranscriptResult(
                company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
                event_type="earnings_call", availability="nothing",
                source_name="Yahoo Finance",
                note=f"Scraper raised: {e}",
            ))
        # Inter-ticker delay (skip after the last one to save 3s)
        if idx < len(items) - 1:
            time.sleep(YAHOO_INTER_REQUEST_DELAY_SEC)
    return results


# ============================================================
# Aggregator — what deep_tier calls
# ============================================================

def fetch_all_transcripts(
    ir_sources: dict,
    motley_fool_tickers: dict,
    yahoo_tickers: dict,
    days_back: int = 10,
) -> dict:
    """v3.33: Sequential-cascade aggregator. Motley Fool first → IR fill-ins →
    Yahoo (via residential proxy) ONLY for companies still missing a full
    transcript at that point. The goal is "get the transcript" while keeping
    proxy bandwidth scoped to names that genuinely need it.

    Returns:
        {
          "fetched_at_iso": ...,
          "results": [TranscriptResult.to_dict(), ...],
          "summary": {
             "full_transcript": N, "materials_only": N,
             "nothing": N, "already_processed": N,
             "by_company": { "SK Hynix": "full_transcript", ... },
             "yahoo_attempted_for": [list of company names Yahoo ran for],
          }
        }

    Cascade logic:
      1. Motley Fool — runs for all 13 US tickers in MOTLEY_FOOL_TICKERS.
         Direct fetch, no proxy. Covers US mega-caps including AMAT, NVDA, AVGO,
         MSFT, GOOGL, META, AMZN, ORCL, DELL, HPE, ASML, TSM, MU.
      2. IR scrapers — runs for SK Hynix, Samsung, Micron. Direct fetch.
         Captures earnings release / slide decks (materials_only) for SK Hynix
         and Samsung; full transcript for Micron.
      3. Yahoo fallback — runs ONLY for names without a full_transcript so far.
         Routes through residential proxy. The Korean names (SK Hynix, Samsung)
         always need this (no Motley Fool coverage); US names need it only if
         Motley Fool returned nothing/materials_only this run.

    De-dup is by (company, event_date, event_type), keeping the BEST
    availability state. URL cache short-circuits already-digested transcripts
    to `already_processed`.
    """
    seen_urls = _load_seen_urls()

    # Step 1 — Motley Fool (primary for US names)
    logger.info("Transcript cascade step 1: Motley Fool (primary for US names)")
    mf_results = fetch_motley_fool_transcripts(motley_fool_tickers, days_back=days_back, seen_urls=seen_urls)

    # Step 2 — IR scrapers
    logger.info("Transcript cascade step 2: IR-site scrapers")
    ir_results = fetch_ir_transcripts(ir_sources, days_back=days_back)

    # Step 3 — figure out who still needs Yahoo
    # Build a per-company "best result so far" view from MF + IR
    _rank = {"full_transcript": 4, "already_processed": 3, "materials_only": 2, "nothing": 1}
    covered: dict[str, str] = {}  # company -> best availability so far
    for r in mf_results + ir_results:
        cur = covered.get(r.company.lower())
        if cur is None or _rank.get(r.availability, 0) > _rank.get(cur, 0):
            covered[r.company.lower()] = r.availability

    # v3.33: Yahoo fires for ANYTHING that isn't a fresh full_transcript from
    # this run. Per Raul's rule: "goal is the transcript" — cost is secondary.
    # This catches:
    #   • "nothing"           — MF + IR found nothing → Yahoo tries
    #   • "materials_only"    — only slides found → Yahoo tries for the call text
    #   • "already_processed" — MF returned an OLD cached URL; the new quarter
    #                           may already be on Yahoo even before MF transcribes.
    #                           Yahoo's own URL cache handles the no-new-content
    #                           case (one ~50KB index fetch + skip).
    #   • not present at all  — Yahoo is the only source for SK Hynix + Samsung
    # The only state that skips Yahoo is "full_transcript" captured this run.
    yahoo_to_run = {}
    for company, ticker in yahoo_tickers.items():
        cur = covered.get(company.lower())  # None if MF + IR didn't see this company
        if cur != "full_transcript":
            yahoo_to_run[company] = ticker

    yahoo_results: list[TranscriptResult] = []
    if yahoo_to_run:
        logger.info(
            f"Transcript cascade step 3: Yahoo Finance fallback for {len(yahoo_to_run)} "
            f"companies missing full transcript: {list(yahoo_to_run.keys())}"
        )
        yahoo_results = fetch_yahoo_finance_transcripts(yahoo_to_run, days_back=days_back, seen_urls=seen_urls)
    else:
        logger.info("Transcript cascade step 3: skipped (Motley Fool + IR covered everything)")

    # De-dup. Motley Fool first → wins ties (it's the cheaper source). Then IR.
    # Yahoo only beats them on strictly-better availability.
    best: dict[tuple, TranscriptResult] = {}
    for r in mf_results + ir_results + yahoo_results:
        key = (r.company.lower(), r.event_date_iso, r.event_type)
        if key not in best or _rank.get(r.availability, 0) > _rank.get(best[key].availability, 0):
            best[key] = r

    results = list(best.values())

    # Persist URL cache — add any new URL we processed with full or materials content.
    # We do NOT add already_processed results (those came FROM the cache).
    for r in results:
        if r.source_url and r.availability in ("full_transcript", "materials_only"):
            seen_urls.add(r.source_url)
    _save_seen_urls(seen_urls)

    summary = {
        "full_transcript": sum(1 for r in results if r.availability == "full_transcript"),
        "materials_only": sum(1 for r in results if r.availability == "materials_only"),
        "nothing": sum(1 for r in results if r.availability == "nothing"),
        "already_processed": sum(1 for r in results if r.availability == "already_processed"),
        "by_company": {r.company: r.availability for r in results},
        "yahoo_attempted_for": list(yahoo_to_run.keys()),
    }
    logger.info(
        f"Transcript aggregate: {summary['full_transcript']} full, "
        f"{summary['materials_only']} materials-only, "
        f"{summary['already_processed']} already-processed (cached), "
        f"{summary['nothing']} nothing"
    )

    return {
        "fetched_at_iso": datetime.now().isoformat(),
        "results": [r.to_dict() for r in results],
        "summary": summary,
    }


def fetch_transcripts_with_retry(
    ir_sources: dict,
    motley_fool_tickers: dict,
    yahoo_tickers: dict,
) -> tuple[Optional[dict], dict]:
    """Mirror of the old fetch_quartr_with_retry signature, for deep_tier.

    Unlike Quartr (which needed an API key), these are free scrapers — there's
    no 'skipped, no key' path. If everything fails, returns a payload with all
    'nothing' rather than None, so the digester still records the gap.
    """
    try:
        payload = fetch_all_transcripts(ir_sources, motley_fool_tickers, yahoo_tickers)
        return payload, {"status": "ok", "summary": payload["summary"]}
    except Exception as e:
        logger.error(f"Transcript aggregate failed entirely: {e}")
        return None, {"status": "error", "reason": str(e)}
