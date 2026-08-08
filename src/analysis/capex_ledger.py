"""v3.45.0 — Hyperscaler CapEx guidance ledger + deterministic plateau scoring.

Raul's design (2026-08-08): per company, latest print vs its OWN prior
forward guidance. Points: raise >5% = -1, confirm ±5% = +1, cut >5% = +2,
withheld = 0 + watch flag, no print = 0 ("awaiting print").
STATUS IS DECIDED BY COUNT ALONE (ruling A, 2026-08-08): N = companies at
stable-or-cutting: 0 -> Not fired, 1-2 -> Partially fired, 3-4 -> Fired.
The score S is intensity display only — it never moves the band.

Layer 2 proposes ledger rows via LEDGER_UPDATE lines in its reasoning;
this module parses, validates, persists, and computes. Arithmetic decides.
"""
import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LEDGER_PATH = Path("state/capex_guidance_ledger.json")

# v3.46.1 — event-anchored updates. Root cause of the Aug-8 defect: Layer 2
# re-reported the same Aug-7 Amazon call; the duplicate guard compared raw
# period strings ("2026" != "CY2026"), the echo superseded, and
# prior_mid := 220 erased the $200B->$220B raise (stance accelerating ->
# stable, plateau 2/4 -> 3/4 Fired). Fixes: (a) period canonicalization,
# (b) same-event-date duplicates are no-ops, (c) updates must anchor to a
# known earnings-call date (state/earnings_calendar_cache.json) — coverage
# restating an old print is NOT a new guidance event, (d) the contract
# carries stated_prior / verb / basis so a print brings its own baseline
# and words corroborate (tripwire) while ARITHMETIC DECIDES (Raul's
# Jul-31 rule: qualifiers never override numbers).

COMPANY_TICKER = {"Microsoft": "MSFT", "Alphabet": "GOOGL",
                  "Amazon": "AMZN", "Meta": "META"}
_RAISE_VERBS = {"raised", "raises", "raise", "lifted", "boosted", "ups",
                "increased", "hiked"}
_CUT_VERBS = {"cut", "cuts", "lowered", "reduced", "trimmed", "slashed"}
_STABLE_VERBS = {"unchanged", "reiterated", "maintained", "confirmed",
                 "narrowed", "reaffirmed"}
_REVISION_VERBS = _RAISE_VERBS | _CUT_VERBS  # official intra-quarter moves


def _norm_period(p: str) -> str:
    """Canonicalize period labels: '2026'->'CY2026', 'FY27'->'FY2027'."""
    p = (p or "").strip().upper()
    m = re.fullmatch(r"(20\d\d)", p)
    if m:
        return f"CY{m.group(1)}"
    m = re.fullmatch(r"FY(\d\d)", p)
    if m:
        return f"FY20{m.group(1)}"
    return p


def _known_call_dates(company: str) -> "set[str] | None":
    """ISO call dates for the company from the earnings-calendar cache.
    None = no calendar data (validation degrades to warn, never blocks)."""
    try:
        from src.data.transcript_sources import _load_earnings_cache
        cache = _load_earnings_cache() or {}
    except Exception:
        return None
    tick = COMPANY_TICKER.get(company)
    entry = cache.get(tick) or cache.get(company) or {}
    dates = set()
    for k in ("earnings_date", "prev_earnings_date", "last_earnings_date"):
        if entry.get(k):
            dates.add(str(entry[k])[:10])
    for d in entry.get("recent_earnings_dates", []) or []:
        dates.add(str(d)[:10])
    return dates or None


def _near(iso_a: str, iso_b: str, days: int = 1) -> bool:
    try:
        return abs((date.fromisoformat(iso_a) - date.fromisoformat(iso_b)).days) <= days
    except Exception:
        return False

COMPANIES = ("Microsoft", "Alphabet", "Amazon", "Meta")

# Seed state as of 2026-08-08, from the Jul-31 and Aug-8 briefs. Sources
# annotated; the MSFT prior/current conflict ($190B Jul-31 report vs $175B
# Aug-7 transcript) is PROVENANCE-TRACE PENDING — seeded per the Aug-7
# transcript, which Raul's approved worked example reflects.
_SEED = {
    "Microsoft": {"period": "FY27", "mid": 175.0, "low": 175.0, "high": 175.0,
                  "prior_mid": 190.0, "event_date": "2026-08-07",
                  "source": "Q4 FY26 earnings call transcript (Aug-7); prior FY27 $190B per Jul-30 print — provenance trace pending"},
    "Meta": {"period": "CY2026", "mid": 137.5, "low": 130.0, "high": 145.0,
             "prior_mid": 135.0, "event_date": "2026-08-07",
             "source": "Q2 2026 call: 2026 reiterated $130-145B vs prior $125-145B; 2027 guidance withheld",
             "withheld_forward": True},
    "Amazon": {"period": "CY2026", "mid": 220.0, "low": 220.0, "high": 220.0,
               "prior_mid": 200.0, "event_date": "2026-08-07",
               "source": "Q2 2026 call: 2026 raised to $220B from $200B, 2027 capacity doubling committed"},
    "Alphabet": {"period": "CY2027", "mid": None, "low": None, "high": None,
                 "prior_mid": None, "event_date": "2026-07-22",
                 "stance_override": "accelerating",
                 "source": "Jul-2026 print: 2027 guided above 2026, no number disclosed"},
}

LEDGER_UPDATE_RE = re.compile(
    r"LEDGER_UPDATE:\s*(?P<company>Microsoft|Alphabet|Amazon|Meta)\s*\|"
    r"\s*(?P<period>[A-Z0-9]{2,8})\s*\|"
    r"\s*\$?(?P<low>\d{2,4}(?:\.\d+)?)\s*(?:-\s*\$?(?P<high>\d{2,4}(?:\.\d+)?))?\s*B?\s*\|"
    r"\s*(?P<event_date>\d{4}-\d{2}-\d{2})\s*\|"
    r"\s*(?P<source>[^\n]{3,200})", re.IGNORECASE)


def _mig_001_amazon_prior(led: dict) -> bool:
    """Aug-8 echo restored: Amazon CY2026 prior_mid 220 -> 200 (call verbatim:
    'up from our prior estimate of about $200 billion')."""
    e = led.get("Amazon") or {}
    if (_norm_period(e.get("period", "")) == "CY2026"
            and e.get("mid") == 220.0 and e.get("prior_mid") == 220.0):
        e["prior_mid"] = 200.0
        e["period"] = "CY2026"
        e["source"] = (e.get("source", "")[:120]
                       + " [mig001: prior restored to $200B — raise erased by same-event echo]")
        led["Amazon"] = e
        return True
    return False


def _mig_002_msft_basis(led: dict) -> bool:
    """MSFT $190B->$175B is a finance->operating lease reclassification; CFO:
    'CY2026 CapEx investment expectations remain unchanged.' Ruled stable +
    basis-change flag (Raul, 2026-08-08)."""
    e = led.get("Microsoft") or {}
    if (e.get("mid") == 175.0 and e.get("prior_mid") == 190.0
            and not e.get("stance_override")):
        e["stance_override"] = "stable"
        e["basis_change"] = True
        e["flags"] = (e.get("flags") or []) + [
            "accounting basis change — investment unchanged; ruled stable"]
        e["source"] = (e.get("source", "")[:120]
                       + " [mig002: lease reclass, not an economic cut — provenance trace resolved]")
        led["Microsoft"] = e
        return True
    return False


_MIGRATIONS = [("001_amazon_prior_200", _mig_001_amazon_prior),
               ("002_msft_basis_stable", _mig_002_msft_basis)]


def _apply_migrations(led: dict, path=None) -> dict:
    done = set(led.get("_migrations") or [])
    changed = False
    for mid_, fn in _MIGRATIONS:
        if mid_ in done:
            continue
        try:
            if fn(led):
                logger.warning(f"[capex-ledger] migration {mid_} applied")
                changed = True
        except Exception as e:
            logger.warning(f"[capex-ledger] migration {mid_} failed (skipped): {e}")
        done.add(mid_)
    led["_migrations"] = sorted(done)
    if changed:
        try:
            save_ledger(led, path)
        except Exception:
            pass
    return led


def load_ledger(path=None) -> dict:
    p = Path(path or LEDGER_PATH)
    led = None
    try:
        raw = json.loads(p.read_text())
        if isinstance(raw, dict) and raw:
            led = raw
    except Exception:
        pass
    if led is None:
        logger.warning("[capex-ledger] bootstrapping ledger from in-code seed")
        led = {k: dict(v) for k, v in _SEED.items()}
    return _apply_migrations(led, path)


def save_ledger(ledger: dict, path=None) -> None:
    p = Path(path or LEDGER_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(ledger, indent=2))
    except Exception as e:
        logger.warning(f"[capex-ledger] save failed (non-fatal): {e}")


def apply_ledger_updates(texts, ledger: dict, today: Optional[str] = None,
                         calendar_dates=None) -> int:
    """Parse LEDGER_UPDATE lines from Layer 2 output texts. Returns count
    applied. v3.46.1 semantics — an update is applied only when it is a NEW
    guidance EVENT: (i) same (company, period, event_date) as stored = the
    same print re-reported -> no-op; (ii) event_date older than stored ->
    rejected; (iii) event_date not on a known earnings-call date (+-1d) ->
    rejected unless it carries an explicit revision verb + stated prior
    (official intra-quarter revision, applied flagged-for-review); no
    calendar data -> applied with an 'unverifiable' flag, never blocked.
    Optional pipe tokens after event_date: prior=<B$> verb=<word>
    basis=changed|same. stated prior sets prior_mid (a print carries its
    own baseline); verbs are tripwires only — arithmetic decides."""
    applied = 0
    today = today or date.today().isoformat()
    for text in texts or []:
        for m in LEDGER_UPDATE_RE.finditer(text or ""):
            company = m.group("company").title()
            try:
                low = float(m.group("low"))
                high = float(m.group("high") or m.group("low"))
                mid = round((low + high) / 2, 1)
                ev = m.group("event_date")
                period = _norm_period(m.group("period"))

                # optional tokens riding in the source tail
                tail = m.group("source").strip()
                stated_prior, verb, basis_change = None, None, False
                parts, keep = [p.strip() for p in tail.split("|")], []
                for p in parts:
                    pl = p.lower()
                    if pl.startswith("prior="):
                        try:
                            stated_prior = float(re.sub(r"[^0-9.]", "", p.split("=", 1)[1]))
                        except Exception:
                            pass
                    elif pl.startswith("verb="):
                        verb = p.split("=", 1)[1].strip().lower()
                    elif pl.startswith("basis="):
                        basis_change = "chang" in pl or "reclass" in pl
                    else:
                        keep.append(p)
                source = " | ".join(keep)[:200]

                if ev > today:
                    logger.warning(f"[capex-ledger] rejected future-dated update {company} {ev}")
                    continue
                cur = ledger.get(company) or {}
                cur_period = _norm_period(cur.get("period", ""))

                # (i) same event re-reported -> no-op (the Amazon-echo fix)
                if cur and cur_period == period and cur.get("event_date") == ev:
                    if cur.get("mid") != mid:
                        logger.warning(
                            f"[capex-ledger] same-event number mismatch ignored "
                            f"({company} {ev}: stored {cur.get('mid')} vs {mid}) — "
                            f"stored print kept")
                    else:
                        logger.info(f"[capex-ledger] duplicate print ignored ({company} {ev})")
                    continue
                # (ii) never supersede with an older print
                if cur.get("event_date") and ev < cur["event_date"] and cur_period == period:
                    logger.warning(f"[capex-ledger] older-than-stored update rejected ({company} {ev} < {cur['event_date']})")
                    continue
                # (iii) event anchoring against the earnings calendar
                flags = []
                dates = (calendar_dates.get(company) if isinstance(calendar_dates, dict)
                         else _known_call_dates(company))
                if dates is None:
                    flags.append("event-date unverifiable (no calendar entry)")
                elif not any(_near(ev, d) for d in dates):
                    if verb in _REVISION_VERBS and stated_prior is not None:
                        flags.append(f"intra-quarter revision ({verb}) — review")
                        logger.warning(f"[capex-ledger] intra-quarter revision applied flagged ({company} {ev})")
                    else:
                        logger.warning(
                            f"[capex-ledger] non-event-dated update rejected — "
                            f"no {company} call near {ev}; coverage restating an "
                            f"old print is not a new event")
                        continue

                prior_mid = stated_prior if stated_prior is not None else cur.get("mid")
                entry = {
                    "period": period, "mid": mid, "low": low, "high": high,
                    "prior_mid": prior_mid, "event_date": ev, "source": source,
                }
                if cur.get("withheld_forward"):
                    entry["withheld_forward"] = True
                if verb:
                    entry["verb"] = verb
                if basis_change:
                    entry["basis_change"] = True

                # verb <-> arithmetic tripwire (words corroborate, numbers decide)
                if prior_mid:
                    chg = mid / prior_mid - 1
                    arith = ("accelerating" if chg > 0.05
                             else "cutting" if chg < -0.05 else "stable")
                    vdir = ("accelerating" if verb in _RAISE_VERBS
                            else "cutting" if verb in _CUT_VERBS
                            else "stable" if verb in _STABLE_VERBS else None)
                    if vdir and vdir != arith:
                        flags.append(f"VERB-ARITHMETIC MISMATCH (verb={verb}, arithmetic={arith}) — review")
                    # Raul's MSFT ruling generalized: announced basis change +
                    # unchanged-investment verb + arithmetic 'cut' = stable w/ flag
                    if basis_change and verb in _STABLE_VERBS and arith == "cutting":
                        entry["stance_override"] = "stable"
                        flags.append("accounting basis change — investment unchanged; ruled stable")
                if flags:
                    entry["flags"] = flags
                ledger[company] = entry
                applied += 1
                logger.warning(f"[capex-ledger] {company}: {cur.get('mid')} -> {mid} ({period}, {ev})"
                               + (f" [{'; '.join(flags)}]" if flags else ""))
            except Exception as e:
                logger.warning(f"[capex-ledger] malformed LEDGER_UPDATE ignored: {e}")
    return applied


def company_stance(entry: dict):
    """-> (stance, points): accelerating -1 | stable +1 | cutting +2 | awaiting 0."""
    if not entry:
        return ("awaiting print", 0)
    if entry.get("stance_override"):
        s = entry["stance_override"]
        return (s, {"accelerating": -1, "stable": 1, "cutting": 2}.get(s, 0))
    mid, prior = entry.get("mid"), entry.get("prior_mid")
    if mid is None or prior in (None, 0):
        return ("awaiting print", 0)
    chg = round((mid / prior - 1) * 100, 2)
    if chg > 5.0:
        return ("accelerating", -1)
    if chg < -5.0:
        return ("cutting", 2)
    return ("stable", 1)


def evaluate_plateau(ledger: dict) -> dict:
    """Deterministic verdict: count bands decide status; score = intensity."""
    lines, n_at, score, flags = [], 0, 0, []
    for c in COMPANIES:
        e = ledger.get(c) or {}
        stance, pts = company_stance(e)
        if stance in ("stable", "cutting"):
            n_at += 1
        score += pts
        chg = ""
        if e.get("mid") is not None and e.get("prior_mid"):
            chg = f" ({(e['mid']/e['prior_mid']-1)*100:+.1f}% vs prior)"
        label = "BEYOND plateau — cutting" if stance == "cutting" else stance
        if stance == "stable" and e.get("basis_change"):
            label = "stable (accounting basis change)"
        lines.append(f"{c}: {label} [{pts:+d}]{chg} — {e.get('period','?')} "
                     f"{'$'+str(e.get('mid'))+'B' if e.get('mid') is not None else 'no number'}, "
                     f"print {e.get('event_date','n/a')}")
        if e.get("withheld_forward"):
            flags.append(f"{c} withheld forward guidance — watch flag")
        for fl in e.get("flags") or []:
            flags.append(f"{c}: {fl}")
    status = ("Not fired" if n_at == 0
              else "Partially fired" if n_at <= 2 else "Fired")
    return {"status": status, "count_at_or_beyond": n_at, "score": score,
            "company_lines": lines, "watch_flags": flags,
            "basis": (f"guidance ledger: {n_at}/4 at plateau-or-beyond "
                      f"(bands 0/1-2/3-4), intensity score {score:+d}")}
