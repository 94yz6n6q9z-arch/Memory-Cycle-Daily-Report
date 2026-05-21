# v3.4.0 — Decode HBM Captive Ratio (DHCR) indicator

The feature release. Single new Tier 1 indicator collapsing the ROADMAP's original three captive-ratio indicators into one composite direction monitor.

## ⚠️ One file removal required after upload

```bash
git rm memory_cycle_data_source_audit_v3_39_7_10.xlsx
```

(Plus any older versioned xlsx files still in the repo from prior uploads.)

---

## What ships

**New indicator:** `t1_decode_hbm_captive_ratio` (DHCR) — Tier 1, direction monitor (no trigger gate). Indicator count 22 → 23.

**The mental model** (Raul, May 21 2026):

> Efficiency gains don't displace HBM — they UNLOCK economic context capacity. Enterprise workloads expand to consume what's economically possible. The binding question is whether enterprise demand grows fast enough to fill the unlocked headroom.

**Today's read:** STRONGLY_BULLISH_HBM, +46pp delta. Frontier KV 14 KB/tok (DeepSeek V4), frontier HBM 576 GB (NVIDIA GB300), implied context capacity ~**42 million tokens per accelerator** (vs ~256K at Q4 2023 — 165× expansion in 30 months). Enterprise AI growing 216% YoY vs capacity creation 111% YoY.

---

## File delta from v3.39.7.10

**NEW files (6):**
- `data/inference_arch_reference.yaml` — 13 open-architecture frontier models + 3 closed models + workload context estimates
- `data/hbm_per_accelerator.yaml` — NVIDIA (9 gens), AMD (4 gens), custom silicon (6 gens), non-HBM substitution risk (3)
- `data/enterprise_ai_revenue.yaml` — MSFT/AWS/GCP/Oracle/NVIDIA quarterly + hyperscaler capex (bootstrapped Q1 2024 → Q1 2026)
- `src/analysis/dhcr.py` — compute module (~800 lines)
- `tests/test_v3_4_0_dhcr.py` — 55 tests across 17 test classes
- `memory_cycle_data_source_audit_v3_4_0.xlsx` — version rename

**MODIFIED files (10):**
- `src/config.py` — DHCR registered (Tier 1, no trigger patterns)
- `state/indicator_evidence.json` — DHCR bootstrap entry
- `src/state.py` — `save_dhcr_state` + `load_dhcr_prior_state` (12-snapshot history cap)
- `src/email_module/template.py` — `_build_dhcr_section3_snippet` + `_build_dhcr_section4_card` + `build_email_html` signature +2 kwargs
- `src/analysis/chart_generator.py` — `generate_dhcr_trajectory_chart` (dual-panel: capacity-vs-demand growth + implied capacity log-scale)
- `src/analysis/llm_summarizer.py` — DHCR block in COMPUTED METRICS prompt
- `src/fast_tier.py` — DHCR computed + persisted + threaded into build_email_html
- `src/analysis/digester.py` — 5 new extraction fields for enterprise context (Phase 2)
- `.github/workflows/fast_tier.yml` — commits state/extraction_log_*.jsonl + state/dhcr_state.json (Phase 2 persistence fix)
- `.github/workflows/deep_tier.yml` — commits state/extraction_log_*.jsonl (Phase 2)
- `tests/test_indicator_evidence.py` — count 22 → 23
- `CHANGES.md` / `CLAUDE.md` / `ROADMAP.md` / `UPLOAD_GUIDE.md`

**REMOVED:** `memory_cycle_data_source_audit_v3_39_7_10.xlsx` (replaced by v3.4.0 rename)

---

## Phase 2 persistence fix

The May 21 audit revealed `state/extraction_log_*.jsonl` was never appearing in the committed state directory — the ROADMAP's premise of "3 weeks of accumulated extraction data ready to calibrate" was empirically false. Two causes:

1. **Workflow gap (now fixed):** the deep tier digester wrote `state/extraction_log_{date}.jsonl` per the v3.3 spec but neither workflow file added it to git. Every runner started with a fresh checkout, file was generated locally, then destroyed at shutdown.
2. **Sparse triggers (accepted):** the digester only writes when at least one architecture signal field is non-null (CXL deployment, LPDDR datacenter win, custom silicon arch, etc.) — so empty-architecture quarters wouldn't produce a file even if persistence worked. This is correct behavior; accepted as is.

v3.4.0 fixes (1). From the first deploy forward, every deep-tier run that detects architecture signals adds a daily JSONL line to the repo, accumulating across runs.

---

## Test state

```bash
PYTHONPATH=. python3 -m pytest tests/ -q
# 380 passed, 87 warnings, 3 subtests passed in 4.98s
```

325 from v3.39.7.10 baseline + 55 new in `tests/test_v3_4_0_dhcr.py`.

The 55 v3.4.0 tests cover (17 classes):
- **YAML structural integrity** (6): all three reference files parse + carry required keys
- **KV/token math** (4): GQA + MLA formulas, FP16/FP8 quantization, YAML pre-computed values trusted
- **Frontier KV computation** (3): date-aware selection, monotonic ratchet, future models excluded
- **Frontier HBM** (3): merchant-only (NVIDIA/AMD), projected entries excluded by default, monotonic growth
- **Implied context capacity** (3): formula verification + edge cases
- **Capacity creation YoY composite** (2): correct combination, None handling
- **Enterprise demand aggregation** (2): run-rate sum + weighted YoY
- **Direction signal** (4): each 5-level band + insufficient-data fallback
- **Trajectory computation** (3): multi-quarter, chronological, capacity expansion
- **Full snapshot** (4): integration, all fields present, narrative non-empty, substitution flags
- **State persistence** (2): round-trip + 12-entry history cap
- **Rendering** (5): section 3 snippet + section 4 card + None handling + direction colors
- **Chart generation** (3): full trajectory + empty + single-point edge cases
- **Config registration** (4): in INDICATORS, Tier 1, count = 23, evidence store entry
- **Sonnet prompt integration** (2): DHCR block present + mental model explainer
- **Workflow persistence** (3): extraction_log + dhcr_state in both workflow files
- **Build email html end-to-end** (2): with/without DHCR snapshot

---

## Upload workflow

**github.dev:**
1. Extract this ZIP locally
2. Open `https://github.dev/<your-username>/Memory-Cycle-Daily-Report`
3. Delete `memory_cycle_data_source_audit_v3_39_7_10.xlsx`
4. Drag-and-drop the extracted folder
5. Source Control panel → stage all → commit `v3.4.0 — Decode HBM Captive Ratio (DHCR) indicator + Phase 2 extraction_log persistence fix` → push
6. Delete `UPLOAD_GUIDE.md` from the repo

**Local git:**
```bash
git rm memory_cycle_data_source_audit_v3_39_7_10.xlsx
# extract zip into repo (overwrites modified files + adds 6 new files)
git add . && git commit -m "v3.4.0 — DHCR indicator + Phase 2 extraction_log persistence" && git push
```

---

## Post-deploy acceptance criteria

**First fast-tier run after upload:**
- [ ] Pipeline completes without errors (DHCR added; no regression to existing indicators)
- [ ] Log line `DHCR computed: STRONGLY_BULLISH_HBM (KV=14KB, HBM=576GB, capacity=42,130,285t, ent_yoy=216%, cap_create_yoy=111%)`
- [ ] `state/dhcr_state.json` appears in the repo after the workflow commit step. **If absent → the `git add state/dhcr_state.json` in fast_tier.yml didn't take. Open issue.**

**First brief after upload:**
- [ ] Section 3 indicator table includes a row for `t1_decode_hbm_captive_ratio` with the direction badge ("STRONGLY BULLISH HBM" today) and driver numbers ("Frontier KV: 14 KB/tok · HBM/accel: 576 GB · Implied capacity: ~42.1M tokens · Capacity-creation YoY: +111% · Enterprise AI YoY: +216% · NVIDIA DC YoY: +69%")
- [ ] Section 4 deep-dive card renders with: supply/demand 2-column layout · dual-panel trajectory chart embedded · Sonnet narrative paragraph · substitution-risk flags (Cerebras WSE-3, Groq, Etched) · mental-model footer
- [ ] Sonnet's exec_summary and indicator_assessments DO NOT describe DHCR with fire-language ("Fired", "Partially fired", "Reversed"). Status pill always shows "Not fired". If Sonnet describes it as fired, the prompt mental-model explainer needs strengthening — open issue.

**First deep-tier run after upload:**
- [ ] If any digested transcript mentions CXL/LPDDR/custom silicon/MoE/INT4 quantization/enterprise agent context, `state/extraction_log_2026-MM-DD.jsonl` should appear in the repo
- [ ] If all transcripts are silent on architecture, no file is created — this is correct (digester only writes when signals non-null)

**Visual quality checks:**
- [ ] Trajectory chart's bottom panel (implied capacity, log scale) shows the ~165× expansion from Q4 2023 (256K tokens) to Q2 2026 (42M tokens) — this is the visual heart of the bull case
- [ ] Top panel shows enterprise revenue line (green) above capacity-creation line (gray) — the bullish shading visible

---

## What v3.4.0 does NOT do

- **No trigger gate.** DHCR ships as direction monitor only. Status pill is always "Not fired." Trigger activation is v3.4.1 work after 4-6 quarters of shadow data validates band boundaries.
- **No LPDDR-specific pricing.** Still tracking DRAM + HBM. LPDDR contract scrape is an open gap from the original ROADMAP, queued for v3.4.2+.
- **No closed-model KV auto-extraction.** GPT-5/Claude 4/Gemini 2.5 KV is hand-estimated with explicit confidence bands. Auto-extraction from cost-per-token gradients is v3.4.2+.
- **No changes to the other 22 indicators.** All v3.39.x fixes preserved as-is.

---

## Roadmap forward

- **v3.4.1** (~Q1 2027): activate trigger gates. Conditions: direction → mildly_bearish_hbm for 2 consecutive quarters (partial fire), → strongly_bearish_hbm for 2 consecutive quarters (full fire), → mildly_bullish_hbm+ for 2 quarters (reversal).
- **v3.4.2+**: closed-model KV auto-extraction from API cost gradients; LPDDR-specific TrendForce scrape; Artificial Analysis tokens-$ trend integration.

## Key insights from the May 21 design session

Worth preserving so future Claude doesn't re-litigate these:

1. **Efficiency does not displace HBM.** It UNLOCKS context capacity. Market always fills the economic frontier.
2. **Don't try to measure aggregate context length directly.** It's unknowable and a lagging artifact. Measure enterprise AI revenue growth as the leading proxy for demand-pull into the unlocked headroom.
3. **DHCR is a direction monitor, not a trigger.** The signal is "is demand growing faster or slower than capacity creation," not "did we cross some threshold."
4. **The 42M implied context capacity per accelerator** is the bull case in concentrated form. Efficiency × HBM creates the headroom; we watch enterprise adoption fill it.
5. **The original ROADMAP three-indicator design was wrong.** Indicators 23/24/25 measured three INPUTS to one underlying question. Collapsing to ONE composite direction monitor is correct.
