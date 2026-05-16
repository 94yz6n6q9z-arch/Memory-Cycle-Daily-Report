# Setup (technical)

For the non-developer walkthrough, see `DEPLOYMENT_GUIDE.md`. This document covers the technical details: environment variables, source dependencies, model lifecycle, and local development.

## Environment variables

Required:
- `ANTHROPIC_API_KEY` — for Haiku (Layer 1, Layer 2) + Sonnet (Layer 3)
- `GMAIL_ADDRESS` — sending Gmail address
- `GMAIL_APP_PASSWORD` — 16-char app password from <https://myaccount.google.com/apppasswords>
- `EMAIL_RECIPIENT` — receiving email address

v3 new sources (all free tier, all recommended):
- `FINNHUB_API_KEY` — US analyst rating actions for MU + semi peers + hyperscalers
- `ALPHA_VANTAGE_KEY` — EPS cross-check
- `DART_API_KEY` — Korean Financial Supervisory Service disclosures (SK Hynix + Samsung)

Optional v3 (Samsung Securities Global research distribution):
- `SAMSUNG_GMAIL_USER` — dedicated Gmail for Samsung Securities PDF distribution
- `SAMSUNG_GMAIL_APP_PW` — Gmail app password for IMAP poll

Model overrides — leave empty in normal operation:
- `HAIKU_MODEL_OVERRIDE` — pin to specific Haiku version
- `SONNET_MODEL_OVERRIDE` — pin to specific Sonnet version

## Workflow schedule

Two GitHub Actions cron jobs:

| Workflow | Schedule (UTC) | What it does |
|---|---|---|
| `deep_tier.yml` | `0 8 * * *` (08:00 daily) | Fetch earnings transcripts (Yahoo Finance + Motley Fool + IR scrapers) + Korean broker PDFs → Layer 1 digests |
| `fast_tier.yml` | `0 11`, `30 11`, `5 12 * * 1-5` (weekdays) | Fast fetch → Layer 2 → Layer 3 → email |

Fast tier has 3 cron triggers for redundancy. Idempotency via `state/brief_sent_dates.json` — only the first successful run produces output.

Deep tier `permissions: contents: write` so it can commit `state/digests/` and `state/cache/` back to the repo. Fast tier same for `state/brief_sent_dates.json` + `state/prior_state.json` + `state/eps_revisions_running.json`.

## Source criticality / retry policy (`src/state.py`)

Three tiers:
- **CRITICAL** (5× retry, 🚨 banner on failure): yfinance, Anthropic API, Naver Finance
- **IMPORTANT** (3× retry, cached stale fallback): Finnhub, Korean broker PDFs, EDGAR feeds, DART. Transcript scrapers (Yahoo Finance + Motley Fool + IR) degrade gracefully per-source rather than retrying
- **NICE_TO_HAVE** (1× retry, skip silently): RSS feeds, web scrapers, Google News

Backoff: 1s → 3s → 9s → 27s → 81s exponential. Cached fallback for IMPORTANT/CRITICAL uses `state/cache/<source_id>.json` keyed by sanitized source ID.

## Local development

```bash
# Clone + install
git clone <your-repo-url>
cd memory-cycle-brief
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set env vars (or use .env + direnv)
cp .env.example .env
# Edit .env with your keys

# Run the deep tier locally (slow — pulls transcripts + PDFs)
set -a; source .env; set +a
python -m src.deep_tier

# Run the fast tier locally (will send email!)
python -m src.fast_tier

# Or run the legacy single-shot entry (kept for ad-hoc local testing)
python -m src.main
```

To test the pipeline end-to-end without sending email, comment out the `send_email(...)` call in `src/fast_tier.py` and inspect the HTML output written to `state/failed_send_*.html`.

## Model lifecycle

`config.py` uses tier aliases (`haiku`, `sonnet`) by default. These auto-resolve to Anthropic's current recommended version per tier; when a specific dated version retires, the alias rolls forward automatically without code change.

Override paths in `HAIKU_MODEL_OVERRIDE` / `SONNET_MODEL_OVERRIDE`:

| Case | Override value |
|---|---|
| Pin to specific version for reproducibility | e.g. `claude-haiku-4-5-20251001` |
| Drop Sonnet for cost (use Haiku for everything) | `SONNET_MODEL_OVERRIDE=haiku` |
| Upgrade Layer 3 to Opus (~3-5× cost) | `SONNET_MODEL_OVERRIDE=opus` |
| Bridge a new-generation regression | pin to prior dated version temporarily |

The validation table at the bottom of every brief surfaces the resolved model name + any retry attempts, so model deprecation issues become visible without breaking the pipeline.

## Trigger framework editing

All indicators in `INDICATORS = [...]` in `config.py`. Each `Indicator` dataclass has:
- `id`, `tier`, `name`, `description`, `what_to_look_for` (plain-English summary)
- `sources` (list of source labels)
- `trigger_patterns` (list of phrase strings the Haiku triage detects)
- `trigger_numeric` (dict of computed thresholds)
- `cadence` (e.g. "2 articles within 14 days")
- `stitching` (how multiple sources combine)

To sharpen a trigger after observing false positives/negatives:
1. Edit the indicator's `trigger_patterns` list (add or remove phrases)
2. Adjust `cadence` if needed
3. Update the `trigger_numeric` thresholds
4. The Layer 1 digester prompt picks up the changes automatically on next run (TRIGGER_REFERENCE_BLOCK is rebuilt at import time)

For Indicator 3 broker tiers, edit `BROKER_AUTHORITY_TIERS` in `config.py` — same dataclass-driven flow.

## Repo file layout

```
.
├── .github/workflows/
│   ├── deep_tier.yml      # 08:00 UTC daily
│   └── fast_tier.yml      # weekday 11:00/11:30/12:05 UTC
├── src/
│   ├── config.py
│   ├── state.py
│   ├── deep_tier.py
│   ├── fast_tier.py
│   ├── main.py
│   ├── diagnostics.py
│   ├── data/
│   ├── analysis/
│   └── email_module/
├── state/                 # auto-managed; commits via workflows
├── requirements.txt
├── .env.example
├── README.md              # overview
├── SETUP.md               # this file
└── DEPLOYMENT_GUIDE.md    # non-developer walkthrough
```
