"""Auditable, non-executable AI overlay experiment records.

This module deliberately does not rank the full universe or calculate orders.
It stores structured Codex results beside a mechanical Top-20 batch so the four
arms can be compared under identical dates and execution assumptions.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

MODES = ("conservative", "balanced", "aggressive")
ARMS = ("mechanical",) + MODES
ALLOWED_ACTIONS = frozenset({"keep", "deprioritize", "unknown"})
SCHEMA_VERSION = 1

MODE_POLICY = {
    "conservative": {"risk_policy": "only_verified_material_risk", "unknown_policy": "fallback_mechanical", "allowed_action": "keep|deprioritize|unknown"},
    "balanced": {"risk_policy": "verified_risk_and_stable_signal", "unknown_policy": "fallback_mechanical", "allowed_action": "keep|deprioritize|unknown"},
    "aggressive": {"risk_policy": "verified_risk_plus_limited_rank_signal", "unknown_policy": "fallback_mechanical", "allowed_action": "keep|deprioritize|unknown"},
}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_result(payload: dict[str, Any], *, batch_id: str, as_of: str, batch_codes: set[str] | None = None, evidence_ids: set[str] | None = None, expected_input_hash: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Codex overlay JSON schema_version 无效")
    mode = payload.get("mode")
    if mode not in MODES:
        raise ValueError("mode 必须是 conservative/balanced/aggressive")
    if payload.get("batch_id") != batch_id or payload.get("as_of") != as_of:
        raise ValueError("overlay 必须绑定同一机械冻结批次和日期")
    if not payload.get("model_version") or not payload.get("prompt_version"):
        raise ValueError("必须记录 model_version 和 prompt_version")
    available = payload.get("available_at")
    if not isinstance(available, str) or payload.get("input_hash") is None:
        raise ValueError("必须记录 available_at 和 input_hash")
    try:
        parsed = datetime.fromisoformat(available.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("available_at 必须是 ISO 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("available_at 必须带时区")
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload["input_hash"])) and expected_input_hash is not None:
        raise ValueError("input_hash 必须是冻结输入的 sha256")
    if expected_input_hash is not None and payload["input_hash"] != expected_input_hash:
        raise ValueError("input_hash 与机械冻结输入不一致")
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        raise ValueError("decisions 必须是数组")
    checked = []
    seen_codes = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("ts_code"), str) or not re.fullmatch(r"\d{6}\.(SH|SZ)", row["ts_code"]):
            raise ValueError("每个 decision 必须有 ts_code")
        code = row["ts_code"]
        if code in seen_codes:
            raise ValueError("decisions 不得包含重复代码")
        if batch_codes is not None and code not in batch_codes:
            raise ValueError("decision 代码不属于机械 Top20")
        ids = row.get("evidence_ids", [])
        if not isinstance(ids, list) or any(not isinstance(x, str) or (evidence_ids is not None and x not in evidence_ids) for x in ids):
            raise ValueError("evidence_ids 必须来自已冻结证据")
        seen_codes.add(code)
        if row.get("allowed_action") not in ALLOWED_ACTIONS:
            raise ValueError("allowed_action 不允许改变仓位或下单")
        forbidden = {"quantity", "amount", "stop_price", "cash", "position_size", "leverage", "buy_price", "sell_price"}
        if any(row.get(key) is not None for key in forbidden):
            raise ValueError("overlay 不得输出 quantity/amount/stop_price")
        if row.get("action") is not None:
            raise ValueError("overlay 不得输出 buy")
        checked.append({"ts_code": code, "allowed_action": row["allowed_action"], "reason": str(row.get("reason", "")), "evidence_ids": ids})
    result = dict(payload)
    result["decisions"] = checked
    result["rules"] = MODE_POLICY[mode]
    result["output_hash"] = _hash({"batch_id": batch_id, "as_of": as_of, "mode": mode, "rules": result["rules"], "model_version": result["model_version"], "prompt_version": result["prompt_version"], "available_at": available, "input_hash": result["input_hash"], "decisions": checked})
    result["mechanical_fallback"] = True
    result["trading_weight"] = 0
    return result


class OverlayLedger:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS overlay_results (result_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, mode TEXT NOT NULL, input_hash TEXT NOT NULL, output_hash TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(batch_id, mode, input_hash))")
        self.db.commit()

    def close(self):
        self.db.close()

    def record(self, payload: dict[str, Any], *, batch_id: str, as_of: str, batch_codes: set[str] | None = None, evidence_ids: set[str] | None = None, expected_input_hash: str | None = None) -> dict[str, Any]:
        result = validate_result(payload, batch_id=batch_id, as_of=as_of, batch_codes=batch_codes, evidence_ids=evidence_ids, expected_input_hash=expected_input_hash)
        rid = _hash({"batch_id": batch_id, "mode": result["mode"], "input_hash": result["input_hash"]})
        existing = self.db.execute("SELECT output_hash,payload_json FROM overlay_results WHERE batch_id=? AND mode=? AND input_hash=?", (batch_id, result["mode"], result["input_hash"])).fetchone()
        if existing:
            if existing[0] != result["output_hash"]:
                raise ValueError("同一实验键已存在不同 output_hash，拒绝覆盖")
            result = json.loads(existing[1])
            result["result_id"] = rid
            return result
        self.db.execute("INSERT INTO overlay_results VALUES (?,?,?,?,?,?,?)", (rid, batch_id, result["mode"], result["input_hash"], result["output_hash"], json.dumps(result, ensure_ascii=False, sort_keys=True), datetime.now().astimezone().isoformat()))
        self.db.commit()
        result["result_id"] = rid
        return result
