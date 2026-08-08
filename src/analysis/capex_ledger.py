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


def load_ledger(path=None) -> dict:
    p = Path(path or LEDGER_PATH)
    try:
        led = json.loads(p.read_text())
        if isinstance(led, dict) and led:
            return led
    except Exception:
        pass
    logger.warning("[capex-ledger] bootstrapping ledger from in-code seed")
    return {k: dict(v) for k, v in _SEED.items()}


def save_ledger(ledger: dict, path=None) -> None:
    p = Path(path or LEDGER_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(ledger, indent=2))
    except Exception as e:
        logger.warning(f"[capex-ledger] save failed (non-fatal): {e}")


def apply_ledger_updates(texts, ledger: dict, today: Optional[str] = None) -> int:
    """Parse LEDGER_UPDATE lines from Layer 2 output texts. Returns count
    applied. A new row supersedes the entry: old mid becomes prior_mid.
    Malformed or stale (event_date > today) lines are logged and ignored."""
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
                if ev > today:
                    logger.warning(f"[capex-ledger] rejected future-dated update {company} {ev}")
                    continue
                cur = ledger.get(company) or {}
                if cur.get("mid") == mid and cur.get("period") == m.group("period").upper():
                    continue  # no change
                ledger[company] = {
                    "period": m.group("period").upper(), "mid": mid,
                    "low": low, "high": high,
                    "prior_mid": cur.get("mid"),
                    "event_date": ev, "source": m.group("source").strip()[:200],
                }
                if cur.get("withheld_forward"):
                    ledger[company]["withheld_forward"] = True
                applied += 1
                logger.warning(f"[capex-ledger] {company}: {cur.get('mid')} -> {mid} ({m.group('period').upper()}, {ev})")
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
        lines.append(f"{c}: {label} [{pts:+d}]{chg} — {e.get('period','?')} "
                     f"{'$'+str(e.get('mid'))+'B' if e.get('mid') is not None else 'no number'}, "
                     f"print {e.get('event_date','n/a')}")
        if e.get("withheld_forward"):
            flags.append(f"{c} withheld forward guidance — watch flag")
    status = ("Not fired" if n_at == 0
              else "Partially fired" if n_at <= 2 else "Fired")
    return {"status": status, "count_at_or_beyond": n_at, "score": score,
            "company_lines": lines, "watch_flags": flags,
            "basis": (f"guidance ledger: {n_at}/4 at plateau-or-beyond "
                      f"(bands 0/1-2/3-4), intensity score {score:+d}")}
