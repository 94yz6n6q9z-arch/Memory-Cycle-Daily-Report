# Triangulation methodology (v3.5.4, 2026-05-24)

This document encodes the lesson from the v3.4.14 / v3.5.3 failure cycle so it
isn't repeated. It is written for future Claude sessions and for Raul as a
durable reference for how this framework should treat data sourcing.

## The failure

In v3.4.14, the context-length scraper was designed to fetch published context
windows from provider documentation pages (OpenAI, Anthropic, Google, Cursor,
Claude Code, etc.). The market-share weights for combining those values within
a category were written as hardcoded constants based on intuition rather than
public data: `ChatGPT 60% / Claude 20% / Gemini 15% / Others 5%`.

Both decisions were wrong in the same way:

1. **The scraper assumed the data was scrapeable.** Most provider doc pages
   are Cloudflare-protected SPAs that return 403 to generic user agents, or
   JavaScript-rendered pages where the relevant numbers don't appear in the
   initial HTML. When the first production run executed, every URL-based
   provider failed. The "result" was a weighted average of hardcoded fallback
   constants, labelled `data_extracted` in the diagnostic. The framework
   accepted bogus values as real measurements.

2. **The hardcoded weights were wrong by an order of magnitude in places.**
   Actual April 2026 web-traffic share is ChatGPT 56.7%, Gemini 25.5%,
   Copilot 14.4%, Claude 6%. Microsoft Copilot was absent from the
   framework entirely. Claude was overweighted 4x. The real share data was
   the first result of a one-minute Google search for
   "ChatGPT market share 2026" — and the same was true for every other
   weight we needed.

Both failures had the same root cause: **building automation as a substitute
for research, instead of doing the research first and automating after.**

## The principle

> Approximately right with public proxy data is better than elaborately wrong
> from broken scraping.

When the framework needs a quantitative input it doesn't compute itself, the
order of operations is:

1. **Look for a publicly disclosed proxy first.** Earnings calls, investor
   relations decks, SimilarWeb / Sacra reports, vendor blog posts, regulatory
   filings, MAU/WAU disclosures. These exist for almost everything the
   framework cares about, and they don't require scraping.

2. **Document the proxy and its source** directly inside the data file
   (state YAML / state JSON). Every value carries a `source_urls` field and
   a `rationale` paragraph. Future auditors (including future Claude
   sessions) can verify the value is current.

3. **Set a manual review cadence** with a calendar date, not an automated
   refresh. Quarterly review for fast-moving inputs, annually for slow ones.
   Calendar-gated review is far more reliable than "the scraper will catch
   any change" because the scraper can silently fail.

4. **Only then consider automation** — and only for inputs where the source
   is structurally machine-readable (a JSON API, a sitemap of stable URLs,
   a regulatory filing format). For inputs where the source is
   unstructured (HTML pages that change weekly, paywalled reports,
   judgement-based estimates), automation is a trap.

5. **When automation does run, it must distinguish "I got real data" from
   "I fell back to defaults".** Any aggregator/scraper that can return a
   value derived from hardcoded fallbacks must label that fact in the
   diagnostic. The v3.5.1/v3.5.3 fixes added an `any_real_scrape` flag
   for exactly this purpose.

## Concrete inputs in this framework and their canonical sources

| Input | Type | Canonical proxy | Refresh cadence |
|---|---|---|---|
| `typical_context_tokens` per category | Quasi-static spec | Vendor docs + Anthropic/OpenAI/Google quarterly product announcements | Quarterly review |
| `volume_share_pct` per category | Slow-moving estimate | Revenue mix disclosures (Anthropic 80% enterprise, OpenAI 60% consumer) + web traffic share (SimilarWeb / Sacra) + earnings transcripts of hyperscalers | Quarterly review, with daily aggregator catching deltas |
| Within-category provider weights (e.g. ChatGPT vs Gemini vs Claude in consumer_chat) | Slow-moving market share | Web traffic share (SimilarWeb monthly reports), MAU/WAU disclosures from earnings calls | Quarterly review |
| Anthropic / OpenAI revenue mix | Disclosed | Quarterly investor disclosures, Reuters/Bloomberg reporting | Quarterly |
| Claude Code / Cursor / Copilot ARR | Disclosed | Time Magazine, company announcements, Sacra | Quarterly |
| Cross-category absolute token volumes | Modeled | Revenue × inverse-of-average-price for pay-per-token segments; WAU × prompts/week × tokens/prompt for consumer | Quarterly |

## Anti-patterns to avoid

These were committed during v3.4.14 development and should be treated as
red flags going forward:

1. **"We'll scrape product doc pages."** Provider documentation is the
   worst possible source: Cloudflare-protected, JavaScript-rendered,
   restructured frequently, no stable selectors. Use the vendor's API
   docs (which are structured for machine consumption) or their JSON
   endpoints if they have them. Otherwise, manual quarterly review.

2. **"Use a hardcoded fallback if the scraper fails."** Static fallbacks
   are fine, but they MUST be flagged as fallbacks in any diagnostic.
   The v3.5.1 fix added the `any_data` gate; v3.5.3 added `any_real_scrape`.
   Static providers (URL=None) should never claim `data_extracted` status.

3. **"This is what I think the market share is."** If you're writing a
   weight without citing a source URL in a comment or the data file,
   stop and search for the public proxy first. The data exists.

4. **"The aggregator will catch any change in the underlying market."**
   Aggregators that read earnings transcripts catch _signals_ about
   change, not the baseline. The baseline must be set from public proxies
   at the time the framework starts running.

## Recalibration log

| Date | What was recalibrated | Trigger |
|---|---|---|
| 2026-05-24 | Full `state/context_length_state.json` rewrite: typical_context_tokens × 5 categories, volume_share_pct × 5 categories, operative_context_history reset | v3.5.3 audit found the entire framework was running on values set from intuition rather than public proxies |

Next review due: 2026-08-24 (quarterly).
