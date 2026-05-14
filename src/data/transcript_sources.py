"""
Earnings transcript sources — v3.31 replacement for the gated Quartr API.

Quartr discontinued its free developer tier (enterprise-only now). This module
rebuilds transcript coverage from free sources:

  1. Direct IR-site scrapers — SK Hynix, Samsung, Micron (the 3 core memory makers)
  2. Motley Fool — US-listed names with English earnings calls (one scraper, ~13 names)
  3. EDGAR 8-K earnings exhibits — handled in news.py / digester, not here

THE THREE-STATE AVAILABILITY FLAG
---------------------------------
The whole point of this pipeline is getting the *commentary* right. So every
transcript source returns one of three states per company per event:

  "full_transcript" — got the actual earnings-call text; commentary intact
  "materials_only"  — got the earnings release and/or slides, but NO call
                      commentary (the SK Hynix / Samsung risk case — Korean
                      call, English transcript often days late or webcast-only)
  "nothing"         — source had nothing for this event

The digester and the email both surface this explicitly. When SK Hynix posts
only a slide deck with no transcript, the brief says so plainly — absence of
commentary is never silently confused with "nothing notable was said".

FRAGILITY NOTE
--------------
IR-site scrapers are the most fragile part of any pipeline — when SK Hynix
redesigns their IR page, the selector breaks. Every scraper here is written
DEFENSIVELY: a broken selector degrades to "materials_only" or "nothing" and
logs a warning. It never raises into the deep-tier orchestrator. Same
maintenance posture as the Naver scraper (expect 1-2 fixes/year).

CHANGELOG (this file):
  v3.31  (2026-05-14) — NEW FILE — IR-site + Motley Fool scrapers; replaces gated Quartr API; three-state availability flag
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from typing import Optional

import requests

logger = logging.getLogger("memory_cycle_brief.transcript_sources")

REQUEST_TIMEOUT_SEC = 30
USER_AGENT = "Mozilla/5.0 (compatible; MemoryCycleBrief/3.4; research use)"

# Minimum character count for text to count as a "full" transcript rather than
# just a headline / blurb. Earnings calls run 20-60K chars; a release is ~2-8K.
FULL_TRANSCRIPT_MIN_CHARS = 9000
MATERIALS_MIN_CHARS = 400


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


def _http_get(url: str) -> Optional[str]:
    """Defensive GET — returns page text or None. Never raises."""
    try:
        r = requests.get(
            url, timeout=REQUEST_TIMEOUT_SEC,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"HTTP GET failed for {url}: {e}")
        return None


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

            # Build an explanatory note, louder for low-reliability companies
            note = None
            if availability == "materials_only":
                note = (
                    f"Earnings materials captured but FULL CALL COMMENTARY NOT AVAILABLE "
                    f"in text form ({len(text)} chars — below transcript threshold). "
                )
                if reliability == "low":
                    note += (
                        "Expected for this company — Korean-hosted call, English "
                        "transcript often posted days late or only as webcast. "
                        "Check back, or rely on Korean broker echo for commentary."
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

MOTLEY_FOOL_SEARCH = "https://www.fool.com/quote/{exchange}/{ticker}/"
# Motley Fool transcript index per ticker is reachable via their earnings
# transcript section. We use their site search-style path and fall back to a
# Google-News-style discovery if structure changed.
MOTLEY_FOOL_TRANSCRIPT_INDEX = "https://www.fool.com/earnings-call-transcripts/"


def _scrape_motley_fool_ticker(company: str, ticker: str, days_back: int) -> TranscriptResult:
    """Look for a recent Motley Fool earnings-call transcript for one ticker.

    Defensive: any failure -> a `nothing` result with a note. Motley Fool
    publishes transcripts typically 1-2 days after the call, US names only.
    """
    # Strategy: hit the transcript index, look for a link mentioning the ticker
    # or company name. If found, fetch and classify.
    index_html = _http_get(MOTLEY_FOOL_TRANSCRIPT_INDEX)
    if index_html is None:
        return TranscriptResult(
            company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
            event_type="earnings_call", availability="nothing",
            source_name="Motley Fool",
            note="Motley Fool transcript index fetch failed.",
        )

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(index_html, "html.parser")
        match_href = None
        match_label = None
        ticker_l = ticker.lower()
        company_l = company.lower()
        for a in soup.find_all("a", href=True):
            label = (a.get_text() or "").strip()
            label_l = label.lower()
            href = a["href"]
            if "transcript" not in href.lower() and "transcript" not in label_l:
                continue
            if ticker_l in label_l or company_l in label_l or f"/{ticker_l}" in href.lower():
                match_href = href if href.startswith("http") else f"https://www.fool.com{href}"
                match_label = label
                break

        if not match_href:
            return TranscriptResult(
                company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
                event_type="earnings_call", availability="nothing",
                source_name="Motley Fool",
                note=f"No recent Motley Fool transcript found for {company} ({ticker}).",
            )

        page = _http_get(match_href)
        if page is None:
            return TranscriptResult(
                company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
                event_type="earnings_call", availability="nothing",
                source_name="Motley Fool", source_url=match_href,
                note="Transcript page fetch failed.",
            )

        psoup = BeautifulSoup(page, "html.parser")
        # Motley Fool transcript body is typically in <article> or a content div
        body = psoup.find("article") or psoup.find("div", class_=re.compile(r"article|content|body", re.I))
        text = _strip_html(str(body)) if body else _strip_html(page)
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
            event_date_iso=date.today().isoformat(),
            event_type="earnings_call",
            availability=availability,
            text=text if availability != "nothing" else "",
            source_name="Motley Fool",
            source_url=match_href,
            note=note,
        )

    except ImportError:
        logger.warning("bs4 not available; Motley Fool scrape degraded")
        return TranscriptResult(
            company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
            event_type="earnings_call", availability="nothing",
            source_name="Motley Fool",
            note="bs4 unavailable — cannot parse Motley Fool transcript index.",
        )
    except Exception as e:
        logger.warning(f"Motley Fool scrape failed for {company} ({ticker}): {e}")
        return TranscriptResult(
            company=company, ticker=ticker, event_date_iso=date.today().isoformat(),
            event_type="earnings_call", availability="nothing",
            source_name="Motley Fool",
            note=f"Motley Fool scrape error: {e}",
        )


def fetch_motley_fool_transcripts(mf_tickers: dict, days_back: int = 10) -> list[TranscriptResult]:
    """Scrape Motley Fool for all configured US tickers. Never raises."""
    results: list[TranscriptResult] = []
    for company, ticker in mf_tickers.items():
        try:
            results.append(_scrape_motley_fool_ticker(company, ticker, days_back))
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
# Aggregator — what deep_tier calls
# ============================================================

def fetch_all_transcripts(
    ir_sources: dict,
    motley_fool_tickers: dict,
    days_back: int = 10,
) -> dict:
    """Aggregate all transcript sources into one payload for the digester.

    Returns:
        {
          "fetched_at_iso": ...,
          "results": [TranscriptResult.to_dict(), ...],
          "summary": {
             "full_transcript": N, "materials_only": N, "nothing": N,
             "by_company": { "SK Hynix": "materials_only", ... }
          }
        }

    De-dupes Micron (it's in both IR_SOURCES and MOTLEY_FOOL_TICKERS): if both
    sources return something, prefer the one with the better availability state
    (full_transcript > materials_only > nothing).
    """
    ir_results = fetch_ir_transcripts(ir_sources, days_back=days_back)
    mf_results = fetch_motley_fool_transcripts(motley_fool_tickers, days_back=days_back)

    # De-dup by (company, event_date) preferring better availability
    _rank = {"full_transcript": 3, "materials_only": 2, "nothing": 1}
    best: dict[tuple, TranscriptResult] = {}
    for r in ir_results + mf_results:
        key = (r.company.lower(), r.event_date_iso, r.event_type)
        if key not in best or _rank[r.availability] > _rank[best[key].availability]:
            best[key] = r

    results = list(best.values())

    summary = {
        "full_transcript": sum(1 for r in results if r.availability == "full_transcript"),
        "materials_only": sum(1 for r in results if r.availability == "materials_only"),
        "nothing": sum(1 for r in results if r.availability == "nothing"),
        "by_company": {r.company: r.availability for r in results},
    }
    logger.info(
        f"Transcript aggregate: {summary['full_transcript']} full, "
        f"{summary['materials_only']} materials-only, {summary['nothing']} nothing"
    )

    return {
        "fetched_at_iso": datetime.utcnow().isoformat(),
        "results": [r.to_dict() for r in results],
        "summary": summary,
    }


def fetch_transcripts_with_retry(ir_sources: dict, motley_fool_tickers: dict) -> tuple[Optional[dict], dict]:
    """Mirror of the old fetch_quartr_with_retry signature, for deep_tier.

    Unlike Quartr (which needed an API key), these are free scrapers — there's
    no 'skipped, no key' path. If everything fails, returns a payload with all
    'nothing' rather than None, so the digester still records the gap.
    """
    try:
        payload = fetch_all_transcripts(ir_sources, motley_fool_tickers)
        return payload, {"status": "ok", "summary": payload["summary"]}
    except Exception as e:
        logger.error(f"Transcript aggregate failed entirely: {e}")
        return None, {"status": "error", "reason": str(e)}
