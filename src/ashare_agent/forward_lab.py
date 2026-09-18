"""Bounded forward observation experiments for frozen daily selections.

``ForwardLab`` stores research observations only.  A frozen selection is a
signal snapshot, and every later result is a price observation: no fill,
cash, broker, or portfolio return is inferred.  The public methods are:

* ``freeze(selection, created_at, evidence=[])``: validate and persist the
  first valid selection for a Shanghai date, with four fixed single-factor
  arms.
* ``observe(as_of, benchmark, histories, observed_at)``: evaluate all frozen
  batches whose signal date precedes ``as_of`` at 1, 5, and 20 sessions.
* ``summary()``: return batch-separated, revision-aware price observation
  statistics and arm overlap information.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from copy import deepcopy
from datetime import date, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .session_calendar import next_sessions as calendar_next_sessions
from .session_calendar import sessions as calendar_sessions

SHANGHAI = ZoneInfo("Asia/Shanghai")
ARMS = ("mechanical", "momentum60", "momentum120", "news_guard")
HORIZONS = (1, 5, 20)
_RULE_VERSION = "forward-lab-v1"
_EVIDENCE_RULE_VERSION = "context-evidence-v1"
_EXPERIMENT_DEFINITION = {
    "candidate_pool": "original_top20",
    "arms": {
        "mechanical": "original_score_desc",
        "momentum60": "momentum60_desc",
        "momentum120": "momentum120_desc",
        "news_guard": "original_score_desc_excluding_aggregator_news_risk",
    },
    "horizons": [1, 5, 20],
    "entry": "first_benchmark_session_after_signal_open",
    "exit": "nth_benchmark_session_close",
    "qfq_policy": "aligned_dates_and_constant_factor",
    "unknown_policy": "retain_denominator_and_revision",
}
_DYNAMIC_PARAMETER_KEYS = {
    "as_of", "date", "created_at", "started_at", "completed_at", "generated_at", "run_id", "runtime"
}


def _dt(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        try:
            result = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{label} 必须是带时区的 ISO 时间") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{label} 必须带时区")
    return result.astimezone(SHANGHAI)


def _day(value: Any, label: str = "日期") -> str:
    if isinstance(value, datetime):
        return _dt(value, label).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label}格式无效") from exc


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except (ValueError, TypeError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _number(value: Any, label: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是有限数字") from exc
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"{label}必须是有限{('正' if positive else '非负' if nonnegative else '')}数字")
    return result


def _normalise_frame(frame: Any, name: str, required: tuple[str, ...]) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError(f"{name}必须是非空 DataFrame")
    result = frame.copy()
    dates = result["trade_date"] if "trade_date" in result.columns else result.index
    values = [_day(value, f"{name}交易日") for value in dates]
    if len(values) != len(set(values)) or values != sorted(values):
        raise ValueError(f"{name}交易日重复或非递增")
    result.index = values
    result.index.name = "trade_date"
    missing = [column for column in required if column not in result.columns]
    if missing:
        raise ValueError(f"{name}缺少字段：{','.join(missing)}")
    columns_to_check = set(required) | ({"volume"} if "volume" in result.columns else set())
    for column in columns_to_check:
        result[column] = pd.to_numeric(result[column], errors="coerce")
        if not result[column].map(math.isfinite).all():
            raise ValueError(f"{name}.{column}包含非有限值")
        if column != "volume" and (result[column] <= 0).any():
            raise ValueError(f"{name}.{column}必须为正")
    if (result["volume"] < 0).any():
        raise ValueError(f"{name}.volume不能为负")
    return result


def _frame_hash(frame: pd.DataFrame | None) -> str:
    if frame is None:
        return "missing"
    return _hash(
        {
            "columns": list(frame.columns),
            "index": list(frame.index),
            "data": frame.to_json(orient="split", double_precision=15),
        }
    )


def _context_rules_hash() -> str:
    return hashlib.sha256(Path(__file__).with_name("context_feed.py").read_bytes()).hexdigest()


def _code_hash(filename: str) -> str:
    return hashlib.sha256(Path(__file__).with_name(filename).read_bytes()).hexdigest()


def _calendar_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    stable = deepcopy(manifest)
    # Receipt acquisition metadata is retained in selection_json but does not
    # define the experiment rules.  Closure rules and stable source identity
    # remain part of the rules hash.
    transient_source_fields = {
        "fetched_at", "raw_html_path", "download_status", "http_status", "attempts",
    }
    for source in stable.get("sources", []):
        if isinstance(source, dict):
            for field in transient_source_fields:
                source.pop(field, None)
    stable.pop("audit", None)
    return {
        "schema": stable["schema"],
        "revision": stable["revision"],
        "covered_start": stable["covered_start"],
        "covered_end": stable["covered_end"],
        "sources": stable.get("sources", []),
        "rules": stable["rules"],
        "manifest_hash": _hash(stable),
    }


def _check_calendar_sources(calendar: dict[str, Any], created: datetime) -> None:
    sources = calendar.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("calendar_manifest 缺少来源")
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ValueError(f"calendar_manifest.sources[{index}] 格式无效")
        try:
            published_day = _day(source.get("published_at"), f"calendar_manifest.sources[{index}].published_at")
        except ValueError as exc:
            raise ValueError("calendar_manifest 来源发布日期无效") from exc
        if published_day > created.date().isoformat():
            raise ValueError("calendar_manifest 来源发布日期晚于冻结日期")
        fetched = source.get("fetched_at")
        if source.get("download_status") != "complete":
            raise ValueError("calendar_manifest 来源未完成下载")
        if fetched is None:
            raise ValueError("完整 calendar_manifest 来源缺少 fetched_at")
        if not source.get("content_hash"):
            raise ValueError("完整 calendar_manifest 来源缺少 content_hash")
        if not isinstance(source.get("raw_html_path"), str) or not source["raw_html_path"].strip():
            raise ValueError("完整 calendar_manifest 来源缺少 raw_html_path")
        fetched_at = _dt(fetched, f"calendar_manifest.sources[{index}].fetched_at")
        if fetched_at > created:
            raise ValueError("calendar_manifest 来源 fetched_at 晚于冻结时间")


def _rules_manifest(selection: dict[str, Any]) -> dict[str, Any]:
    parameters = selection.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("选股缺少 parameters，无法建立可追溯规则身份")
    stable_parameters = {
        str(key): value
        for key, value in parameters.items()
        if str(key).lower() not in _DYNAMIC_PARAMETER_KEYS
    }
    calendar = selection.get("calendar_manifest")
    if not isinstance(calendar, dict):
        raise ValueError("选股缺少 calendar_manifest，无法绑定交易日历")
    try:
        if calendar_sessions(selection["as_of"], selection["as_of"], calendar) != [selection["as_of"]]:
            raise ValueError("选股 as_of 不是绑定交易日历的开市日")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("calendar_manifest 无效或不覆盖选股 as_of") from exc
    return {
        "manifest_version": _RULE_VERSION,
        "legacy": False,
        "selection_parameters": json.loads(_canonical(stable_parameters)),
        "source_hash": str(selection["source_hash"]),
        "context_rules_hash": _context_rules_hash(),
        "evidence_derivation_version": _EVIDENCE_RULE_VERSION,
        "forward_rules_code_hash": _code_hash("forward_lab.py"),
        "calendar_code_hash": _code_hash("session_calendar.py"),
        "calendar_manifest": _calendar_identity(calendar),
        "experiment_definition": _EXPERIMENT_DEFINITION,
    }


def _rules_hash(manifest: dict[str, Any]) -> str:
    return _hash(manifest)


def _evaluation_rules_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes() + b"forward-observation-v1").hexdigest()


def _calendar_window(calendar: dict[str, Any], signal: str, horizon: int) -> tuple[list[str], str | None]:
    try:
        return calendar_next_sessions(signal, horizon, calendar), None
    except ValueError as exc:
        # Keep the sessions that are actually covered so an out-of-coverage
        # horizon can still report its entry day.  Never fill the remainder
        # from benchmark rows or business-day arithmetic.
        try:
            signal_day = date.fromisoformat(signal)
            start = (signal_day + timedelta(days=1)).isoformat()
            partial = calendar_sessions(start, calendar["covered_end"], calendar)
        except (KeyError, TypeError, ValueError):
            partial = []
        return partial, f"calendar_unknown：{exc}"


class ForwardLab:
    """Persist daily forward experiments without pretending to have fills.

    ``freeze`` returns ``status='frozen'`` on the first valid input and on an
    identical retry.  A different input for an already frozen Shanghai date
    returns ``status='already_frozen'`` and leaves the first record intact.
    ``observe`` returns one row per frozen arm/code/horizon, with status
    ``known``, ``unknown``, or ``pending``.  ``summary`` counts all rows in
    the denominator and reports only known price observations in return
    statistics.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "forward-lab.sqlite", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS batches(
              batch_id TEXT PRIMARY KEY, as_of TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL, input_hash TEXT NOT NULL,
              rules_hash TEXT NOT NULL, selection_json TEXT NOT NULL,
              arms_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
              rules_manifest_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations(
              observation_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
              arm TEXT NOT NULL, ts_code TEXT NOT NULL, horizon INTEGER NOT NULL,
              revision INTEGER NOT NULL, observed_at TEXT NOT NULL,
              status TEXT NOT NULL, signal_date TEXT NOT NULL,
              entry_day TEXT, exit_day TEXT, entry_price REAL, exit_price REAL,
              price_return REAL, benchmark_return REAL, excess_return REAL,
              raw_hash TEXT NOT NULL, qfq_hash TEXT NOT NULL, benchmark_hash TEXT NOT NULL,
              input_fingerprint TEXT NOT NULL, evaluation_rules_hash TEXT NOT NULL, reason TEXT NOT NULL,
              UNIQUE(batch_id, arm, ts_code, horizon, revision)
            );
            CREATE INDEX IF NOT EXISTS observations_key
              ON observations(batch_id, arm, ts_code, horizon, revision);
            """
        )
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(batches)")}
        if "rules_manifest_json" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE batches ADD COLUMN rules_manifest_json TEXT NOT NULL "
                    "DEFAULT '{\"legacy\":true,\"legacy_reason\":\"rules manifest unavailable in legacy batch\"}'"
                )
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(observations)")}
        if "input_fingerprint" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE observations ADD COLUMN input_fingerprint TEXT NOT NULL DEFAULT ''")
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(observations)")}
        if "evaluation_rules_hash" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE observations ADD COLUMN evaluation_rules_hash TEXT NOT NULL DEFAULT ''")

    def close(self) -> None:
        self.db.close()

    def _check_receipts(self, receipts: Any, cutoff: datetime) -> None:
        if not isinstance(receipts, list) or not receipts:
            raise ValueError("来源 receipts 不能为空")
        for receipt in receipts:
            if not isinstance(receipt, dict):
                raise ValueError("来源 receipt 格式无效")
            for field in ("available_at", "fetched_at"):
                stamp = _dt(receipt.get(field), f"receipt.{field}")
                if stamp > cutoff:
                    raise ValueError("来源 receipt 晚于冻结时间")

    @staticmethod
    def _evidence_map(candidates: list[dict[str, Any]], evidence: list[dict[str, Any]], selection: dict[str, Any]):
        by_code: dict[str, list[dict[str, Any]]] = {row["ts_code"]: [] for row in candidates}
        for item in evidence:
            if not isinstance(item, dict) or not item.get("evidence_id"):
                raise ValueError("证据缺少 evidence_id")
            code = str(item.get("ts_code", "")).upper()
            if code in by_code:
                by_code[code].append(item)
        declared = selection.get("coverage", {})
        result = {}
        for code, items in by_code.items():
            coverage = dict(declared.get(code, {})) if isinstance(declared, dict) else {}
            for kind in ("news", "community"):
                if kind not in coverage:
                    coverage[kind] = "available" if any(item.get("kind") == kind for item in items) else "unknown"
            result[code] = (items, coverage)
        return result

    def _arms(self, candidates: list[dict[str, Any]], evidence: list[dict[str, Any]], selection: dict[str, Any]):
        mapped = self._evidence_map(candidates, evidence, selection)
        rows = []
        for row in candidates:
            items, coverage = mapped[row["ts_code"]]
            news = [
                item
                for item in items
                if item.get("kind") == "news" and item.get("quality") == "aggregator_timestamp"
            ]
            risks = sorted({str(flag) for item in news for flag in item.get("risk_flags", [])})
            rows.append(
                {
                    "ts_code": row["ts_code"],
                    "name": str(row.get("name", "")),
                    "rank": row["rank"],
                    "factor_values": {
                        "score": row["score"],
                        "momentum60": row["momentum60"],
                        "momentum120": row["momentum120"],
                    },
                    "source_coverage": coverage,
                    "evidence_ids": sorted(str(item["evidence_id"]) for item in items),
                    "risk_flags": risks,
                    "news_status": coverage.get("news", "unknown"),
                }
            )
        ordered = {
            "mechanical": sorted(rows, key=lambda row: (-row["factor_values"]["score"], row["ts_code"])),
            "momentum60": sorted(rows, key=lambda row: (-row["factor_values"]["momentum60"], row["ts_code"])),
            "momentum120": sorted(rows, key=lambda row: (-row["factor_values"]["momentum120"], row["ts_code"])),
            "news_guard": sorted(
                (row for row in rows if not row["risk_flags"]),
                key=lambda row: (-row["factor_values"]["score"], row["ts_code"]),
            ),
        }
        return {
            arm: {"codes": [row["ts_code"] for row in values[:5]], "rows": values[:5]}
            for arm, values in ordered.items()
        }

    def freeze(self, selection: dict[str, Any], created_at: datetime, evidence: list[dict[str, Any]] | None = None):
        if not isinstance(selection, dict):
            raise ValueError("selection 必须是对象")
        created = _dt(created_at, "created_at")
        if created.hour < 16:
            raise ValueError("冻结必须在上海时间 16:00 后")
        as_of = _day(selection.get("as_of"), "selection.as_of")
        if as_of != created.date().isoformat():
            raise ValueError("只能冻结 created_at 当日的选股")
        if selection.get("status") not in {"complete", "partial"} or selection.get("synthetic"):
            raise ValueError("只能冻结真实 complete/partial 选股")
        if not selection.get("source_hash"):
            raise ValueError("选股缺少 source_hash")
        started = _dt(selection.get("started_at"), "started_at")
        completed = _dt(selection.get("completed_at"), "completed_at")
        for stamp, label in ((started, "started_at"), (completed, "completed_at")):
            if stamp.date().isoformat() != as_of or stamp.hour < 16:
                raise ValueError(f"{label} 必须是当日 16:00 后的带时区时间")
        if started > completed or completed > created:
            raise ValueError("选股完成时间必须不晚于冻结时间")
        calendar = selection.get("calendar_manifest")
        if not isinstance(calendar, dict):
            raise ValueError("选股缺少 calendar_manifest，无法绑定交易日历")
        _check_calendar_sources(calendar, created)
        self._check_receipts(selection.get("receipts"), created)
        raw_candidates = selection.get("candidates")
        if not isinstance(raw_candidates, list):
            raise ValueError("选股 candidates 必须是列表")
        candidates = []
        seen = set()
        for index, source in enumerate(raw_candidates[:20]):
            if not isinstance(source, dict):
                raise ValueError("候选格式无效")
            code = str(source.get("ts_code", "")).upper()
            if not code or code in seen:
                raise ValueError("候选代码缺失或重复")
            seen.add(code)
            factors = {}
            for field in ("score", "momentum60", "momentum120"):
                factors[field] = _number(source.get(field), f"{code}.{field}")
            candidates.append(
                {
                    "ts_code": code,
                    "name": str(source.get("name", "")),
                    "rank": int(source.get("rank", index + 1)),
                    **factors,
                }
            )
        evidence = list(evidence or [])
        for item in evidence:
            if not isinstance(item, dict) or not item.get("evidence_id"):
                raise ValueError("证据缺少 evidence_id")
            for field in ("published_at", "available_at", "fetched_at"):
                stamp = _dt(item.get(field), f"evidence.{field}")
                if stamp > created:
                    raise ValueError("证据时间晚于冻结时间")
        rules_manifest = _rules_manifest(selection)
        rules_hash = _rules_hash(rules_manifest)
        input_hash = _hash({"selection": selection, "evidence": evidence})
        previous = self.db.execute("SELECT * FROM batches WHERE as_of=?", (as_of,)).fetchone()
        if previous:
            same = previous["input_hash"] == input_hash and previous["rules_hash"] == rules_hash
            try:
                previous_manifest = json.loads(previous["rules_manifest_json"])
            except (TypeError, ValueError):
                previous_manifest = {"legacy": True, "legacy_reason": "rules manifest unreadable"}
            return {
                "status": "frozen" if same else "already_frozen",
                "batch_id": previous["batch_id"],
                "as_of": as_of,
                "rules_hash": previous["rules_hash"],
                "rules_manifest": previous_manifest,
                "idempotent": same,
            }
        arms = self._arms(candidates, evidence, selection)
        batch_id = _hash({"as_of": as_of, "input_hash": input_hash, "rules_hash": rules_hash})[:32]
        with self.db:
            self.db.execute(
                "INSERT INTO batches(batch_id,as_of,created_at,input_hash,rules_hash,selection_json,arms_json,evidence_json,rules_manifest_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    as_of,
                    created.isoformat(),
                    input_hash,
                    rules_hash,
                    _canonical(selection),
                    _canonical(arms),
                    _canonical(evidence),
                    _canonical(rules_manifest),
                ),
            )
        return {
            "status": "frozen", "batch_id": batch_id, "as_of": as_of,
            "rules_hash": rules_hash, "rules_manifest": rules_manifest, "idempotent": False,
        }

    @staticmethod
    def _benchmark(frame: pd.DataFrame) -> pd.DataFrame:
        return _normalise_frame(frame, "benchmark", ("open", "close"))

    @staticmethod
    def _history(value: Any, code: str, field: str) -> pd.DataFrame:
        return _normalise_frame(value, f"{code}.{field}", ("open", "close", "high", "low", "volume"))

    @staticmethod
    def _batch_calendar(batch: sqlite3.Row) -> dict[str, Any] | None:
        try:
            selection = json.loads(batch["selection_json"])
            manifest = selection.get("calendar_manifest")
            if not isinstance(manifest, dict):
                return None
            if calendar_sessions(batch["as_of"], batch["as_of"], manifest) != [batch["as_of"]]:
                return None
            return manifest
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None

    @staticmethod
    def _evaluate(
        batch: sqlite3.Row,
        arm: str,
        item: dict[str, Any],
        horizon: int,
        as_of: str,
        expected_sessions: list[str],
        benchmark: pd.DataFrame,
        histories: dict[str, Any],
    ):
        code = item["ts_code"]
        if len(expected_sessions) < horizon:
            return dict(status="unknown", reason="交易日历未提供完整持有窗口")
        entry_day, exit_day = expected_sessions[0], expected_sessions[horizon - 1]
        if exit_day > as_of:
            return dict(status="pending", reason="持有窗口尚未完成", entry_day=entry_day, exit_day=exit_day)
        missing_benchmark = [day for day in expected_sessions[:horizon] if day not in benchmark.index]
        if missing_benchmark:
            return dict(
                status="unknown", reason="benchmark 缺少日历预期交易日：" + ",".join(missing_benchmark),
                entry_day=entry_day, exit_day=exit_day,
            )
        value = histories.get(code)
        if not isinstance(value, dict) or "raw" not in value or "qfq" not in value:
            return dict(status="unknown", reason="缺少 raw/qfq 历史", entry_day=entry_day, exit_day=exit_day)
        raw_hash = qfq_hash = _frame_hash(None)
        try:
            raw = ForwardLab._history(value["raw"], code, "raw")
            raw_hash = _frame_hash(raw)
            qfq = ForwardLab._history(value["qfq"], code, "qfq")
            qfq_hash = _frame_hash(qfq)
            if set(raw.index) != set(qfq.index):
                raise ValueError("raw/qfq 日期不对齐")
            needed = expected_sessions[:horizon]
            if any(day not in raw.index for day in needed):
                raise ValueError("历史缺少完整观察窗口")
            window = raw.loc[needed]
            if (window.volume <= 0).any():
                raise ValueError("观察窗口存在停牌或无成交日")
            factors = qfq.loc[needed, "close"] / raw.loc[needed, "close"]
            if factors.min() <= 0 or (factors.max() / factors.min() - 1) > 0.002:
                raise ValueError("观察期间复权比例发生变化")
            entry_price = _number(raw.loc[entry_day, "open"], "entry_price", positive=True)
            exit_price = _number(raw.loc[exit_day, "close"], "exit_price", positive=True)
            benchmark_entry = _number(benchmark.loc[entry_day, "open"], "benchmark_entry", positive=True)
            benchmark_exit = _number(benchmark.loc[exit_day, "close"], "benchmark_exit", positive=True)
            price_return = exit_price / entry_price - 1
            benchmark_return = benchmark_exit / benchmark_entry - 1
            return dict(
                status="known",
                reason="",
                entry_day=entry_day,
                exit_day=exit_day,
                entry_price=entry_price,
                exit_price=exit_price,
                price_return=price_return,
                benchmark_return=benchmark_return,
                excess_return=price_return - benchmark_return,
                raw_hash=raw_hash,
                qfq_hash=qfq_hash,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return dict(
                status="unknown", reason=str(exc), entry_day=entry_day, exit_day=exit_day,
                raw_hash=raw_hash, qfq_hash=qfq_hash,
            )

    def observe(self, as_of: str, benchmark: pd.DataFrame, histories: dict[str, Any], observed_at: datetime):
        observation_day = _day(as_of, "as_of")
        observed = _dt(observed_at, "observed_at")
        if observed.hour < 16 or observed.date().isoformat() < observation_day:
            raise ValueError("observe 必须在上海时间评价日 16:00 后")
        benchmark = self._benchmark(benchmark)
        if observation_day not in benchmark.index or benchmark.index[-1] != observation_day:
            raise ValueError("as_of 必须是 benchmark 的最后实际观测日")
        batches = self.db.execute("SELECT * FROM batches ORDER BY as_of,batch_id").fetchall()
        if not batches:
            return {"as_of": observation_day, "observed_at": observed.isoformat(), "batches": []}
        result = {"as_of": observation_day, "observed_at": observed.isoformat(), "batches": []}
        benchmark_hash = _frame_hash(benchmark)
        evaluation_rules_hash = _evaluation_rules_hash()
        for batch in batches:
            if observation_day < batch["as_of"]:
                continue
            arms = self._effective_arms(batch)
            watch_codes = self._batch_watch_codes(batch, arms)
            selected_codes = {
                row["ts_code"]
                for payload in arms.values()
                for row in payload.get("rows", [])
            }
            explicit_codes = selected_codes & set(histories)
            if not watch_codes and not explicit_codes:
                continue
            stale_reason = None
            expected_by_horizon: dict[int, list[str]] = {}
            calendar_reasons: dict[int, str] = {}
            calendar = self._batch_calendar(batch)
            if calendar is None:
                stale_reason = "calendar_unknown：冻结记录缺少可验证交易日历 manifest"
            else:
                try:
                    covered_start = date.fromisoformat(calendar["covered_start"])
                    covered_end = date.fromisoformat(calendar["covered_end"])
                    observed_date = date.fromisoformat(observation_day)
                    # A later benchmark observation may occur after this
                    # finite calendar.  Evaluate each frozen window from its
                    # own expected dates; do not invalidate earlier completed
                    # windows merely because the evaluation day is uncovered.
                    if observed_date <= covered_end and (
                        observed_date < covered_start
                        or calendar_sessions(observation_day, observation_day, calendar) != [observation_day]
                    ):
                        stale_reason = "calendar_unknown：评价日不是绑定日历的开市日"
                except ValueError as exc:
                    stale_reason = f"calendar_unknown：{exc}"
                if stale_reason is None:
                    for horizon in HORIZONS:
                        # The window is anchored to the signal date.  The
                        # observation date is only used above to validate the
                        # actual evaluation session.
                        expected, reason = _calendar_window(calendar, batch["as_of"], horizon)
                        expected_by_horizon[horizon] = expected
                        if reason:
                            calendar_reasons[horizon] = reason
            if stale_reason is None and (
                date.fromisoformat(observation_day) - date.fromisoformat(batch["as_of"])
            ).days > 640:
                stale_reason = "冻结信号距评价日超过 640 天"
            records = []
            for arm, payload in arms.items():
                for item in payload["rows"]:
                    for horizon in HORIZONS:
                        existing = self.db.execute(
                            "SELECT * FROM observations WHERE batch_id=? AND arm=? AND ts_code=? AND horizon=? "
                            "ORDER BY revision DESC LIMIT 1",
                            (batch["batch_id"], arm, item["ts_code"], horizon),
                        ).fetchone()
                        horizon_calendar_reason = calendar_reasons.get(horizon)
                        archived_age = stale_reason == "冻结信号距评价日超过 640 天"
                        if existing and existing["status"] == "known" and (
                            archived_age
                            or (
                                not stale_reason
                                and not horizon_calendar_reason
                                and item["ts_code"] not in histories
                            )
                        ):
                            records.append(dict(existing))
                            continue
                        expected_sessions = expected_by_horizon.get(horizon, [])
                        reason = stale_reason or horizon_calendar_reason
                        evaluated = (
                            {
                                "status": "unknown",
                                "reason": reason,
                                "raw_hash": _frame_hash(None),
                                "qfq_hash": _frame_hash(None),
                                "entry_day": expected_sessions[0] if expected_sessions else None,
                                "exit_day": (
                                    expected_sessions[horizon - 1]
                                    if len(expected_sessions) >= horizon
                                    else None
                                ),
                            }
                            if reason
                            else self._evaluate(
                                batch, arm, item, horizon, observation_day, expected_sessions, benchmark, histories
                            )
                        )
                        raw_hash, qfq_hash = evaluated.get("raw_hash", _frame_hash(None)), evaluated.get("qfq_hash", _frame_hash(None))
                        input_fingerprint = _hash(
                            {
                                "batch": batch["batch_id"], "arm": arm, "code": item["ts_code"],
                                "horizon": horizon, "as_of": observation_day,
                                "status": evaluated["status"], "entry": evaluated.get("entry_price"),
                                "exit": evaluated.get("exit_price"), "raw": raw_hash, "qfq": qfq_hash,
                                "benchmark": benchmark_hash, "evaluation_rules": evaluation_rules_hash,
                            }
                        )
                        if existing and existing["input_fingerprint"] == input_fingerprint:
                            records.append(dict(existing))
                            continue
                        key = (batch["batch_id"], arm, item["ts_code"], horizon)
                        revision = self.db.execute(
                            "SELECT coalesce(max(revision),0)+1 FROM observations WHERE batch_id=? AND arm=? AND ts_code=? AND horizon=?",
                            key,
                        ).fetchone()[0]
                        identity = _hash(
                            {
                                "input_fingerprint": input_fingerprint,
                                "batch": batch["batch_id"], "arm": arm,
                                "code": item["ts_code"], "horizon": horizon, "revision": revision,
                            }
                        )
                        body = {
                            "observation_id": identity,
                            "batch_id": batch["batch_id"], "arm": arm, "ts_code": item["ts_code"],
                            "horizon": horizon, "revision": revision, "observed_at": observed.isoformat(),
                            "status": evaluated["status"], "signal_date": batch["as_of"],
                            "entry_day": evaluated.get("entry_day"), "exit_day": evaluated.get("exit_day"),
                            "entry_price": evaluated.get("entry_price"), "exit_price": evaluated.get("exit_price"),
                            "price_return": evaluated.get("price_return"),
                            "benchmark_return": evaluated.get("benchmark_return"),
                            "excess_return": evaluated.get("excess_return"),
                            "raw_hash": raw_hash, "qfq_hash": qfq_hash,
                            "benchmark_hash": benchmark_hash, "input_fingerprint": input_fingerprint,
                            "evaluation_rules_hash": evaluation_rules_hash,
                            "reason": evaluated.get("reason", ""),
                        }
                        with self.db:
                            self.db.execute(
                                "INSERT INTO observations(" + ",".join(body) + ") VALUES(" + ",".join("?" for _ in body) + ")",
                                tuple(body.values()),
                            )
                        records.append(body)
            result["batches"].append({"batch_id": batch["batch_id"], "as_of": batch["as_of"], "observations": records})
        return result

    def _effective_arms(self, batch):
        from .review_center import effective_arms
        return effective_arms(self.db, batch)

    def _batch_watch_codes(self, batch: sqlite3.Row, arms: dict[str, Any] | None = None) -> set[str]:
        arms = arms or self._effective_arms(batch)
        selected = {
            (arm, row["ts_code"])
            for arm, payload in arms.items()
            for row in payload.get("rows", [])
        }
        rows = [
            dict(row)
            for row in self.db.execute("SELECT * FROM observations WHERE batch_id=?", (batch["batch_id"],))
        ]
        latest: dict[tuple[str, str, int], dict[str, Any]] = {}
        for row in rows:
            key = (row["arm"], row["ts_code"], row["horizon"])
            if key not in latest or row["revision"] > latest[key]["revision"]:
                latest[key] = row
        return {
            code
            for arm, code in selected
            if any(
                latest.get((arm, code, horizon)) is None
                or latest[(arm, code, horizon)]["status"] != "known"
                for horizon in HORIZONS
            )
        }

    def watch_codes(self) -> list[str]:
        """Return frozen codes whose 20-session result is not complete.

        A code remains tracked until every latest row for every arm containing
        it is known through horizon 20.  Missing rows and unknown/pending
        revisions remain in the pool so a later observation can recover them.
        """

        batches = self.db.execute("SELECT * FROM batches").fetchall()
        watch: set[str] = set()
        for batch in batches:
            watch.update(self._batch_watch_codes(batch))
        return sorted(watch)

    @staticmethod
    def _metrics(rows: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
        known = [row for row in rows if row["status"] == "known"]
        returns = [row["price_return"] for row in known]
        excess = [row["excess_return"] for row in known]
        average = sum(returns) / len(returns) if returns else None
        return {
            "samples": len(rows), "known": len(known),
            "unknown": sum(row["status"] == "unknown" for row in rows),
            "pending": sum(row["status"] == "pending" for row in rows),
            "avg_price_return": average,
            "avg_excess_return": sum(excess) / len(excess) if excess else None,
        }

    def summary(self) -> dict[str, Any]:
        batches = self.db.execute("SELECT * FROM batches ORDER BY as_of,batch_id").fetchall()
        output = {"price_observation_only": True, "no_fills": True, "alpha_claim": False, "batches": []}
        for batch in batches:
            arms = self._effective_arms(batch)
            try:
                rules_manifest = json.loads(batch["rules_manifest_json"])
            except (TypeError, ValueError):
                rules_manifest = {"legacy": True, "legacy_reason": "rules manifest unreadable"}
            all_rows = [dict(row) for row in self.db.execute("SELECT * FROM observations WHERE batch_id=?", (batch["batch_id"],))]
            latest: dict[tuple[str, str, int], dict[str, Any]] = {}
            for row in all_rows:
                key = (row["arm"], row["ts_code"], row["horizon"])
                if key not in latest or row["revision"] > latest[key]["revision"]:
                    latest[key] = row
            arm_summary = {}
            for arm in arms:
                selected_codes = arms.get(arm, {}).get("codes", [])
                frozen_rows = [
                    {
                        "ts_code": row["ts_code"],
                        "source_coverage": row.get("source_coverage", {}),
                        "news_status": row.get("news_status", "unknown"),
                        "evidence_ids": row.get("evidence_ids", []),
                    }
                    for row in arms.get(arm, {}).get("rows", [])
                ]
                arm_summary[arm] = {
                    "codes": selected_codes,
                    "frozen_rows": frozen_rows,
                    "horizons": {
                        str(horizon): self._metrics(
                            [
                                latest.get((arm, code, horizon), {"status": "pending"})
                                for code in selected_codes
                            ],
                            horizon,
                        )
                        for horizon in HORIZONS
                    },
                }
            overlap = {}
            for left, right in combinations(arms, 2):
                common = sorted(set(arms.get(left, {}).get("codes", [])) & set(arms.get(right, {}).get("codes", [])))
                overlap[f"{left}|{right}"] = {"count": len(common), "codes": common}
            overlay_rows = []
            try:
                overlay_rows = [dict(row) for row in self.db.execute("SELECT mode, output_hash, created_at FROM overlay_results WHERE batch_id=? ORDER BY mode", (batch["batch_id"],))]
            except sqlite3.OperationalError:
                pass
            output["batches"].append(
                {
                    "batch_id": batch["batch_id"], "as_of": batch["as_of"],
                    "created_at": batch["created_at"], "rules_hash": batch["rules_hash"],
                    "rules_manifest": rules_manifest,
                    "legacy_rules": bool(rules_manifest.get("legacy")),
                    "evaluation_rules_hashes": sorted(
                        {row.get("evaluation_rules_hash", "") for row in all_rows if row.get("evaluation_rules_hash")}
                    ) or [_evaluation_rules_hash()],
                    "arms": arm_summary,
                    "latest_unknowns": [
                        {
                            "ts_code": row["ts_code"], "horizon": row["horizon"],
                            "reason": row["reason"], "evaluation_rules_hash": row.get("evaluation_rules_hash", ""),
                        }
                        for row in latest.values() if row["status"] == "unknown"
                    ],
                    "overlap_samples": overlap,
                    "ai_overlay": {
                        "modes": [row["mode"] for row in overlay_rows],
                        "imported": len(overlay_rows),
                        "trading_weight": 0,
                        "status": "imported" if overlay_rows else "absent_mechanical_fallback",
                    },
                }
            )
        return output


__all__ = ["ForwardLab", "ARMS", "HORIZONS", "SHANGHAI"]
