# v3.4.2 — DHCR matrix redesign + reliability + CapEx capitalization

Three workstreams from the May 22 2026 production failure + design conversation.

## ⚠️ One file removal required after upload

```bash
git rm memory_cycle_data_source_audit_v3_4_1.xlsx
git rm tests/test_v3_4_0_dhcr.py
```

(The v3.4.0 DHCR tests are removed because the framework was redesigned in v3.4.2; the v3.4.2 test file covers the new framework comprehensively.)

---

## What ships in v3.4.2

| # | Workstream | Mechanism |
|---|---|---|
| A | Sonnet reliability for 529 | 3-retry app-level backoff (30s/90s/240s) + Haiku 4.5 fallback if all retries fail |
| A | Layer 2 reliability for 529 | 3-retry per-signal backoff (10s/30s/90s); DHCR explicitly skipped (computed deterministically) |
| B | DHCR full framework redesign | 5×5 matrix with memory-zone color coding, dual-input X-axis, anticipation premium, sanity check, score 0-100, bull/bear context in email |
| B | Section 4 placement | Renamed "Top 5" → "Top 6 indicators — deep dive". DHCR card rendered FIRST inside section 4. Section 3 unchanged. |
| C | CapEx capitalization | 86 replacements in user-facing strings; code identifiers preserved |

## File delta v3.4.1 → v3.4.2

**NEW (3):**
- `data/context_length_signals.yaml` — dual-input X-axis triangulation YAML
- `tests/test_v3_4_2_dhcr_matrix.py` — 36 tests across 15 classes
- `memory_cycle_data_source_audit_v3_4_2.xlsx` — version rename

**DELETED (1):**
- `tests/test_v3_4_0_dhcr.py` — obsolete (v3.4.0 framework superseded)

**MODIFIED (8):**
- `src/analysis/dhcr.py` — full rewrite for matrix framework
- `src/analysis/chart_generator.py` — added `generate_dhcr_matrix_chart`
- `src/analysis/llm_summarizer.py` — Sonnet retry + Haiku fallback + CapEx
- `src/analysis/signal_reasoner.py` — Layer 2 retry + DHCR skip
- `src/email_module/template.py` — section 4 title + DHCR-first + new card layout with bull/bear boxes + framework primer
- `src/fast_tier.py` — matrix chart generation + threading
- `src/config.py` — CapEx capitalization (59 user-facing string replacements)
- `data/enterprise_ai_revenue.yaml` — CapEx in 4 note fields
- `tests/test_v3_39_5_void_closures.py` + `tests/test_v3_39_6_calculation_driven.py` — updated CapEx assertions

**REMOVED:** `memory_cycle_data_source_audit_v3_4_1.xlsx`

## Test state

```bash
PYTHONPATH=. python3 -m pytest tests/ -q
# 385 passed, 17 warnings, 3 subtests passed in 1.74s
```

## Upload workflow

```bash
git rm memory_cycle_data_source_audit_v3_4_1.xlsx tests/test_v3_4_0_dhcr.py
# extract this zip over the repo
git add . && git commit -m "v3.4.2 — DHCR matrix redesign (5x5 with memory zones, dual-input X-axis, anticipation premium, sanity check, bull/bear context), Sonnet/Layer 2 reliability (529 retry + Haiku fallback), CapEx capitalization" && git push
```

## Post-deploy acceptance criteria

**First brief after upload — DHCR card (NEW v3.4.2 layout):**
- [ ] Section 4 title reads "**Top 6 indicators — deep dive**" (was "Top 5")
- [ ] DHCR card is the FIRST card in section 4 (above the 5 Sonnet-picked deep dives)
- [ ] Section 3 (Tier indicators table) UNCHANGED — DHCR row stays where it was
- [ ] DHCR card shows:
  - Header: "Decode HBM Captive Ratio (DHCR)" + green "Not fired" pill + blue "SCORE 52/100" badge
  - 5×5 matrix chart: cells color-coded green (DDR) → yellow (LPDDR) → amber (hybrid) → red-orange (HBM); black outline circle at R3C2 (workload) connected by dashed line to red filled circle at R4C2 (market-deployed); legend at bottom
  - Two-column workload-vs-market readout with KV details + operative context + anticipation gap
  - Per-category context contribution table (5 rows: consumer chat / coding agents / enterprise vertical agents / dev raw API / other) with context, share, contribution, confidence
  - Volume layer block: aggregate run-rate, weighted YoY, NVIDIA DC YoY, volume index, trajectory, HBM bit demand
  - Sanity check status: "3/3 signals confirm matrix output" (green, no calibration flag today)
  - Sonnet narrative paragraph
  - 🐂 **BULL CASE** green box with enterprise-share growth scenario quantified
  - 🐻 **BEAR CASE** red box with stall-scenario consequences
  - Framework primer footer + "Why this matters" explainer
- [ ] All "CapEx" in user-facing text (descriptions, prompts, labels) — search the rendered HTML for "capex" or "Capex" as standalone words; should find ZERO

**If Anthropic API has another 529 storm:**
- [ ] Sonnet retries silently up to ~6min; logs show retry attempts with backoff
- [ ] If all retries fail, Haiku fallback kicks in; brief still ships with DHCR matrix
- [ ] Layer 2 per-signal retries logged when 529 hit
- [ ] DHCR card always populated regardless of LLM availability (it's deterministically computed)

## Monthly DHCR refresh process (NEW v3.4.2)

`data/context_length_signals.yaml` needs human-curated update each month:

1. **Review product documentation** for each category's flagship products:
   - Consumer chat: ChatGPT/Claude/Gemini default context windows
   - Coding agents: Cursor/Devin/Claude Code/Copilot Workspace
   - Enterprise vertical: Hebbia/Harvey/MS Copilot Enterprise case studies
   - Dev API: typical default per provider
   - Other: Perplexity, embedded chat, RAG wrappers
2. **Review hyperscaler earnings transcripts** (already digested) for volume share signals
3. **Update** `typical_context_tokens` and `volume_share_pct` per category
4. **Append** a `refresh_evidence` entry with date + notes
5. **Verify** `volume_share_pct` sums to 100; rebalance if needed

The framework is most sensitive to the enterprise vertical category — pay closest attention there. Bootstrap value is 800K context × 5% share; Raul's thesis is this will grow to 25-30% share × 3-5M context over 24 months.

## What v3.4.2 does NOT change

- Section 3 tier indicators table — DHCR row stays where it is
- All other 22 indicator definitions
- Cycle matrix (v3.39.7)
- v3.4.1 fixes (trigger banner colors, change-flag plain text, Reversed eradication, exec brief 250-word cap, where_we_stand 120-word cap + Layer 2 fallback)
- 4-tier value-chain TRACKED_COMPANIES
- v3.4.0 evidence store persistence + extraction_log JSONL
