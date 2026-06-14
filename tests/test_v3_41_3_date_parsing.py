"""v3.41.3 — tests for the robust feed date parser (_entry_published).

Root cause of the Samsung-strike-RESOLUTION miss: feedparser only populates
published_parsed/updated_parsed for date formats it recognizes. Korea Herald
(which carries the Samsung labor news) and some Google News locales emit formats
it skips, leaving items UNDATED → sorted to datetime.min in fetch_all_news →
starved by the recency sort + the triage cap, so the resolving (newest) item
never sorted above the older 'strike nears' items even after the queries were
added (v3.39.7.5). The fix falls back to parsing the raw date string (RFC822
then ISO8601), stdlib only.
"""
from datetime import datetime, timezone

from src.data.news import _entry_published, _parse_date_string


class TestParseDateString:
    def test_rfc822_with_offset_converts_to_utc(self):
        # standard RSS <pubDate>, Korea +0900 → 00:30 UTC
        dt = _parse_date_string("Tue, 20 May 2026 09:30:00 +0900")
        assert dt == datetime(2026, 5, 20, 0, 30, tzinfo=timezone.utc)

    def test_rfc822_gmt(self):
        dt = _parse_date_string("Tue, 20 May 2026 09:30:00 +0000")
        assert dt == datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)

    def test_iso8601_with_offset(self):
        dt = _parse_date_string("2026-05-20T09:30:00+09:00")
        assert dt == datetime(2026, 5, 20, 0, 30, tzinfo=timezone.utc)

    def test_iso8601_z_suffix(self):
        dt = _parse_date_string("2026-05-20T09:30:00Z")
        assert dt == datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)

    def test_iso_date_only_assumed_utc(self):
        dt = _parse_date_string("2026-05-20")
        assert dt == datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)

    def test_naive_datetime_assumed_utc(self):
        dt = _parse_date_string("2026-05-20T09:30:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 9

    def test_garbage_returns_none(self):
        assert _parse_date_string("not a date at all") is None

    def test_empty_and_none_return_none(self):
        assert _parse_date_string("") is None
        assert _parse_date_string("   ") is None
        assert _parse_date_string(None) is None


class TestEntryPublished:
    def test_struct_fast_path_unchanged(self):
        dt = _entry_published({"published_parsed": (2026, 5, 20, 9, 30, 0, 0, 0, 0)})
        assert dt == datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)

    def test_struct_takes_precedence_over_raw(self):
        # even if the raw string is garbage, the pre-parsed struct wins
        dt = _entry_published({
            "published_parsed": (2026, 5, 20, 9, 30, 0, 0, 0, 0),
            "published": "garbage",
        })
        assert dt == datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)

    def test_falls_back_to_raw_published_when_struct_absent(self):
        # the Korea Herald case: no *_parsed, but a parseable raw pubDate
        dt = _entry_published({"published": "Tue, 20 May 2026 09:30:00 +0900"})
        assert dt == datetime(2026, 5, 20, 0, 30, tzinfo=timezone.utc)

    def test_falls_back_to_updated_field(self):
        dt = _entry_published({"updated": "2026-05-20T09:30:00Z"})
        assert dt == datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)

    def test_undated_entry_still_returns_none(self):
        # genuinely dateless entry stays undated (safe — never crashes)
        assert _entry_published({"title": "x", "link": "y"}) is None
        assert _entry_published({}) is None

    def test_resolving_item_sorts_after_older_item_by_recency(self):
        # the actual bug: newest item must out-rank an older one once both dated
        older = _entry_published({"published": "Mon, 19 May 2026 08:00:00 +0900"})
        newer = _entry_published({"published": "Wed, 21 May 2026 08:00:00 +0900"})
        assert older is not None and newer is not None
        assert newer > older  # resolution (newer) now sorts to the top
