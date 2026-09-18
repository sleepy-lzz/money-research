from __future__ import annotations

import copy
import json

import pytest

from ashare_agent.session_calendar import calendar_manifest, next_sessions, sessions


def test_calendar_manifest_is_json_compatible_and_independent() -> None:
    first = calendar_manifest()
    json.dumps(first, ensure_ascii=False)
    first["rules"]["closed_periods"][0]["start"] = "2025-01-02"
    first["sources"][0]["url"] = "https://example.invalid/changed"

    second = calendar_manifest()
    assert second["rules"]["closed_periods"][0]["start"] == "2025-01-01"
    assert second["sources"][0]["url"].startswith("https://www.sse.com.cn/")


def test_known_holidays_and_weekends_are_closed_without_makeup_sessions() -> None:
    result = sessions("2025-01-25", "2025-02-10")
    assert "2025-01-26" not in result
    assert "2025-01-28" not in result
    assert "2025-02-04" not in result
    assert "2025-02-08" not in result
    assert result == ["2025-01-27", "2025-02-05", "2025-02-06", "2025-02-07", "2025-02-10"]

    assert "2026-09-25" not in sessions("2026-09-24", "2026-09-29")
    assert "2026-09-26" not in sessions("2026-09-24", "2026-09-29")
    assert "2026-09-28" in sessions("2026-09-24", "2026-09-29")
    assert "2026-10-01" not in sessions("2026-09-30", "2026-10-09")
    assert "2026-10-07" not in sessions("2026-09-30", "2026-10-09")
    assert "2026-10-08" in sessions("2026-09-30", "2026-10-09")


def test_missing_year_and_invalid_ranges_are_rejected() -> None:
    with pytest.raises(ValueError):
        sessions("2024-12-31", "2025-01-02")
    with pytest.raises(ValueError):
        sessions("2025-01-03", "2027-01-01")
    with pytest.raises(ValueError):
        sessions("2025-02-01", "2025-01-01")


def test_invalid_manifest_is_rejected() -> None:
    invalid = calendar_manifest()
    del invalid["rules"]
    with pytest.raises(ValueError):
        sessions("2025-01-01", "2025-01-02", invalid)

    invalid = calendar_manifest()
    invalid["rules"]["closed_periods"][0]["end"] = "2024-12-31"
    with pytest.raises(ValueError):
        sessions("2025-01-01", "2025-01-02", invalid)

    invalid = calendar_manifest()
    invalid["sources"][0]["fetched_at"] = "2026-09-11T01:18:59"
    with pytest.raises(ValueError):
        sessions("2025-01-01", "2025-01-02", invalid)

    invalid = calendar_manifest()
    invalid["sources"][0]["download_status"] = "complete"
    invalid["sources"][0]["content_hash"] = None
    with pytest.raises(ValueError):
        sessions("2025-01-01", "2025-01-02", invalid)


def test_frozen_manifest_revision_and_rule_changes_are_used() -> None:
    frozen = calendar_manifest()
    frozen["revision"] = "local-review-r2"
    frozen["rules"]["closed_periods"].append(
        {"start": "2025-03-03", "end": "2025-03-03", "reason": "review-only closure"}
    )
    assert "2025-03-03" not in sessions("2025-03-03", "2025-03-03", frozen)
    assert "2025-03-03" in sessions("2025-03-03", "2025-03-03")

    frozen_again = copy.deepcopy(frozen)
    assert next_sessions("2025-03-03", 2, frozen_again) == ["2025-03-04", "2025-03-05"]


def test_next_sessions_is_strict_and_fails_past_coverage() -> None:
    assert next_sessions("2025-01-01", 3) == ["2025-01-02", "2025-01-03", "2025-01-06"]
    assert next_sessions("2025-01-01", 0) == []
    with pytest.raises(ValueError):
        next_sessions("2026-12-31", 1)
    with pytest.raises(ValueError):
        next_sessions("2025-01-01", -1)
