"""Data contracts and immutable snapshots for the daily A-share workflow.

The module deliberately keeps the provider boundary small.  A snapshot is the
unit consumed by the strategy and can be saved, hashed, and replayed without
contacting a provider.  ``TushareDownloader`` only creates raw cache files and
does not claim that a cache is fit for trading until the relevant historical
evidence has been supplied.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

MARKET_COLUMNS = [
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "up_limit",
    "down_limit",
    "suspended",
    "is_st",
    "state_known",
    "industry",
    "industry_known",
]
SECURITY_COLUMNS = ["ts_code", "list_date", "delist_date", "exchange", "board", "name"]
ACTION_COLUMNS = [
    "action_id",
    "ts_code",
    "record_date",
    "ex_date",
    "pay_date",
    "share_list_date",
    "cash_per_share",
    "bonus_ratio",
    "known_at",
]
BENCHMARK_COLUMNS = ["trade_date", "ts_code", "close"]
_DATE_COLUMNS = {
    "trade_date",
    "list_date",
    "delist_date",
    "record_date",
    "ex_date",
    "pay_date",
    "share_list_date",
    "known_at",
}
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SERIALIZED_FILES = {
    "market": "market.parquet",
    "securities": "securities.parquet",
    "actions": "actions.parquet",
    "benchmarks": "benchmarks.parquet",
    "calendar": "calendar.json",
}


def _valid_iso_date(value: Any) -> bool:
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _jsonable(value: Any) -> Any:
    """Convert common pandas/numpy values into deterministic JSON values."""

    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _normalise_date(value: Any, *, nullable: bool = False) -> str | None:
    if value is None or (nullable and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        value = value.date()
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None if nullable else text
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10] if "T" in text or " " in text else text


def _normalise_dates(frame: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    frame = frame.copy()
    for column in columns:
        if column in frame.columns:
            frame[column] = frame[column].map(lambda value: _normalise_date(value, nullable=True))
    return frame


def _empty_frame(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_frame_bytes(frame: pd.DataFrame) -> bytes:
    """Return stable bytes for a content identity independent of pandas dtype noise."""

    frame = frame.copy()
    # The contract treats column names and row order as part of the snapshot;
    # normalise values while retaining row order so duplicate rows remain visible.
    frame = _normalise_dates(frame, _DATE_COLUMNS)
    records = []
    for row in frame.astype(object).where(pd.notna(frame), None).to_dict(orient="records"):
        records.append({str(key): _jsonable(value) for key, value in row.items()})
    payload = {"columns": [str(column) for column in frame.columns], "records": records}
    return _canonical_json(payload)


def _content_digest(snapshot: Snapshot) -> str:
    digest = hashlib.sha256()
    digest.update(_stable_frame_bytes(snapshot.market))
    digest.update(_stable_frame_bytes(snapshot.securities))
    digest.update(_stable_frame_bytes(snapshot.actions))
    digest.update(_stable_frame_bytes(snapshot.benchmarks))
    digest.update(_canonical_json(snapshot.calendar))
    metadata = {key: value for key, value in snapshot.metadata.items() if key != "snapshot_id"}
    digest.update(_canonical_json(metadata))
    return digest.hexdigest()


def _frame_for_snapshot(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    result = frame.copy(deep=True)
    # Keep required columns first but preserve explicitly supplied optional columns.
    ordered = list(columns) + [column for column in result.columns if column not in columns]
    ordered = [column for column in ordered if column in result.columns]
    return _normalise_dates(result.loc[:, ordered], _DATE_COLUMNS)


@dataclass(frozen=True)
class Snapshot:
    """Immutable snapshot handle containing normalized tables and replay metadata.

    DataFrames are copied at construction.  They remain ordinary pandas objects
    for compatibility with the strategy worker; callers should treat them as
    values and use ``save_snapshot`` for durable immutable storage.
    """

    market: pd.DataFrame
    securities: pd.DataFrame
    calendar: list[str]
    actions: pd.DataFrame
    benchmarks: pd.DataFrame
    metadata: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "market", _frame_for_snapshot(self.market, MARKET_COLUMNS))
        object.__setattr__(self, "securities", _frame_for_snapshot(self.securities, SECURITY_COLUMNS))
        object.__setattr__(self, "actions", _frame_for_snapshot(self.actions, ACTION_COLUMNS))
        object.__setattr__(self, "benchmarks", _frame_for_snapshot(self.benchmarks, BENCHMARK_COLUMNS))
        calendar = [_normalise_date(value) for value in self.calendar]
        object.__setattr__(self, "calendar", list(calendar))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def snapshot_id(self) -> str:
        """Stable ID supplied by metadata, or a content digest when omitted."""

        value = self.metadata.get("snapshot_id")
        return str(value) if value else _content_digest(self)

    def sql(self, query: str) -> pd.DataFrame:
        """Run a read-only DuckDB query against ``market`` and related views."""

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty SQL string")
        statement = query.strip().rstrip(";").strip().lower()
        if ";" in statement:
            raise ValueError("Snapshot.sql accepts one read-only SQL statement")
        if not statement.startswith(("select", "with", "describe")):
            raise ValueError("Snapshot.sql only permits read-only SELECT/WITH/DESCRIBE statements")
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject
            raise RuntimeError("duckdb is required for Snapshot.sql") from exc

        connection = duckdb.connect(database=":memory:", config={"enable_external_access": "false"})
        try:
            connection.register("market", self.market)
            connection.register("securities", self.securities)
            connection.register("actions", self.actions)
            connection.register("benchmarks", self.benchmarks)
            connection.register("calendar", pd.DataFrame({"trade_date": self.calendar}))
            return connection.execute(query).fetchdf()
        finally:
            connection.close()


def validate_snapshot(snapshot: Snapshot) -> dict[str, Any]:
    """Validate structure and historical-status capabilities.

    Structural errors make a snapshot unusable.  Unknown historical status is a
    deliberate warning and makes the snapshot research-only (``tradable=False``)
    so a caller can still inspect prices and run data diagnostics.
    """

    errors: list[str] = []
    warnings: list[str] = []
    market = snapshot.market
    securities = snapshot.securities
    actions = snapshot.actions
    benchmarks = snapshot.benchmarks
    coverage_gap = False

    def require_columns(frame: pd.DataFrame, columns: Sequence[str], name: str) -> None:
        missing = [column for column in columns if column not in frame.columns]
        if missing:
            errors.append(f"{name} missing required columns: {', '.join(missing)}")

    require_columns(market, MARKET_COLUMNS, "market")
    require_columns(securities, SECURITY_COLUMNS, "securities")
    require_columns(actions, ACTION_COLUMNS, "actions")
    require_columns(benchmarks, BENCHMARK_COLUMNS, "benchmarks")

    calendar = list(snapshot.calendar)
    if not calendar:
        errors.append("calendar is empty")
    if any(not _valid_iso_date(value) for value in calendar):
        errors.append("calendar must contain valid ISO YYYY-MM-DD dates")
    if len(calendar) != len(set(calendar)):
        errors.append("calendar contains duplicate dates")
    if calendar != sorted(calendar):
        errors.append("calendar must be sorted ascending")

    def validate_dates(frame: pd.DataFrame, name: str) -> None:
        for column in _DATE_COLUMNS.intersection(frame.columns):
            values = frame[column].dropna().astype(str)
            bad = values[~values.map(_valid_iso_date)]
            if not bad.empty:
                errors.append(f"{name}.{column} contains invalid ISO dates")

    validate_dates(market, "market")
    validate_dates(securities, "securities")
    validate_dates(actions, "actions")
    validate_dates(benchmarks, "benchmarks")

    if all(column in market.columns for column in ("ts_code", "trade_date")):
        if market[["ts_code", "trade_date"]].duplicated().any():
            errors.append("market has duplicate ts_code/trade_date rows")
        unknown_dates = sorted(set(market["trade_date"].dropna().astype(str)) - set(calendar))
        if unknown_dates:
            errors.append(f"market contains dates outside calendar: {', '.join(unknown_dates[:5])}")

        # Missing rows remain missing rather than being called suspensions.  A
        # coverage gap makes the snapshot research-only until its cause is
        # proven by provider evidence.
        observed_dates = sorted(set(market["trade_date"].dropna().astype(str)))
        expected_dates = [value for value in calendar if observed_dates and value <= observed_dates[-1]]
        missing_active = 0
        if expected_dates and {"ts_code", "list_date", "delist_date"}.issubset(securities.columns):
            observed_pairs = set(zip(market["ts_code"].astype(str), market["trade_date"].astype(str)))
            for security in securities.itertuples(index=False):
                code = str(getattr(security, "ts_code"))
                list_date = getattr(security, "list_date", None)
                delist_date = getattr(security, "delist_date", None)
                if not list_date or not _valid_iso_date(str(list_date)):
                    continue
                for trade_date in expected_dates:
                    if str(list_date) <= trade_date and (not delist_date or trade_date < str(delist_date)):
                        if (code, trade_date) not in observed_pairs:
                            missing_active += 1
            if missing_active:
                coverage_gap = True
                warnings.append(
                    f"market is missing {missing_active} active security/session rows; reason is unknown"
                )

    if "available_at" in market.columns and not market.empty:
        parsed_available: list[pd.Timestamp] = []
        for value in market["available_at"]:
            if pd.isna(value):
                errors.append("market.available_at contains missing values")
                continue
            timestamp = pd.to_datetime(value, errors="coerce")
            if pd.isna(timestamp):
                errors.append("market.available_at contains invalid timestamps")
                continue
            if getattr(timestamp, "tzinfo", None) is None:
                errors.append("market.available_at timestamps must include a timezone")
                continue
            parsed_available.append(timestamp)
        if parsed_available and "trade_date" in market.columns:
            for trade_date, timestamp in zip(market["trade_date"], market["available_at"]):
                if pd.isna(timestamp):
                    continue
                parsed = pd.to_datetime(timestamp, errors="coerce")
                if pd.isna(parsed) or getattr(parsed, "tzinfo", None) is None:
                    continue
                if parsed.date().isoformat() < str(trade_date):
                    errors.append("market.available_at cannot precede its trade_date")
                    break

    if (
        all(column in securities.columns for column in ("ts_code",))
        and securities["ts_code"].duplicated().any()
    ):
        errors.append("securities has duplicate ts_code rows")
    if (
        all(column in market.columns for column in ("ts_code", "trade_date"))
        and "ts_code" in securities.columns
    ):
        unknown_symbols = sorted(set(market["ts_code"].dropna()) - set(securities["ts_code"].dropna()))
        if unknown_symbols:
            errors.append(f"market references unknown securities: {', '.join(map(str, unknown_symbols[:5]))}")

    for column in (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "adj_factor",
        "up_limit",
        "down_limit",
    ):
        if column in market.columns:
            values = pd.to_numeric(market[column], errors="coerce")
            required = market.get("tradeable", pd.Series(True, index=market.index)).eq(True)
            if (
                values.loc[required].isna().any()
                or (~np.isfinite(values.loc[required])).any()
                or np.isinf(values).any()
            ):
                errors.append(f"market.{column} contains missing/non-finite values")
            if column == "adj_factor" and (values <= 0).any():
                errors.append("market.adj_factor must be positive")
            if column in {"open", "high", "low", "close", "volume", "amount"} and (values < 0).any():
                errors.append(f"market.{column} contains negative values")

    for column in ("suspended", "is_st", "state_known", "industry_known"):
        if column in market.columns:
            values = market[column]
            if values.isna().any():
                errors.append(f"market.{column} contains null booleans")
            values = values.dropna()
            if not values.map(lambda value: isinstance(value, (bool, np.bool_))).all():
                errors.append(f"market.{column} must contain booleans")
    status_unknown_rows = pd.Series(False, index=market.index)
    industry_unknown_rows = pd.Series(False, index=market.index)
    if "industry" in market.columns and "industry_known" in market.columns:
        industry_unknown_rows = market["industry_known"].eq(False) | market["industry_known"].isna()
        industry_unknown_rows |= market["industry_known"].eq(True) & market["industry"].isna()
        if industry_unknown_rows.any():
            warnings.append("historical industry is unknown for some market rows")
    if "state_known" in market.columns:
        status_unknown_rows = market["state_known"].eq(False) | market["state_known"].isna()
    if "is_st" in market.columns:
        status_unknown_rows |= market["is_st"].isna()
    if status_unknown_rows.any():
        warnings.append("historical ST/risk status is unknown for some market rows")

    if {"open", "high", "low", "close", "suspended"}.issubset(market.columns) and not market.empty:
        active = market.get("tradeable", ~market["suspended"].fillna(True).astype(bool)).eq(True)
        opened = pd.to_numeric(market.loc[active, "open"], errors="coerce")
        high = pd.to_numeric(market.loc[active, "high"], errors="coerce")
        low = pd.to_numeric(market.loc[active, "low"], errors="coerce")
        close = pd.to_numeric(market.loc[active, "close"], errors="coerce")
        if ((opened <= 0) | (high <= 0) | (low <= 0) | (close <= 0)).any():
            errors.append("non-suspended market OHLC values must be positive")
        if (high < pd.concat([opened, close], axis=1).max(axis=1)).any():
            errors.append("market.high is below open or close")
        if (low > pd.concat([opened, close], axis=1).min(axis=1)).any():
            errors.append("market.low is above open or close")
        if (high < low).any():
            errors.append("market.high is below market.low")

    if "list_date" in securities.columns and "delist_date" in securities.columns:
        for row in securities.itertuples(index=False):
            list_value = getattr(row, "list_date", None)
            delist_value = getattr(row, "delist_date", None)
            if list_value and delist_value and str(list_value) >= str(delist_value):
                errors.append(f"security {getattr(row, 'ts_code', '?')} has list_date >= delist_date")

    if "trade_date" in benchmarks.columns:
        bad_benchmark_dates = set(benchmarks["trade_date"].dropna().astype(str)) - set(calendar)
        if bad_benchmark_dates:
            errors.append("benchmarks contains dates outside calendar")
        # The final calendar session may be a future execution day. Require
        # benchmark coverage through observed market data, never through itself.
        if "trade_date" in market and not market.empty:
            observed_end = str(market.trade_date.max())
            required_days = {day for day in calendar if day <= observed_end}
            if benchmarks.empty:
                coverage_gap = True
                warnings.append("benchmark is missing all required calendar sessions")
            elif "ts_code" in benchmarks:
                for code, rows in benchmarks.groupby("ts_code"):
                    missing = sorted(required_days - set(rows.trade_date.astype(str)))
                    if missing:
                        coverage_gap = True
                        warnings.append(
                            f"benchmark {code} is missing {len(missing)} required calendar sessions: "
                            + ", ".join(missing[:5])
                        )

    actions_known = True
    if not actions.empty:
        if actions["action_id"].duplicated().any():
            errors.append("actions has duplicate action_id values")
        if actions["known_at"].isna().any():
            warnings.append("some corporate actions have no known_at date")
            actions_known = False
        for field in ("cash_per_share", "bonus_ratio"):
            amounts = pd.to_numeric(actions[field], errors="coerce")
            if amounts.isna().any() or (~np.isfinite(amounts)).any() or (amounts < 0).any():
                warnings.append(f"corporate action {field} contains unknown or negative values")
                actions_known = False
        for action in actions.itertuples(index=False):
            cash_value = pd.to_numeric(pd.Series([getattr(action, "cash_per_share")]), errors="coerce").iloc[
                0
            ]
            bonus_value = pd.to_numeric(pd.Series([getattr(action, "bonus_ratio")]), errors="coerce").iloc[0]
            cash = float(cash_value) if pd.notna(cash_value) else 0.0
            bonus = float(bonus_value) if pd.notna(bonus_value) else 0.0
            if not getattr(action, "ts_code", None) or not getattr(action, "record_date", None):
                actions_known = False
            if not getattr(action, "ex_date", None):
                actions_known = False
            if cash > 0 and not getattr(action, "pay_date", None):
                actions_known = False
            if bonus > 0 and not getattr(action, "share_list_date", None):
                actions_known = False
        if not actions_known:
            warnings.append("some corporate actions lack dates or supported amounts")
    if snapshot.metadata.get("unknown_actions"):
        warnings.append("unsupported or unknown corporate actions are present")
        actions_known = False
    if snapshot.metadata.get("suspension_evidence_incomplete"):
        warnings.append("suspension event types are incomplete; suspension status is not proven")
        actions_known = False

    actions_complete = snapshot.metadata.get("actions_complete")
    if actions_complete is not True:
        warnings.append("corporate actions are not proven complete; formal trading is disabled")
    if snapshot.metadata.get("synthetic"):
        warnings.append("synthetic data: results are for software validation, not market evidence")
    if snapshot.metadata.get("point_in_time_evidence") is False:
        warnings.append("point-in-time evidence is absent for at least one historical field")

    unknown_status = bool(status_unknown_rows.any() or industry_unknown_rows.any())
    if "session_state_known" in market and not market.session_state_known.eq(True).all():
        unknown_status = True
        warnings.append("session status missing: an absent daily row is not evidence of suspension")
    if {"quote_present", "suspended"}.issubset(market):
        no_quote = ~market.quote_present.eq(True)
        if (no_quote & market.get("tradeable", pd.Series(False, index=market.index)).eq(True)).any():
            errors.append("quote-less session cannot be tradeable")
        if market.loc[no_quote, ["open", "high", "low", "close"]].notna().any().any():
            errors.append("quote-less session must not fabricate OHLC")
    if benchmarks.duplicated(["trade_date", "ts_code"]).any():
        errors.append("benchmark contains duplicate keys")
    if (
        not benchmarks.empty
        and (
            (pd.to_numeric(benchmarks.close, errors="coerce") <= 0)
            | ~np.isfinite(pd.to_numeric(benchmarks.close, errors="coerce"))
        ).any()
    ):
        errors.append("benchmark close must be positive and finite")
    from .hardening import audit_real_snapshot

    provenance_issues = audit_real_snapshot(snapshot) if not errors else []
    warnings.extend(provenance_issues)
    tradable = (
        not errors and not unknown_status and not coverage_gap and actions_complete is True and actions_known
    )
    tradable = tradable and not provenance_issues
    return {
        "errors": errors,
        "warnings": warnings,
        "tradable": tradable,
        "data_confidence": "synthetic"
        if snapshot.metadata.get("synthetic")
        else ("receipt_bound_source_attestation" if tradable else "unverified_reconstruction"),
        "source_authenticity": "external audit required; hashes do not prove source truth",
    }


def save_snapshot(snapshot: Snapshot, root: Path) -> Path:
    """Write a validated snapshot as an immutable Parquet directory.

    Existing content is never replaced.  Saving the same snapshot twice returns
    the existing directory; a path collision with different content raises.
    """

    result = validate_snapshot(snapshot)
    if result["errors"]:
        raise ValueError("invalid snapshot: " + "; ".join(result["errors"]))
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    snapshot_id = snapshot.snapshot_id
    target = root / snapshot_id
    digest = _content_digest(snapshot)

    if target.exists():
        manifest_path = target / "manifest.json"
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if existing and existing.get("content_id") == digest:
                return target
        raise FileExistsError(f"snapshot path already contains different data: {target}")

    staging = root / f".{snapshot_id}.{uuid.uuid4().hex}.tmp"
    staging.mkdir(parents=False)
    try:
        frames = {
            "market": snapshot.market,
            "securities": snapshot.securities,
            "actions": snapshot.actions,
            "benchmarks": snapshot.benchmarks,
        }
        for name, frame in frames.items():
            frame.to_parquet(staging / _SERIALIZED_FILES[name], index=False)
        (staging / _SERIALIZED_FILES["calendar"]).write_bytes(_canonical_json(snapshot.calendar))

        files: dict[str, Any] = {}
        for name, filename in _SERIALIZED_FILES.items():
            path = staging / filename
            files[filename] = {
                "sha256": _sha256_path(path),
                "bytes": path.stat().st_size,
                "rows": len(frames[name]) if name in frames else len(snapshot.calendar),
            }
        manifest = {
            "format": "ashare-snapshot-v1",
            "snapshot_id": snapshot_id,
            "content_id": digest,
            "metadata": _jsonable(snapshot.metadata),
            "validation": result,
            "files": files,
        }
        (staging / "manifest.json").write_bytes(_canonical_json(manifest))
        try:
            staging.replace(target)
        except FileExistsError as exc:
            raise FileExistsError(f"snapshot path appeared while saving: {target}") from exc
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def load_snapshot(path: Path) -> Snapshot:
    """Load and hash-check a snapshot directory produced by ``save_snapshot``."""

    path = Path(path)
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"snapshot manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid snapshot manifest: {manifest_path}") from exc
    if manifest.get("format") != "ashare-snapshot-v1":
        raise ValueError("unsupported snapshot format")
    for filename, details in manifest.get("files", {}).items():
        file_path = path / filename
        if not file_path.exists():
            raise ValueError(f"snapshot file missing: {filename}")
        actual = _sha256_path(file_path)
        if actual != details.get("sha256"):
            raise ValueError(f"snapshot hash mismatch: {filename}")

    try:
        market = pd.read_parquet(path / _SERIALIZED_FILES["market"])
        securities = pd.read_parquet(path / _SERIALIZED_FILES["securities"])
        actions = pd.read_parquet(path / _SERIALIZED_FILES["actions"])
        benchmarks = pd.read_parquet(path / _SERIALIZED_FILES["benchmarks"])
        calendar = json.loads((path / _SERIALIZED_FILES["calendar"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read snapshot data: {path}") from exc
    snapshot = Snapshot(
        market=market,
        securities=securities,
        calendar=calendar,
        actions=actions,
        benchmarks=benchmarks,
        metadata=manifest.get("metadata", {}),
    )
    if snapshot.snapshot_id != str(manifest.get("snapshot_id")):
        raise ValueError("snapshot_id mismatch in manifest")
    if _content_digest(snapshot) != manifest.get("content_id"):
        raise ValueError("snapshot content hash mismatch")
    validation = validate_snapshot(snapshot)
    if validation["errors"]:
        raise ValueError("loaded snapshot is structurally invalid: " + "; ".join(validation["errors"]))
    return snapshot


def _next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _calendar_horizon(value: date) -> date:
    # A two-week horizon covers weekends and ordinary exchange holidays, so a
    # next-session proposal can use a schedule row even when the following
    # weekday is closed.
    return value + timedelta(days=14)


def make_demo_snapshot(sessions: int = 520, seed: int = 42) -> Snapshot:
    """Create deterministic labelled data for tests and offline demonstrations."""

    if sessions < 1:
        raise ValueError("sessions must be positive")
    if sessions > 5000:
        raise ValueError("sessions is unreasonably large")
    # A fixed end date keeps the fixture reproducible and leaves a known future
    # calendar session for next-day proposal tests.
    end = date(2025, 12, 31)
    days: list[date] = []
    current = end
    while len(days) < sessions:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    days.reverse()
    calendar = [item.isoformat() for item in days] + [_next_weekday(days[-1]).isoformat()]

    symbols = [f"600{index:03d}.SH" for index in range(1, 13)] + [
        f"000{index:03d}.SZ" for index in range(1, 5)
    ]
    industries = ["BANK", "TECH", "HEALTH", "INDUSTRIAL", "CONSUMER", "ENERGY"]
    securities = pd.DataFrame(
        [
            {
                "ts_code": symbol,
                "list_date": "2018-01-02",
                "delist_date": None,
                "exchange": "SSE" if symbol.endswith(".SH") else "SZSE",
                "board": "MAIN",
                "name": f"Demo {symbol.split('.')[0]}",
            }
            for symbol in symbols
        ],
        columns=SECURITY_COLUMNS,
    )

    rng = np.random.default_rng(seed)
    rows: list[dict[str, Any]] = []
    last_prices = {symbol: float(8 + index * 2.7) for index, symbol in enumerate(symbols)}
    for day_index, session in enumerate(days):
        trade_date = session.isoformat()
        for symbol_index, symbol in enumerate(symbols):
            previous = last_prices[symbol]
            drift = 0.00010 * (symbol_index - 5) + 0.0002 * math.sin(day_index / 41.0)
            daily_return = drift + float(rng.normal(0, 0.018))
            close = max(1.0, previous * (1.0 + daily_return))
            open_price = max(0.5, previous * (1.0 + float(rng.normal(0, 0.006))))
            high = max(open_price, close) * (1.0 + abs(float(rng.normal(0, 0.006))))
            low = min(open_price, close) * (1.0 - abs(float(rng.normal(0, 0.006))))
            # Keep synthetic turnover above the baseline liquidity floor so a
            # demo backtest exercises candidate and order paths. Volume is in
            # shares and rounded to a normal 100-share lot.
            volume = int(max(100_000, rng.lognormal(mean=13.0, sigma=0.35) * 30) // 100 * 100)
            amount = float(close * volume)
            rows.append(
                {
                    "ts_code": symbol,
                    "trade_date": trade_date,
                    "open": round(open_price, 2),
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "close": round(close, 2),
                    "volume": volume,
                    "amount": round(amount, 2),
                    "adj_factor": 1.0,
                    "up_limit": round(previous * 1.10, 2),
                    "down_limit": round(previous * 0.90, 2),
                    "suspended": False,
                    "is_st": False,
                    "state_known": True,
                    "industry": industries[symbol_index % len(industries)],
                    "industry_known": True,
                    "available_at": f"{trade_date}T18:00:00+08:00",
                }
            )
            last_prices[symbol] = close
    market = pd.DataFrame(rows, columns=MARKET_COLUMNS + ["available_at"])
    benchmark_rows = []
    for trade_date in [item.isoformat() for item in days]:
        closes = market.loc[market["trade_date"].eq(trade_date), "close"]
        benchmark_rows.append(
            {"trade_date": trade_date, "ts_code": "000300.SH", "close": float(closes.mean())}
        )
    benchmarks = pd.DataFrame(benchmark_rows, columns=BENCHMARK_COLUMNS)
    actions = _empty_frame(ACTION_COLUMNS)
    metadata = {
        "snapshot_id": f"demo-v2-{sessions}-{seed}",
        "content_id": "synthetic",
        "source": "synthetic-demo",
        "synthetic": True,
        "actions_complete": True,
        "point_in_time_evidence": True,
        "market_end": days[-1].isoformat(),
        "next_session": calendar[-1],
        "calendar_kind": "synthetic_weekdays",
        "amount_unit": "CNY",
        "volume_unit": "shares",
        "benchmark_kind": "synthetic",
    }
    return Snapshot(market, securities, calendar, actions, benchmarks, metadata)


def _parse_request_date(value: str) -> str:
    normalised = _normalise_date(value)
    if not normalised or not _ISO_DATE_RE.fullmatch(normalised):
        raise ValueError(f"invalid date: {value!r}")
    try:
        date.fromisoformat(normalised)
    except ValueError as exc:
        raise ValueError(f"invalid date: {value!r}") from exc
    return normalised


def _date_for_tushare(value: str) -> str:
    return value.replace("-", "")


class TushareDownloader:
    """Incremental raw Tushare cache with explicit PIT evidence requirements.

    ``client`` is intentionally an optional test/provider seam.  The normal
    constructor creates a Tushare Pro client lazily only when a token is
    available and ``download`` needs a missing partition.  No provider is
    contacted at import time.
    """

    ENDPOINTS = (
        "daily",
        "adj_factor",
        "stk_limit",
        "suspend_d",
        "trade_cal",
        "stock_basic",
        "dividend",
        "index_daily",
    )
    DAILY_ENDPOINTS = ("daily", "adj_factor", "stk_limit", "suspend_d", "dividend")
    STOCK_BASIC_STATUSES = ("L", "D", "P")
    MAX_ROWS = 5000
    MAX_RETRIES = 3

    def __init__(
        self,
        root: Path,
        token: str | None = None,
        client: Any | None = None,
        row_limit: int = MAX_ROWS,
        max_retries: int = MAX_RETRIES,
        min_request_interval: float = 0.35,
        cache_ttl_hours: float = 24.0,
    ) -> None:
        self.root = Path(root)
        self.raw_root = self.root / "raw"
        self.token = token or os.getenv("TUSHARE_TOKEN")
        self._client = client
        self.row_limit = max(1, int(row_limit))
        self.max_retries = max(1, int(max_retries))
        self.min_request_interval = max(0.0, float(min_request_interval))
        self.cache_ttl_hours = max(0.0, float(cache_ttl_hours))
        self._last_request_at = 0.0
        self._last_action_issues: list[str] = []
        self._suspension_evidence_incomplete = False

    def _cache_path(self, endpoint: str, key: str) -> Path:
        return self.raw_root / endpoint / key

    def _legacy_cache_path(self, endpoint: str, key: str) -> Path:
        return self.raw_root / endpoint / f"{key}.parquet"

    @staticmethod
    def _record_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _manifest_fresh(self, manifest: Mapping[str, Any]) -> bool:
        fetched_at = self._record_time(manifest.get("fetched_at"))
        if fetched_at is None:
            return False
        age = (datetime.now(UTC) - fetched_at).total_seconds()
        if manifest.get("endpoint") not in {"stock_basic", "trade_cal"}:
            return age >= 0  # Historical partitions change only on explicit refresh.
        return age >= 0 and age <= self.cache_ttl_hours * 3600

    def _revision_records(self, endpoint: str, key: str, *, verify: bool = True) -> list[dict[str, Any]]:
        logical = self._cache_path(endpoint, key)
        revisions = logical / "revisions"
        legacy = self._legacy_cache_path(endpoint, key)
        if legacy.exists() and not revisions.exists():
            raise ValueError(f"unmanifested legacy cache requires refresh: {legacy}")
        records: list[dict[str, Any]] = []
        if not revisions.exists():
            return records
        for manifest_path in sorted(revisions.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid raw cache manifest: {manifest_path}") from exc
            data_path = manifest_path.parent / str(manifest.get("data_file", "data.parquet"))
            if verify and manifest.get("status") in {"complete", "partial"}:
                if not data_path.exists():
                    raise ValueError(f"raw cache data missing: {data_path}")
                actual_hash = _sha256_path(data_path)
                if actual_hash != manifest.get("sha256"):
                    raise ValueError(f"raw cache hash mismatch: {data_path}")
                frame = pd.read_parquet(data_path)
                if int(manifest.get("rowcount", -1)) != len(frame):
                    raise ValueError(f"raw cache rowcount mismatch: {data_path}")
            records.append({"manifest": manifest, "manifest_path": manifest_path, "data_path": data_path})
        records.sort(
            key=lambda item: (
                str(item["manifest"].get("fetched_at", "")),
                str(item["manifest"].get("revision", "")),
            )
        )
        return records

    def _latest_record(self, endpoint: str, key: str, *, verify: bool = True) -> dict[str, Any] | None:
        records = self._revision_records(endpoint, key, verify=verify)
        return records[-1] if records else None

    def _read_record_frame(self, record: Mapping[str, Any]) -> pd.DataFrame:
        manifest = record["manifest"]
        if manifest.get("status") != "complete":
            raise ValueError(f"raw cache partition is not complete: {manifest.get('status')}")
        data_path = Path(record["data_path"])
        if not data_path.exists():
            raise ValueError(f"raw cache data missing: {data_path}")
        if _sha256_path(data_path) != manifest.get("sha256"):
            raise ValueError(f"raw cache hash mismatch: {data_path}")
        frame = pd.read_parquet(data_path)
        if len(frame) != int(manifest.get("rowcount", -1)):
            raise ValueError(f"raw cache rowcount mismatch: {data_path}")
        return frame

    def _get_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not self.token:
            return None
        try:
            import tushare as ts
        except ImportError:
            return None
        self._client = ts.pro_api(self.token)
        return self._client

    def capabilities(self, date: str, probe: bool = False) -> dict[str, Any]:
        requested = _parse_request_date(date)
        cache_entries: dict[str, dict[str, Any]] = {}
        for endpoint in self.ENDPOINTS:
            files = (
                sorted((self.raw_root / endpoint).glob("*.parquet"))
                if (self.raw_root / endpoint).exists()
                else []
            )
            cache_entries[endpoint] = {
                "cached_files": len(files),
                "cached": bool(files),
                "requested_date": requested,
            }
        result = {
            "provider": "tushare-pro",
            "requested_date": requested,
            "token_configured": bool(self.token),
            "network_checked": False,
            "capability_status": "cache_only",
            "cache_root": str(self.raw_root),
            "endpoints": cache_entries,
            "historical_status": {
                "st": "requires explicit PIT evidence",
                "industry": "requires explicit PIT evidence",
                "listing": "uses stock_basic dates; completeness must be checked",
                "actions": "requires explicit completeness evidence",
            },
        }
        if probe:
            client = self._get_client()
            if client is None:
                result["probe_error"] = "no client/token; capability probe was not attempted"
            else:
                try:
                    self._call_endpoint("trade_cal", requested, requested)
                except RuntimeError as exc:
                    result["probe_error"] = str(exc)
                else:
                    result["network_checked"] = True
                    result["capability_status"] = "probed"
        return result

    def _call_endpoint(self, endpoint: str, start: str, end: str, **options: Any) -> pd.DataFrame:
        client = self._get_client()
        if client is None:
            raise RuntimeError("Tushare token/client is unavailable; raw download was not attempted")
        start_ts = _date_for_tushare(start)
        end_ts = _date_for_tushare(end)
        method = getattr(client, endpoint, None)
        if method is None:
            raise RuntimeError(f"Tushare client does not expose endpoint {endpoint}")
        params: dict[str, Any]
        if endpoint == "trade_cal":
            params = {"exchange": "SSE", "start_date": start_ts, "end_date": end_ts}
        elif endpoint == "stock_basic":
            params = {
                "exchange": "",
                "list_status": options.get("list_status", "L"),
                "fields": "ts_code,symbol,name,exchange,market,list_status,list_date,delist_date",
            }
        elif endpoint == "suspend_d":
            params = {"trade_date": options.get("trade_date", start_ts)}
        elif endpoint == "dividend":
            params = {"ex_date": options.get("ex_date", start_ts)}
        elif endpoint == "index_daily":
            params = {
                "ts_code": options.get("ts_code", "000300.SH"),
                "start_date": start_ts,
                "end_date": end_ts,
            }
        elif options.get("trade_date"):
            params = {"trade_date": options["trade_date"]}
        else:
            params = {"start_date": start_ts, "end_date": end_ts}
        if options.get("ts_code") and endpoint not in {"index_daily"}:
            params["ts_code"] = options["ts_code"]
        if options.get("limit") is not None:
            params["limit"] = int(options["limit"])
        if options.get("offset") is not None:
            params["offset"] = int(options["offset"])
        self._request_trace = getattr(self, "_request_trace", []) + [
            dict(endpoint=endpoint, params=dict(params))
        ]
        self._last_request_params = dict(params)

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                elapsed = time.monotonic() - self._last_request_at
                if elapsed < self.min_request_interval:
                    time.sleep(self.min_request_interval - elapsed)
                self._last_request_at = time.monotonic()
                result = method(**params)
                if result is None:
                    return pd.DataFrame()
                return result.copy() if isinstance(result, pd.DataFrame) else pd.DataFrame(result)
            except Exception as exc:  # provider exceptions vary by Tushare version
                last_error = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(min(0.25 * (2**attempt), 1.0))
        raise RuntimeError(
            f"Tushare {endpoint} request failed after {self.max_retries} attempts"
        ) from last_error

    def _write_cache(
        self,
        endpoint: str,
        key: str,
        frame: pd.DataFrame,
        *,
        params: Mapping[str, Any] | None = None,
        status: str = "complete",
        error: str | None = None,
    ) -> dict[str, Any]:
        logical = self._cache_path(endpoint, key)
        revision = f"rev-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex[:10]}"
        revision_dir = logical / "revisions" / revision
        revision_dir.mkdir(parents=True, exist_ok=False)
        data_path = revision_dir / "data.parquet"
        frame.to_parquet(data_path, index=False)
        fetched_at = datetime.now(UTC).isoformat()
        manifest: dict[str, Any] = {
            "format": "ashare-raw-request-v1",
            "endpoint": endpoint,
            "logical_key": key,
            "revision": revision,
            "params": _jsonable(dict(params or {})),
            "fetched_at": fetched_at,
            "expires_at": (datetime.now(UTC) + timedelta(hours=self.cache_ttl_hours)).isoformat(),
            "rowcount": len(frame),
            "sha256": _sha256_path(data_path),
            "data_file": "data.parquet",
            "status": status,
            "transport_complete": status == "complete",
            "source_coverage_verified": False,
            "requests": getattr(self, "_request_trace", []),
        }
        if error:
            manifest["error"] = error
        (revision_dir / "manifest.json").write_bytes(_canonical_json(manifest))
        return {"manifest": manifest, "manifest_path": revision_dir / "manifest.json", "data_path": data_path}

    def _write_failure(
        self,
        endpoint: str,
        key: str,
        *,
        params: Mapping[str, Any] | None = None,
        status: str = "failed",
        error: str,
    ) -> dict[str, Any]:
        return self._write_cache(endpoint, key, pd.DataFrame(), params=params, status=status, error=error)

    def _cached_stock_codes(self) -> list[str]:
        codes: set[str] = set()
        for status in self.STOCK_BASIC_STATUSES:
            try:
                record = self._latest_record("stock_basic", f"status_{status}")
                if record and record["manifest"].get("status") == "complete":
                    frame = self._read_record_frame(record)
                    if "ts_code" in frame.columns:
                        codes.update(frame["ts_code"].dropna().astype(str))
            except ValueError:
                continue
        return sorted(codes)

    def _fetch_capped_partition(self, endpoint: str, day: str, warnings: list[str]) -> pd.DataFrame | None:
        options: dict[str, Any] = {"trade_date": _date_for_tushare(day), "limit": self.row_limit}
        if endpoint == "dividend":
            options = {"ex_date": _date_for_tushare(day), "limit": self.row_limit}
        try:
            self._request_trace = []
            frame = self._call_endpoint(endpoint, day, day, **options)
        except RuntimeError as exc:
            warnings.append(str(exc))
            return None
        if len(frame) < self.row_limit:
            return frame

        # A Tushare result at the provider limit may be truncated. Split by
        # symbol before caching; if the fake/provider ignores ts_code, leave it
        # uncached and report an incomplete partition instead of pretending.
        codes = self._cached_stock_codes()
        if not codes:
            warnings.append(
                f"{endpoint} {day} reached row limit without a symbol universe; partition is incomplete"
            )
            return None
        pieces: list[pd.DataFrame] = []
        for offset in range(0, len(codes), 300):
            chunk = ",".join(codes[offset : offset + 300])
            chunk_options = dict(options)
            chunk_options["ts_code"] = chunk
            try:
                piece = self._call_endpoint(endpoint, day, day, **chunk_options)
            except RuntimeError as exc:
                warnings.append(str(exc))
                return None
            if len(piece) >= self.row_limit:
                warnings.append(
                    f"{endpoint} {day} remained at row limit after symbol splitting; partition is incomplete"
                )
                return None
            pieces.append(piece)
        return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()

    def _stock_pages(self, start: str, end: str, status: str) -> pd.DataFrame:
        pages, seen = [], set()
        for offset in range(0, 1_000_000, self.row_limit):
            page = self._call_endpoint(
                "stock_basic", start, end, list_status=status, limit=self.row_limit, offset=offset
            )
            if not page.empty:
                if not {"ts_code", "list_date", "delist_date"}.issubset(page):
                    raise RuntimeError("stock_basic listing lifecycle fields missing")
                if status == "D" and page.delist_date.isna().any():
                    raise RuntimeError("Delisted stock has no delist_date")
                digest = hashlib.sha256(_stable_frame_bytes(page)).hexdigest()
                if digest in seen:
                    raise RuntimeError("stock_basic pagination did not advance")
                seen.add(digest)
                pages.append(page)
            if len(page) < self.row_limit:
                return pd.concat(pages, ignore_index=True) if pages else page
        raise RuntimeError("stock_basic pagination exceeded bound")

    def download(self, start: str, end: str, refresh: bool = False) -> dict[str, Any]:
        start = _parse_request_date(start)
        end = _parse_request_date(end)
        if start > end:
            raise ValueError("start must be <= end")
        self.raw_root.mkdir(parents=True, exist_ok=True)
        files: dict[str, list[str]] = {}
        rows: dict[str, int] = {}
        cached_items: list[str] = []
        downloaded: list[str] = []
        warnings: list[str] = []
        endpoint_completeness: dict[str, dict[str, Any]] = {}

        def warn(message: str) -> None:
            if message not in warnings:
                warnings.append(message)

        def component(endpoint: str) -> dict[str, Any]:
            return endpoint_completeness.setdefault(
                endpoint, {"complete": True, "requests": [], "reasons": []}
            )

        def record(
            endpoint: str, cache_record: Mapping[str, Any], complete: bool, reason: str | None = None
        ) -> None:
            manifest = dict(cache_record["manifest"])
            manifest["data_path"] = str(cache_record.get("data_path", ""))
            item = component(endpoint)
            item["requests"].append(manifest)
            if not complete:
                item["complete"] = False
                if reason and reason not in item["reasons"]:
                    item["reasons"].append(reason)

        def cached_partition(endpoint: str, key: str) -> dict[str, Any] | None:
            try:
                cache_record = self._latest_record(endpoint, key)
            except ValueError as exc:
                warn(str(exc))
                record(
                    endpoint, {"manifest": {"status": "corrupt", "error": str(exc)}}, False, "cache_corrupt"
                )
                return None
            if cache_record is None:
                return None
            manifest = cache_record["manifest"]
            if manifest.get("status") != "complete":
                return None
            if not self._manifest_fresh(manifest):
                manifest = dict(manifest)
                manifest["cache_status"] = "expired"
                component(endpoint)["requests"].append(manifest)
                warn(f"{endpoint}/{key} cache metadata expired; refresh required")
                return None
            try:
                frame = self._read_record_frame(cache_record)
            except ValueError as exc:
                warn(str(exc))
                record(
                    endpoint, {"manifest": {"status": "corrupt", "error": str(exc)}}, False, "cache_corrupt"
                )
                return None
            result = dict(cache_record)
            result["frame"] = frame
            record(endpoint, cache_record, True)
            return result

        def fetched(
            endpoint: str,
            key: str,
            fetch: Any,
            *,
            params: Mapping[str, Any] | None = None,
            allow_partial: bool = False,
        ) -> dict[str, Any] | None:
            try:
                self._request_trace = []
                frame = fetch()
            except RuntimeError as exc:
                error = str(exc)
                cache_record = self._write_failure(endpoint, key, params=params, error=error)
                record(endpoint, cache_record, False, "request_failed")
                warn(error)
                return None
            if frame is None:
                frame = pd.DataFrame()
            if len(frame) >= self.row_limit and not allow_partial:
                cache_record = self._write_cache(
                    endpoint, key, frame, params=params, status="partial", error="row_limit"
                )
                record(endpoint, cache_record, False, "row_limit_truncated")
                warn(f"{endpoint}/{key} reached row limit; response is partial")
                return None
            cache_record = self._write_cache(endpoint, key, frame, params=params, status="complete")
            record(endpoint, cache_record, True)
            return {**cache_record, "frame": frame}

        def use_or_fetch(
            endpoint: str,
            key: str,
            fetch: Any,
            *,
            params: Mapping[str, Any] | None = None,
            allow_large: bool = False,
        ) -> dict[str, Any] | None:
            if not refresh:
                hit = cached_partition(endpoint, key)
                if hit is not None:
                    cached_items.append(f"{endpoint}:{key}")
                    return hit
            result = fetched(endpoint, key, fetch, params=params, allow_partial=allow_large)
            if result is not None:
                downloaded.append(f"{endpoint}:{key}")
            return result

        extended_end = _calendar_horizon(date.fromisoformat(end)).isoformat()
        trade_key = f"range_{start.replace('-', '')}_{extended_end.replace('-', '')}"
        trade_result = use_or_fetch(
            "trade_cal",
            trade_key,
            lambda: self._call_endpoint("trade_cal", start, extended_end),
            params={
                "exchange": "SSE",
                "start_date": _date_for_tushare(start),
                "end_date": _date_for_tushare(extended_end),
            },
        )
        trade_frame = trade_result["frame"] if trade_result is not None else pd.DataFrame()
        if trade_result is not None:
            files["trade_cal"] = [str(trade_result["data_path"])]
            rows["trade_cal"] = len(trade_frame)
        calendar_valid = not trade_frame.empty and {"cal_date", "is_open"}.issubset(trade_frame.columns)
        if calendar_valid:
            dates = _normalise_dates(trade_frame, {"cal_date"})
            expected_days = set(pd.date_range(start, extended_end).strftime("%Y-%m-%d"))
            calendar_valid = (
                not dates.cal_date.duplicated().any()
                and set(dates.cal_date) == expected_days
                and dates.is_open.isin([0, 1]).all()
            )
        if calendar_valid:
            open_days = sorted(
                value
                for value in dates.loc[dates["is_open"].eq(1), "cal_date"].dropna().astype(str)
                if start <= value <= end
            )
        else:
            open_days = []
            component("trade_cal")["complete"] = False
            if "missing_calendar" not in component("trade_cal")["reasons"]:
                component("trade_cal")["reasons"].append("missing_calendar")
            warn("trade_cal is missing or malformed; weekday fallback is disabled")

        stock_ready = True
        for status in self.STOCK_BASIC_STATUSES:
            key = f"status_{status}"
            result = use_or_fetch(
                "stock_basic",
                key,
                lambda status=status: self._stock_pages(start, end, status),
                params={"exchange": "", "list_status": status},
                allow_large=True,
            )
            if result is None:
                stock_ready = False
                continue
            path = str(result["data_path"])
            files.setdefault("stock_basic", []).append(path)
            rows["stock_basic"] = rows.get("stock_basic", 0) + len(result["frame"])

        for endpoint in self.DAILY_ENDPOINTS:
            if not calendar_valid:
                component(endpoint)["complete"] = False
                component(endpoint)["reasons"].append("missing_calendar")
                continue
            endpoint_files: list[str] = []
            endpoint_rows = 0
            for day in open_days:
                key = day.replace("-", "")
                if not refresh:
                    result = cached_partition(endpoint, key)
                else:
                    result = None
                if result is None:
                    frame = self._fetch_capped_partition(endpoint, day, warnings)
                    if frame is None:
                        cache_record = self._write_failure(
                            endpoint,
                            key,
                            params=getattr(self, "_last_request_params", {}),
                            status="partial",
                            error="request or row-limit incomplete",
                        )
                        record(endpoint, cache_record, False, "request_or_row_limit_incomplete")
                        continue
                    cache_record = self._write_cache(
                        endpoint,
                        key,
                        frame,
                        params=getattr(self, "_last_request_params", {}),
                    )
                    record(endpoint, cache_record, True)
                    downloaded.append(f"{endpoint}:{key}")
                    result = {**cache_record, "frame": frame}
                else:
                    cached_items.append(f"{endpoint}:{key}")
                endpoint_files.append(str(result["data_path"]))
                endpoint_rows += len(result["frame"])
            if endpoint_files:
                files[endpoint] = endpoint_files
                rows[endpoint] = endpoint_rows
            if len(endpoint_files) != len(open_days):
                component(endpoint)["complete"] = False
                component(endpoint)["reasons"].append("missing_or_incomplete_partition")

        index_key = f"range_{start.replace('-', '')}_{end.replace('-', '')}"
        index_result = use_or_fetch(
            "index_daily",
            index_key,
            lambda: self._call_endpoint("index_daily", start, end),
            params={
                "ts_code": "000300.SH",
                "start_date": _date_for_tushare(start),
                "end_date": _date_for_tushare(end),
            },
        )
        if index_result is not None:
            files["index_daily"] = [str(index_result["data_path"])]
            rows["index_daily"] = len(index_result["frame"])

        if not stock_ready:
            component("stock_basic")["complete"] = False
            component("stock_basic")["reasons"].append("one_or_more_status_requests_failed")
        overall_complete = all(item.get("complete", False) for item in endpoint_completeness.values())
        status = "complete" if overall_complete else ("unavailable" if not files else "partial")
        request_manifest = [
            {"endpoint": endpoint, **request}
            for endpoint, item in endpoint_completeness.items()
            for request in item.get("requests", [])
        ]
        return {
            "status": status,
            "start": start,
            "end": end,
            "files": files,
            "rows": rows,
            "cached": cached_items,
            "downloaded": downloaded,
            "warnings": warnings,
            "endpoint_completeness": endpoint_completeness,
            "request_manifest": request_manifest,
            "trading_ready": False,
            "reason": "raw cache requires normalization, point-in-time evidence, and historical universe checks",
        }

    @staticmethod
    def _read_evidence(evidence_dir: Path | None, stem: str) -> pd.DataFrame:
        if evidence_dir is None:
            return pd.DataFrame()
        evidence_dir = Path(evidence_dir)
        for suffix in (".parquet", ".csv", ".jsonl", ".json"):
            path = evidence_dir / f"{stem}{suffix}"
            if not path.exists():
                continue
            if suffix == ".parquet":
                return pd.read_parquet(path)
            if suffix == ".csv":
                return pd.read_csv(path)
            if suffix == ".jsonl":
                return pd.read_json(path, lines=True)
            return pd.DataFrame(json.loads(path.read_text(encoding="utf-8")))
        return pd.DataFrame()

    def _load_raw(self, endpoint: str, start: str, end: str) -> pd.DataFrame:
        directory = self.raw_root / endpoint
        if not directory.exists():
            return pd.DataFrame()
        start_key = start.replace("-", "")
        end_key = end.replace("-", "")
        logical_keys: list[str] = []
        legacy_paths = list(directory.glob("*.parquet"))
        if legacy_paths:
            raise ValueError(f"unmanifested legacy raw cache requires refresh: {legacy_paths[0]}")
        for logical in sorted(path for path in directory.iterdir() if path.is_dir()):
            key = logical.name
            if endpoint == "stock_basic" and key.startswith("status_"):
                logical_keys.append(key)
            elif key.startswith("range_"):
                logical_keys.append(key)
            elif re.fullmatch(r"\d{8}", key) and start_key <= key <= end_key:
                logical_keys.append(key)
        if not logical_keys:
            return pd.DataFrame()
        frames: list[pd.DataFrame] = []
        for key in logical_keys:
            record = self._latest_record(endpoint, key)
            if record is None or record["manifest"].get("status") != "complete":
                continue
            # Staleness is evaluated by download() for its active requests. Old
            # overlapping revisions are retained here solely for conflict checks.
            frames.append(self._read_record_frame(record))
        if not frames:
            return pd.DataFrame()

        key_columns = {
            "stock_basic": ["ts_code"],
            "trade_cal": ["cal_date", "exchange"],
            "index_daily": ["ts_code", "trade_date"],
            "dividend": ["ts_code", "record_date", "ex_date", "pay_date"],
        }.get(endpoint, ["ts_code", "trade_date"])
        key_columns = [column for column in key_columns if any(column in frame.columns for frame in frames)]
        merged_frames = pd.concat(frames, ignore_index=True)
        if key_columns and all(column in merged_frames.columns for column in key_columns):
            seen: dict[tuple[Any, ...], bytes] = {}
            keep: list[bool] = []
            for row in merged_frames.to_dict(orient="records"):
                key = tuple(_jsonable(row.get(column)) for column in key_columns)
                payload = _canonical_json(row)
                previous = seen.get(key)
                if previous is not None and previous != payload:
                    raise ValueError(f"conflicting overlapping raw cache rows for {endpoint}: {key}")
                keep.append(previous is None)
                seen[key] = payload
            result = merged_frames.loc[keep].reset_index(drop=True)
        else:
            result = merged_frames.reset_index(drop=True)
        for column in ("trade_date", "cal_date", "ex_date"):
            if column in result.columns:
                result = _normalise_dates(result, {column})
                filter_end = end
                if endpoint == "trade_cal" and column == "cal_date":
                    filter_end = _calendar_horizon(date.fromisoformat(end)).isoformat()
                result = result.loc[result[column].between(start, filter_end, inclusive="both")]
        return result.reset_index(drop=True)

    def build_snapshot(self, start: str, end: str, evidence_dir: Path | None = None) -> Snapshot:
        """Normalize cached data, retaining unknown PIT fields as unknown.

        The method may fetch a missing raw partition if a configured client is
        available.  It never turns current stock metadata into historical ST or
        industry labels.  ``evidence_dir`` is described in ``docs/DATA.md``.
        """

        start = _parse_request_date(start)
        end = _parse_request_date(end)
        if start > end:
            raise ValueError("start must be <= end")
        download_result = self.download(start, end)
        raw = {endpoint: self._load_raw(endpoint, start, end) for endpoint in self.ENDPOINTS}
        from .universe import supported_security

        basic_scope = raw["stock_basic"].copy()
        if not basic_scope.empty:
            basic_scope = basic_scope.loc[
                basic_scope.ts_code.map(
                    lambda code: supported_security(
                        dict(board=_board_for_code(code), exchange=_exchange_for_code(code))
                    )
                )
            ]
        supported_codes = set(basic_scope.ts_code) if not basic_scope.empty else set()
        daily = raw["daily"].copy()
        if "ts_code" in daily:
            unknown_main = {
                code
                for code in daily.ts_code
                if supported_security(dict(board=_board_for_code(code), exchange=_exchange_for_code(code)))
            } - supported_codes
            if unknown_main:
                raise ValueError("Mainboard daily symbols absent from stock inventory")
            daily = daily.loc[daily.ts_code.isin(supported_codes)]
        if daily.empty:
            raise ValueError("no daily raw cache available for requested range")
        daily = _normalise_dates(daily, {"trade_date"})
        daily["ts_code"] = daily["ts_code"].astype(str)
        raw_volume = (
            daily["vol"]
            if "vol" in daily.columns
            else daily.get("volume", pd.Series(np.nan, index=daily.index))
        )
        raw_amount = daily["amount"] if "amount" in daily.columns else pd.Series(np.nan, index=daily.index)
        daily["volume"] = pd.to_numeric(raw_volume, errors="coerce") * 100
        daily["amount"] = pd.to_numeric(raw_amount, errors="coerce") * 1000
        daily = daily.rename(columns={"vol": "_raw_vol"})

        from .hardening import add_valuations, make_spine

        schedule = _normalise_dates(raw["trade_cal"], {"cal_date"})
        if schedule.empty or not {"cal_date", "is_open"}.issubset(schedule):
            raise ValueError("Verified trade calendar required; no weekday inference")
        open_calendar = sorted(set(schedule.loc[schedule.is_open.eq(1), "cal_date"]))
        full_day = _normalise_dates(self._read_evidence(evidence_dir, "suspensions"), {"trade_date"})
        daily = make_spine(daily, basic_scope, open_calendar, start, end, full_day)

        adj = raw["adj_factor"].copy()
        if not adj.empty:
            adj = _normalise_dates(adj, {"trade_date"})
            adj = adj[[column for column in ("ts_code", "trade_date", "adj_factor") if column in adj.columns]]
            daily = daily.drop(columns=["adj_factor"], errors="ignore")
            daily = daily.merge(adj, on=["ts_code", "trade_date"], how="left")
        else:
            daily["adj_factor"] = np.nan
        if "adj_factor" not in daily.columns:
            daily["adj_factor"] = np.nan

        limits = raw["stk_limit"].copy()
        if not limits.empty:
            limits = _normalise_dates(limits, {"trade_date"})
            limits = limits.rename(columns={"up_limit": "up_limit", "down_limit": "down_limit"})
            limits = limits[
                [
                    column
                    for column in ("ts_code", "trade_date", "up_limit", "down_limit")
                    if column in limits.columns
                ]
            ]
            daily = daily.drop(columns=["up_limit", "down_limit"], errors="ignore")
            daily = daily.merge(limits, on=["ts_code", "trade_date"], how="left")
        for column in ("up_limit", "down_limit"):
            if column not in daily.columns:
                daily[column] = np.nan

        suspend = raw["suspend_d"].copy()
        self._suspension_evidence_incomplete = False
        if not suspend.empty and {"ts_code", "trade_date"}.issubset(suspend.columns):
            suspend = _normalise_dates(suspend, {"trade_date"})
            if "suspend_type" not in suspend.columns:
                suspend["suspended"] = pd.NA
                self._suspension_evidence_incomplete = True
            else:
                suspend_type = suspend["suspend_type"].fillna("").astype(str).str.upper()
                suspend["suspended"] = suspend_type.map({"S": True, "R": False})
                if suspend["suspended"].isna().any():
                    self._suspension_evidence_incomplete = True
            suspend = suspend[["ts_code", "trade_date", "suspended"]]
            suspend = suspend.groupby(["ts_code", "trade_date"], as_index=False)["suspended"].agg(
                lambda values: True if values.eq(True).any() else (pd.NA if values.isna().any() else False)
            )
            daily = daily.drop(columns=["suspended"], errors="ignore")
            daily = daily.merge(suspend, on=["ts_code", "trade_date"], how="left")
        suspended = (
            daily["suspended"] if "suspended" in daily.columns else pd.Series(False, index=daily.index)
        )
        daily["suspended"] = suspended.eq(True)
        event_halt = daily["suspended"].copy()
        daily["suspended"] = daily["full_day_confirmed"]
        daily["session_state_known"] = daily["quote_present"] | daily["full_day_confirmed"]
        daily["tradeable"] = daily["quote_present"] & ~event_halt & ~daily["suspended"]
        daily.loc[daily["suspended"], ["volume", "amount"]] = 0.0

        state_evidence = self._read_evidence(evidence_dir, "state")
        industry_evidence = self._read_evidence(evidence_dir, "industry")
        availability_evidence = self._read_evidence(evidence_dir, "availability")
        for evidence_frame in (state_evidence, industry_evidence):
            for flag in ("is_st", "state_known", "industry_known"):
                if (
                    flag in evidence_frame
                    and not evidence_frame[flag].dropna().map(lambda v: isinstance(v, (bool, np.bool_))).all()
                ):
                    raise ValueError(f"{flag} evidence requires boolean values, not strings or numbers")
        daily["is_st"] = False
        daily["state_known"] = False
        if not state_evidence.empty and {"ts_code", "trade_date", "is_st"}.issubset(state_evidence.columns):
            state_evidence = _normalise_dates(state_evidence, {"trade_date"})
            columns = ["ts_code", "trade_date", "is_st"]
            if "state_known" in state_evidence.columns:
                columns.append("state_known")
            daily = daily.drop(columns=["is_st", "state_known"], errors="ignore").merge(
                state_evidence[columns], on=["ts_code", "trade_date"], how="left"
            )
            if "state_known" not in daily.columns:
                daily["state_known"] = daily["is_st"].notna()
            daily["state_known"] = daily["state_known"].eq(True) & daily["is_st"].notna()
            daily["state_known"] = daily["state_known"].fillna(False).astype(bool)
            daily["is_st"] = daily["is_st"].fillna(False).astype(bool)

        daily["industry"] = None
        daily["industry_known"] = False
        if not industry_evidence.empty and {"ts_code", "trade_date", "industry"}.issubset(
            industry_evidence.columns
        ):
            industry_evidence = _normalise_dates(industry_evidence, {"trade_date"})
            columns = ["ts_code", "trade_date", "industry"]
            if "industry_known" in industry_evidence.columns:
                columns.append("industry_known")
            daily = daily.drop(columns=["industry", "industry_known"], errors="ignore").merge(
                industry_evidence[columns], on=["ts_code", "trade_date"], how="left"
            )
            if "industry_known" not in daily.columns:
                daily["industry_known"] = daily["industry"].notna()
            daily["industry_known"] = daily["industry_known"].fillna(False).astype(bool)

        if not availability_evidence.empty and {"ts_code", "trade_date", "available_at"}.issubset(
            availability_evidence.columns
        ):
            availability_evidence = _normalise_dates(availability_evidence, {"trade_date"})
            daily = daily.drop(columns=["available_at"], errors="ignore").merge(
                availability_evidence[["ts_code", "trade_date", "available_at"]],
                on=["ts_code", "trade_date"],
                how="left",
            )

        opening_evidence = _normalise_dates(self._read_evidence(evidence_dir, "opening"), {"trade_date"})
        if not opening_evidence.empty:
            daily = daily.merge(
                opening_evidence[["ts_code", "trade_date", "opening_volume", "opening_observed_at"]],
                on=["ts_code", "trade_date"],
                how="left",
                validate="one_to_one",
            )
        market_columns = (
            MARKET_COLUMNS
            + ["tradeable", "session_state_known", "quote_present"]
            + [c for c in ("available_at", "opening_volume", "opening_observed_at") if c in daily.columns]
        )
        market = pd.DataFrame(columns=market_columns)
        for column in market_columns:
            if column in daily.columns:
                market[column] = daily[column]
            elif column in {"open", "high", "low", "close", "volume", "amount"}:
                market[column] = np.nan
            else:
                market[column] = None

        basic = basic_scope.copy()
        if basic.empty:
            securities = pd.DataFrame(columns=SECURITY_COLUMNS)
        else:
            basic["ts_code"] = basic["ts_code"].astype(str)
            securities = pd.DataFrame(
                {
                    "ts_code": basic["ts_code"],
                    "list_date": basic.get("list_date"),
                    "delist_date": basic.get("delist_date"),
                    "exchange": basic["ts_code"].map(_exchange_for_code),
                    "board": basic["ts_code"].map(_board_for_code),
                    "name": basic.get("name", basic["ts_code"]),
                },
                columns=SECURITY_COLUMNS,
            )
            securities = _normalise_dates(securities, {"list_date", "delist_date"})

        trade_cal = raw["trade_cal"].copy()
        if not trade_cal.empty and "cal_date" in trade_cal.columns:
            trade_cal = _normalise_dates(trade_cal, {"cal_date"})
            is_open = (
                trade_cal["is_open"]
                if "is_open" in trade_cal.columns
                else pd.Series(0, index=trade_cal.index)
            )
            calendar = trade_cal.loc[is_open.eq(1), "cal_date"].dropna().astype(str).tolist()
            calendar_end = _calendar_horizon(date.fromisoformat(end)).isoformat()
            calendar = sorted({value for value in calendar if start <= value <= calendar_end})
        else:
            calendar = sorted(set(market["trade_date"].dropna().astype(str)))
        actions = self._normalise_actions(raw["dividend"], evidence_dir)
        action_issues = list(self._last_action_issues)
        extra_actions = self._read_evidence(evidence_dir, "actions")
        if not extra_actions.empty:
            if not set(ACTION_COLUMNS).issubset(extra_actions):
                raise ValueError("Supplementary action evidence must satisfy normalized action schema")
            extra_actions = _normalise_dates(extra_actions[ACTION_COLUMNS], _DATE_COLUMNS)
            combined = pd.concat([actions, extra_actions], ignore_index=True)
            values = [c for c in ACTION_COLUMNS if c != "action_id"]
            unique = combined.drop_duplicates(values)
            if unique.duplicated(["ts_code", "record_date", "ex_date"]).any():
                raise ValueError("Conflicting supplementary corporate action evidence")
            actions = unique.reset_index(drop=True)
        market = add_valuations(market, actions)
        if raw["index_daily"].empty:
            benchmarks = _empty_frame(BENCHMARK_COLUMNS)
            benchmark_kind = "synthetic"
            benchmark_missing = True
        else:
            benchmarks = _benchmark_from_index(raw["index_daily"])
            benchmark_kind = "price"
            benchmark_missing = benchmarks.empty

        evidence_hash = _evidence_digest(evidence_dir)
        raw_fingerprint = _raw_digest(raw)
        metadata = {
            "source": "tushare-pro",
            "synthetic": False,
            "raw_download": download_result,
            "point_in_time_evidence": bool(not state_evidence.empty and not industry_evidence.empty),
            "availability_evidence": bool(
                not availability_evidence.empty
                and {"ts_code", "trade_date", "available_at"}.issubset(availability_evidence.columns)
            ),
            "actions_complete": _actions_complete(evidence_dir),
            "unknown_actions": action_issues,
            "suspension_evidence_incomplete": self._suspension_evidence_incomplete,
            "amount_unit": "CNY (Tushare amount converted from thousand CNY)",
            "volume_unit": "shares (Tushare vol converted from hands)",
            "benchmark_kind": benchmark_kind,
            "benchmark_missing": benchmark_missing,
            "raw_fingerprint": raw_fingerprint,
            "normalization": {
                "missing_daily_rows_are_not_suspension": True,
                "st_and_industry_require_evidence": True,
                "cash_dividend_net_assumption": "cash_div only; cash_div_tax is gross and never substituted",
            },
        }
        for evidence_name in ("session_receipts", "inventory_receipts"):
            receipt_path = Path(evidence_dir) / f"{evidence_name}.json" if evidence_dir else None
            metadata[evidence_name] = (
                json.loads(receipt_path.read_text(encoding="utf-8"))
                if receipt_path and receipt_path.exists()
                else {}
            )
        provisional = Snapshot(market, securities, calendar, actions, benchmarks, metadata)
        normalized_fingerprint = _content_digest(provisional)
        metadata["normalized_fingerprint"] = normalized_fingerprint
        metadata["snapshot_id"] = (
            f"tushare-{start.replace('-', '')}-{end.replace('-', '')}-"
            f"{raw_fingerprint[:12]}-{normalized_fingerprint[:12]}-{evidence_hash[:8]}"
        )
        return Snapshot(market, securities, calendar, actions, benchmarks, metadata)

    def _normalise_actions(self, raw: pd.DataFrame, evidence_dir: Path | None) -> pd.DataFrame:
        self._last_action_issues = []
        if raw.empty:
            return _empty_frame(ACTION_COLUMNS)
        raw = _normalise_dates(
            raw.copy(),
            {
                "record_date",
                "ex_date",
                "pay_date",
                "div_listdate",
                "div_list_date",
                "imp_ann_date",
                "ann_date",
            },
        )
        if "div_proc" not in raw.columns:
            self._last_action_issues.append("div_proc is absent; dividend rows were excluded")
            return _empty_frame(ACTION_COLUMNS)
        if "div_proc" in raw.columns:
            unimplemented = raw["div_proc"].fillna("").astype(str).ne("实施")
            if unimplemented.any():
                self._last_action_issues.append("dividend rows without div_proc=实施 were excluded")
                raw = raw.loc[~unimplemented].copy()
                if raw.empty:
                    return _empty_frame(ACTION_COLUMNS)
        result = pd.DataFrame()
        result["ts_code"] = raw.get("ts_code")
        result["record_date"] = raw.get("record_date")
        result["ex_date"] = raw.get("ex_date")
        result["pay_date"] = raw.get("pay_date")
        share_date = (
            raw["div_listdate"]
            if "div_listdate" in raw.columns
            else raw.get("div_list_date", pd.Series(pd.NA, index=raw.index))
        )
        result["share_list_date"] = share_date
        # Tushare documents cash_div as tax-after (net) and cash_div_tax as
        # tax-before (gross). Never silently use the gross value as net.
        if "cash_div" in raw.columns:
            cash_values = raw["cash_div"]
        else:
            cash_values = pd.Series(np.nan, index=raw.index)
            if "cash_div_tax" in raw.columns and raw["cash_div_tax"].notna().any():
                self._last_action_issues.append("cash_div (net) is absent; cash_div_tax was not substituted")
        bonus_values = raw["stk_div"] if "stk_div" in raw.columns else pd.Series(np.nan, index=raw.index)
        result["cash_per_share"] = pd.to_numeric(cash_values, errors="coerce")
        result["bonus_ratio"] = pd.to_numeric(bonus_values, errors="coerce")
        known_at = raw["imp_ann_date"] if "imp_ann_date" in raw.columns else pd.Series(pd.NA, index=raw.index)
        result["known_at"] = known_at
        # A zero cash component does not need a payment date, and a zero bonus
        # component does not need a share-list date.  In those cases ex-date is
        # a harmless explicit placeholder for the required date column. A
        # positive cash/bonus component must retain the provider's settlement
        # or share-list date; it is never guessed from ex-date.
        cash_values = result["cash_per_share"]
        bonus_values = result["bonus_ratio"]
        zero_cash = cash_values.eq(0) & cash_values.notna() & result["pay_date"].isna()
        zero_bonus = bonus_values.eq(0) & bonus_values.notna() & result["share_list_date"].isna()
        result.loc[zero_cash, "pay_date"] = result.loc[zero_cash, "ex_date"]
        result.loc[zero_bonus, "share_list_date"] = result.loc[zero_bonus, "ex_date"]
        positive_cash_missing_pay = cash_values.gt(0) & result["pay_date"].isna()
        positive_bonus_missing_list = bonus_values.gt(0) & result["share_list_date"].isna()
        if positive_cash_missing_pay.any() or positive_bonus_missing_list.any():
            self._last_action_issues.append("corporate action is missing cash pay date or share-list date")
        if result["ex_date"].isna().any() or result["known_at"].isna().any():
            self._last_action_issues.append("corporate action is missing ex_date or implementation known_at")
        if (result["cash_per_share"].eq(0) & result["bonus_ratio"].eq(0)).any():
            self._last_action_issues.append(
                "corporate action has neither a supported net cash nor bonus amount"
            )
        result["action_id"] = result.apply(
            lambda row: hashlib.sha256(_canonical_json(row.to_dict())).hexdigest()[:24], axis=1
        )
        return result[ACTION_COLUMNS]


def _board_for_code(code: Any) -> str:
    code = str(code)
    prefix = code.split(".")[0]
    if prefix.startswith("688"):
        return "STAR"
    if prefix.startswith(("300", "301")):
        return "CHINEXT"
    if prefix.startswith(("8", "4", "92")):
        return "BSE"
    if prefix.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "MAIN"
    return "OTHER"


def _exchange_for_code(code: Any) -> str:
    text = str(code)
    if text.endswith(".SH"):
        return "SSE"
    if text.endswith(".BJ") or _board_for_code(text) == "BSE":
        return "BSE"
    if text.endswith(".SZ"):
        return "SZSE"
    return "OTHER"


def _benchmark_from_index(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize the actual index_daily response; never derive an index from stocks."""

    if raw.empty or not {"trade_date", "close"}.issubset(raw.columns):
        return _empty_frame(BENCHMARK_COLUMNS)
    result = raw.copy()
    result = _normalise_dates(result, {"trade_date"})
    codes = result["ts_code"] if "ts_code" in result.columns else pd.Series("000300.SH", index=result.index)
    result["ts_code"] = codes.fillna("000300.SH").astype(str)
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    return result[["trade_date", "ts_code", "close"]].dropna(subset=["trade_date", "close"]).drop_duplicates()


def _evidence_digest(evidence_dir: Path | None) -> str:
    if evidence_dir is None or not Path(evidence_dir).exists():
        return "none"
    digest = hashlib.sha256()
    for path in sorted(Path(evidence_dir).glob("*")):
        if path.is_file():
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest() if digest.digest_size else "none"


def _raw_digest(raw: Mapping[str, pd.DataFrame]) -> str:
    digest = hashlib.sha256()
    for endpoint in sorted(raw):
        digest.update(endpoint.encode("utf-8"))
        digest.update(_stable_frame_bytes(raw[endpoint]))
    return digest.hexdigest()


def _actions_complete(evidence_dir: Path | None) -> bool:
    if evidence_dir is None:
        return False
    marker = Path(evidence_dir) / "actions_complete.json"
    if not marker.exists():
        return False
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return value is True or (isinstance(value, Mapping) and value.get("actions_complete") is True)


__all__ = [
    "Snapshot",
    "TushareDownloader",
    "load_snapshot",
    "make_demo_snapshot",
    "save_snapshot",
    "validate_snapshot",
]
