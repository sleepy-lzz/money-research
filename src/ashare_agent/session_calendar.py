"""Independent, auditable SSE/SZSE A-share session calendar.

The default calendar is a deliberately finite bundle of published exchange
closures.  It does not infer sessions from prices or benchmark rows.  A
caller can pass a frozen JSON manifest to every public function; that
manifest is validated and used as supplied for the duration of the call.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import PurePosixPath
from typing import Any, Mapping

_SCHEMA = "ashare-session-calendar/v1"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _load_default_manifest() -> dict[str, Any]:
    try:
        resource = resources.files("ashare_agent.calendars").joinpath(
            "session_calendar_manifest.json"
        )
        manifest = json.loads(resource.read_text(encoding="utf-8"))
        manifest = _validate_manifest(manifest)
        _verify_default_receipts(manifest)
        return manifest
    except (FileNotFoundError, ModuleNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError("bundled session-calendar manifest is unavailable or invalid") from exc


def _iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError(f"{field} must be an ISO date YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must use canonical ISO date form")
    return parsed


def _timestamp(value: Any, field: str) -> None:
    """Validate a fetched timestamp without manufacturing one for a receipt."""

    if value is None:
        return
    if not isinstance(value, str) or "T" not in value or not value:
        raise ValueError(f"{field} must be null or an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be null or an ISO timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")


def _check_hash(value: Any, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not _HEX64_RE.fullmatch(value)):
        raise ValueError(f"{field} must be null or a SHA-256 hex digest")


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("manifest must be a JSON object")

    # Deep-copy before validating and using caller-owned input.  This prevents
    # a concurrent caller from changing the closure set during enumeration.
    manifest = copy.deepcopy(dict(value))
    required = ("schema", "revision", "covered_start", "covered_end", "sources", "rules")
    missing = [field for field in required if field not in manifest]
    if missing:
        raise ValueError(f"manifest missing required fields: {', '.join(missing)}")
    if manifest["schema"] != _SCHEMA:
        raise ValueError(f"unsupported manifest schema: {manifest['schema']!r}")
    if not isinstance(manifest["revision"], str) or not manifest["revision"].strip():
        raise ValueError("manifest revision must be a non-empty string")

    covered_start = _iso_date(manifest["covered_start"], "covered_start")
    covered_end = _iso_date(manifest["covered_end"], "covered_end")
    if covered_start > covered_end:
        raise ValueError("covered_start must not be after covered_end")

    sources = manifest["sources"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("manifest sources must be a non-empty list")
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            raise ValueError(f"sources[{index}] must be an object")
        if not isinstance(source.get("url"), str) or not source["url"].strip():
            raise ValueError(f"sources[{index}].url must be a non-empty string")
        _iso_date(source.get("published_at"), f"sources[{index}].published_at")
        if source.get("published_precision") not in {"day", "date"}:
            raise ValueError(f"sources[{index}].published_precision must be day/date")
        if not isinstance(source.get("coverage_year"), int) or isinstance(
            source.get("coverage_year"), bool
        ):
            raise ValueError(f"sources[{index}].coverage_year must be an integer")
        _timestamp(source.get("fetched_at"), f"sources[{index}].fetched_at")
        _check_hash(source.get("content_hash"), f"sources[{index}].content_hash")
        if source.get("download_status") == "complete":
            if source.get("fetched_at") is None:
                raise ValueError(f"sources[{index}] cannot be complete without fetched_at")
            if source.get("content_hash") is None:
                raise ValueError(f"sources[{index}] cannot be complete without content_hash")
            if not isinstance(source.get("raw_html_path"), str) or not source["raw_html_path"].strip():
                raise ValueError(f"sources[{index}] cannot be complete without raw_html_path")

    covered_years = set(range(covered_start.year, covered_end.year + 1))
    source_years = {source["coverage_year"] for source in sources}
    if source_years != covered_years:
        raise ValueError("source coverage years must exactly match manifest coverage years")
    for year in covered_years:
        venues = {source.get("venue") for source in sources if source["coverage_year"] == year}
        if not {"SSE", "SZSE"}.issubset(venues):
            raise ValueError(f"sources for {year} must include both SSE and SZSE")

    rules = manifest["rules"]
    if not isinstance(rules, Mapping):
        raise ValueError("manifest rules must be an object")
    if rules.get("weekends_closed") is not True:
        raise ValueError("manifest must explicitly declare weekends_closed=true")
    if rules.get("no_makeup_open_sessions") is not True:
        raise ValueError("manifest must explicitly declare no_makeup_open_sessions=true")
    periods = rules.get("closed_periods")
    if not isinstance(periods, list):
        raise ValueError("manifest rules.closed_periods must be a list")

    normalized: list[tuple[date, date]] = []
    for index, period in enumerate(periods):
        if not isinstance(period, Mapping):
            raise ValueError(f"closed_periods[{index}] must be an object")
        period_start = _iso_date(period.get("start"), f"closed_periods[{index}].start")
        period_end = _iso_date(period.get("end"), f"closed_periods[{index}].end")
        if period_start > period_end:
            raise ValueError(f"closed_periods[{index}] start must not be after end")
        if period_start < covered_start or period_end > covered_end:
            raise ValueError(f"closed_periods[{index}] falls outside manifest coverage")
        normalized.append((period_start, period_end))

    normalized.sort()
    for previous, current in zip(normalized, normalized[1:]):
        if current[0] <= previous[1]:
            raise ValueError("manifest closed periods must not overlap")
    return manifest


def _verify_default_receipts(manifest: Mapping[str, Any]) -> None:
    """Verify bundled raw HTML hashes and the duplicated source receipt."""

    package_root = resources.files("ashare_agent")
    receipt_resource = package_root.joinpath("calendars", "source_receipts.json")
    try:
        receipt_manifest = json.loads(receipt_resource.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError("bundled source receipt is unavailable or invalid") from exc
    receipts = receipt_manifest.get("receipts")
    if receipt_manifest.get("status") != "complete" or not isinstance(receipts, list):
        raise ValueError("bundled source receipt is not complete")
    by_url = {item.get("url"): item for item in receipts if isinstance(item, Mapping)}

    for source in manifest["sources"]:
        raw_path = source.get("raw_html_path")
        if not isinstance(raw_path, str):
            raise ValueError("complete source is missing raw_html_path")
        relative = PurePosixPath(raw_path)
        if (
            len(relative.parts) != 3
            or relative.parts[0] != "calendars"
            or relative.parts[1] != "raw"
            or relative.parts[2] in {"", ".", ".."}
        ):
            raise ValueError(f"unsafe raw_html_path: {raw_path!r}")
        raw_resource = package_root.joinpath("calendars", "raw", relative.parts[2])
        try:
            raw = raw_resource.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError(f"bundled raw source missing: {raw_path}") from exc
        digest = hashlib.sha256(raw).hexdigest()
        if digest != source.get("content_hash"):
            raise ValueError(f"bundled raw source hash mismatch: {raw_path}")
        receipt = by_url.get(source.get("url"))
        if receipt is None:
            raise ValueError(f"source receipt missing URL: {source.get('url')}")
        for field in (
            "venue",
            "coverage_year",
            "published_at",
            "published_precision",
            "fetched_at",
            "content_hash",
            "raw_html_path",
            "status",
        ):
            if receipt.get(field) != source.get(field) and not (
                field == "status" and receipt.get(field) == "complete" and source.get("download_status") == "complete"
            ):
                raise ValueError(f"source manifest and receipt differ for {source.get('url')}: {field}")


def _resolve_manifest(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    return _validate_manifest(_load_default_manifest() if manifest is None else manifest)


def calendar_manifest() -> dict[str, Any]:
    """Return a JSON-compatible independent copy of the bundled manifest.

    The returned object can be edited by a caller without changing subsequent
    calls.  Every bundled source has an actual timezone-aware ``fetched_at``
    and a SHA-256 hash checked against its packaged raw HTML receipt.
    """

    return copy.deepcopy(_validate_manifest(_load_default_manifest()))


def sessions(
    start: str,
    end: str,
    manifest: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return verified open session dates in the inclusive ISO date range.

    Dates outside the finite manifest coverage are rejected.  Every weekday
    outside an explicit closure is an open session because the published rules
    say that no weekend is made up with an open session.
    """

    frozen = _resolve_manifest(manifest)
    start_date = _iso_date(start, "start")
    end_date = _iso_date(end, "end")
    covered_start = _iso_date(frozen["covered_start"], "covered_start")
    covered_end = _iso_date(frozen["covered_end"], "covered_end")
    if start_date > end_date:
        raise ValueError("start must not be after end")
    if start_date < covered_start or end_date > covered_end:
        raise ValueError("requested date range is outside manifest coverage")

    periods = [
        (
            _iso_date(item["start"], "closed_period.start"),
            _iso_date(item["end"], "closed_period.end"),
        )
        for item in frozen["rules"]["closed_periods"]
    ]
    result: list[str] = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5 and not any(begin <= current <= finish for begin, finish in periods):
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def next_sessions(
    signal: str,
    count: int,
    manifest: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return ``count`` verified sessions strictly after ``signal``."""

    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("count must be a non-negative integer")
    frozen = _resolve_manifest(manifest)
    signal_date = _iso_date(signal, "signal")
    covered_start = _iso_date(frozen["covered_start"], "covered_start")
    covered_end = _iso_date(frozen["covered_end"], "covered_end")
    if signal_date < covered_start or signal_date > covered_end:
        raise ValueError("signal is outside manifest coverage")
    if count == 0:
        return []
    if signal_date >= covered_end:
        raise ValueError("requested next sessions exceed manifest coverage")

    candidates = sessions((signal_date + timedelta(days=1)).isoformat(), covered_end.isoformat(), frozen)
    if len(candidates) < count:
        raise ValueError("requested next sessions exceed manifest coverage")
    return candidates[:count]


__all__ = ["calendar_manifest", "sessions", "next_sessions"]
