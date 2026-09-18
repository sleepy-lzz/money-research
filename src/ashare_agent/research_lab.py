"""Immutable two-layer research book, isolated from legacy batches and all accounts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, time
from pathlib import Path

import pandas as pd

from .current_screen import screen_rule_hash
from .forward_lab import SHANGHAI, ForwardLab, _check_calendar_sources, _dt, _rules_manifest
from .research_protocol import (
    K, MODE_RULES, MODES, PROMPT, PROMPT_VERSION, PROTOCOL, VERSION, build_arms,
    canonical, digest, portfolio, result_schema, select_ai, strict_loads,
    usable_evidence, validate_review,
)
from .session_calendar import next_sessions, sessions


def code_identity():
    names = ("research_lab.py", "research_protocol.py", "research_statistics.py", "current_screen.py", "current_data.py", "forward_lab.py", "session_calendar.py", "context_feed.py", "daily_lab.py", "research_commands.py")
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}


class ResearchLab(ForwardLab):
    def __init__(self, project_root, *, demo=False, clock=None, modes=None):
        if clock is not None and not demo:
            raise ValueError("test_clock_allowed_only_in_demo_namespace")
        self.project_root = Path(project_root).resolve()
        self.demo = bool(demo)
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        folder = self.project_root / "runtime" / ("research-v2-demo" if demo else "research-v2")
        super().__init__(folder / "observations")
        self.folder = folder
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS research_registry(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_packets(batch_id TEXT PRIMARY KEY, source_digest TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_submissions(batch_id TEXT NOT NULL, mode TEXT NOT NULL,
                digest TEXT NOT NULL, received_at TEXT NOT NULL, body TEXT NOT NULL, arm TEXT NOT NULL,
                PRIMARY KEY(batch_id,mode));
            CREATE TABLE IF NOT EXISTS research_attempts(id INTEGER PRIMARY KEY, batch_id TEXT, mode TEXT,
                received_at TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_retrospective(id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL,
                mode TEXT NOT NULL, received_at TEXT NOT NULL, digest TEXT NOT NULL UNIQUE, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_claims(batch_id TEXT NOT NULL, mode TEXT NOT NULL,
                claimed_at TEXT NOT NULL, PRIMARY KEY(batch_id,mode));
            CREATE TABLE IF NOT EXISTS research_benchmarks(id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL,
                horizon INTEGER NOT NULL, observed_at TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
                UNIQUE(batch_id,horizon,digest));
            CREATE TABLE IF NOT EXISTS research_runs(id INTEGER PRIMARY KEY, received_at TEXT NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
        """)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT body FROM research_registry WHERE id=1").fetchone()
            if old:
                self.registry = strict_loads(old["body"])
                identity = dict(self.registry)
                stored = identity.pop("study_hash", None)
                if digest(identity) != stored:
                    raise ValueError("registry_hash_mismatch")
                if modes is not None and list(modes) != self.registry["enabled_modes"]:
                    raise ValueError("mode_set_is_preregistered; create_a_separate_study_in_a_new_project_copy")
                if self.registry["code_identity"] != code_identity() or self.registry["protocol"] != PROTOCOL:
                    raise ValueError("research_rules_changed; preserve_old_study_and_use_a_new_project_copy")
                if self.registry["synthetic"] != self.demo:
                    raise ValueError("synthetic_namespace_mismatch")
            else:
                enabled = list(modes) if modes is not None else list(MODES)
                if not enabled or len(enabled) != len(set(enabled)) or any(mode not in MODES for mode in enabled):
                    raise ValueError("enable_one_to_three_unique_modes_before_first_freeze")
                self.registry = dict(protocol=deepcopy(PROTOCOL), enabled_modes=enabled,
                                     registered_at=self.now().isoformat(), code_identity=code_identity(), synthetic=self.demo)
                self.registry["study_hash"] = digest(self.registry)
                self.db.execute("INSERT INTO research_registry VALUES(1,?)", (canonical(self.registry),))
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.close()
            raise
        self._evaluation_cache = {}

    def now(self):
        return _dt(self.clock(), "local_clock")

    def event(self, kind, status, body):
        with self.db:
            self.db.execute("INSERT INTO research_runs(received_at,kind,status,body) VALUES(?,?,?,?)",
                            (self.now().isoformat(), kind, status, canonical(body)))

    def batch(self, batch_id):
        row = self.db.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise ValueError("frozen_batch_not_found")
        return row

    def packet(self, batch_id):
        batch = self.batch(batch_id)
        row = self.db.execute("SELECT source_digest,body FROM research_packets WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise ValueError("immutable_input_packet_missing")
        packet = strict_loads(row["body"])
        check = dict(packet)
        ident = check.pop("input_hash")
        if digest(check) != ident:
            raise ValueError("stored_packet_hash_mismatch")
        if packet["batch_id"] != batch_id or packet["input_hash"] != batch["input_hash"] or packet["frozen_at"] != batch["created_at"]:
            raise ValueError("stored_packet_batch_binding_mismatch")
        selection = strict_loads(batch["selection_json"])
        evidence = strict_loads(batch["evidence_json"])
        source = digest({"selection": selection, "evidence": evidence, "study_hash": self.registry["study_hash"]})
        if source != row["source_digest"] or source != packet["input_source_hash"]:
            raise ValueError("stored_freeze_source_hash_mismatch")
        _, expected_arms = build_arms(selection)
        if strict_loads(batch["arms_json"]) != expected_arms:
            raise ValueError("stored_mechanical_arms_mismatch")
        return packet

    def latest_batch(self):
        row = self.db.execute("SELECT batch_id FROM batches ORDER BY as_of DESC LIMIT 1").fetchone()
        if row is None:
            raise ValueError("no_v2_freeze; legacy_Top20_is_not_a_v2_sample")
        return row[0]

    def phase(self, as_of, calendar):
        anchor = _dt(self.registry["registered_at"], "registered_at").date().isoformat()
        ordinal = len(sessions(anchor, as_of, calendar))
        for label in ("development", "embargo1", "validation", "embargo2", "final"):
            lo, hi = PROTOCOL["phases"][label]
            if lo <= ordinal <= hi:
                return dict(name=label, session_ordinal=ordinal,
                            full_window_inside_stage=ordinal + 20 <= hi and not label.startswith("embargo"))
        return dict(name="after_registered_final" if ordinal > 320 else "before_registration",
                    session_ordinal=ordinal, full_window_inside_stage=False)

    def freeze(self, selection, evidence=None):
        """No caller-supplied freeze time: production uses the local program clock."""
        now = self.now()
        evidence = list(evidence or [])
        try:
            if not isinstance(selection, dict) or selection.get("synthetic") is not self.demo:
                raise ValueError("synthetic_or_unlabelled_input_rejected_from_this_namespace")
            day = now.date().isoformat()
            if selection.get("as_of") != day or selection.get("status") not in {"complete", "partial"}:
                raise ValueError("only_today_verified_selection_can_be_frozen")
            if not 16 <= now.hour < 20:
                raise ValueError("freeze_outside_preregistered_16_to_20_window")
            if selection.get("source_hash") != screen_rule_hash(selection.get("calendar_manifest")):
                raise ValueError("selection_source_code_hash_mismatch")
            params = selection.get("parameters", {})
            expected_params = dict(top=20, min_amount=PROTOCOL["pool"]["min_amount20"], momentum=[60,120], trend=120)
            if any(params.get(key) != value for key, value in expected_params.items()):
                raise ValueError("selection_parameters_outside_fixed_protocol")
            if params.get("symbols") or params.get("universe_policy", "sina_current_mainboard_directory") != "sina_current_mainboard_directory":
                raise ValueError("custom_universe_not_allowed_in_registered_study")
            start, end = (_dt(selection.get(k), k) for k in ("started_at", "completed_at"))
            if not start <= end <= now or start.date().isoformat() != day or start.hour < 16:
                raise ValueError("selection_time_chain_invalid")
            self._check_receipts(selection.get("receipts"), now)
            _check_calendar_sources(selection["calendar_manifest"], now)
            legacy_manifest = _rules_manifest(selection)
            future = next_sessions(day, 20, selection["calendar_manifest"])
            deadline = datetime.combine(now.date(), time(20), SHANGHAI)
            entry = _dt(future[0] + "T09:30:00+08:00", "evaluation_start")
            if not deadline < entry:
                raise ValueError("submission_deadline_must_precede_evaluation")
            candidates, arms = build_arms(selection)
            source_digest = digest({"selection": selection, "evidence": evidence, "study_hash": self.registry["study_hash"]})
            filtered, quarantined, omitted = usable_evidence(evidence, {r["ts_code"] for r in candidates}, now)
            batch_id = digest({"source_digest": source_digest, "as_of": day})[:32]
            packet = dict(schema_version=2, batch_id=batch_id, as_of=day,
                          frozen_at=now.isoformat(), data_cutoff=day + "T15:00:00+08:00",
                          source_completed_at=selection["completed_at"], received_by_freezer_at=now.isoformat(),
                          submission_deadline=deadline.isoformat(), evaluation_start=entry.isoformat(),
                          input_source_hash=source_digest, study_hash=self.registry["study_hash"],
                          strategy_config=deepcopy(PROTOCOL), source_rules=legacy_manifest,
                          code_identity=self.registry["code_identity"],
                          enabled_modes=self.registry["enabled_modes"], mode_rules=MODE_RULES,
                          candidates=candidates, common_pool_hash=digest(selection["research_universe"]),
                          common_pool_count=len(selection["research_universe"]["rows"]),
                          evidence=filtered, evidence_quarantine=quarantined, evidence_budget_omissions=omitted,
                          prompt=PROMPT, prompt_version=PROMPT_VERSION,
                          prompt_hash=hashlib.sha256(PROMPT.encode()).hexdigest(),
                          model_at_freeze={"reported_name": None, "exact_version": None,
                                           "identity_source": "unavailable", "sampling_control": "unavailable"},
                          phase=self.phase(day, selection["calendar_manifest"]),
                          synthetic=self.demo, source_receipts=selection["receipts"],
                          benchmark_diagnostics=selection.get("benchmark_diagnostics", {"regime": "unknown"}))
            packet["market_data_cutoff"] = packet["data_cutoff"]
            packet["evidence_cutoff"] = packet["frozen_at"]
            packet["prompt_template_hash"] = packet["prompt_hash"]
            packet["mode_prompts"] = {m: self._mode_prompt(packet, m) for m in packet["enabled_modes"]}
            packet["mode_prompt_hashes"] = {m: hashlib.sha256(text.encode()).hexdigest() for m,text in packet["mode_prompts"].items()}
            packet["prompt_hash"] = digest(packet["mode_prompts"])
            packet["input_hash"] = digest(packet)
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT * FROM batches WHERE as_of=?", (day,)).fetchone()
            if old:
                old_digest = self.db.execute("SELECT source_digest FROM research_packets WHERE batch_id=?", (old["batch_id"],)).fetchone()[0]
                if old_digest != source_digest:
                    raise ValueError("freeze_conflict_first_valid_batch_is_immutable")
                self.db.rollback()
                return dict(status="already_frozen_identical", batch_id=old["batch_id"], input_hash=old["input_hash"], synthetic=self.demo)
            manifest = dict(version=VERSION, study_hash=self.registry["study_hash"], protocol=PROTOCOL,
                            enabled_modes=self.registry["enabled_modes"], phase=packet["phase"], synthetic=self.demo)
            self.db.execute("INSERT INTO batches(batch_id,as_of,created_at,input_hash,rules_hash,selection_json,arms_json,evidence_json,rules_manifest_json) VALUES(?,?,?,?,?,?,?,?,?)",
                            (batch_id, day, now.isoformat(), packet["input_hash"], self.registry["study_hash"],
                             canonical(selection), canonical(arms), canonical(evidence), canonical(manifest)))
            self.db.execute("INSERT INTO research_packets VALUES(?,?,?)", (batch_id, source_digest, canonical(packet)))
            self.db.commit()
            self.event("freeze", "accepted", dict(batch_id=batch_id, eligible_count=packet["common_pool_count"], ai_evidence_quarantine=len(quarantined)))
            return dict(status="frozen", batch_id=batch_id, input_hash=packet["input_hash"], synthetic=self.demo)
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            self.db.rollback()
            self.event("freeze", "rejected", dict(reason=str(exc)))
            raise

    def _attempt(self, batch_id, modes, when, value_hash, status, reason):
        with self.db:
            for mode in modes or [None]:
                self.db.execute("INSERT INTO research_attempts(batch_id,mode,received_at,digest,status,reason) VALUES(?,?,?,?,?,?)",
                                (batch_id, mode, when, value_hash, status, reason))

    def submit_text(self, batch_id, text, *, retrospective=False, validate_only=False):
        received = self.now()  # captured before parsing, not from model metadata
        try:
            if not isinstance(text, str) or len(text.encode("utf-8")) > 1_000_000:
                raise ValueError("result_too_large_or_not_text")
            values = strict_loads(text)
        except (ValueError, TypeError) as exc:
            h = hashlib.sha256(str(text).encode()).hexdigest()
            self._attempt(batch_id, [], received.isoformat(), h, "invalid_json", str(exc))
            raise ValueError("invalid_json:" + str(exc)) from exc
        return self._submit(batch_id, values, received, retrospective=retrospective, validate_only=validate_only)

    def submit(self, batch_id, values, *, retrospective=False, validate_only=False):
        return self.submit_text(batch_id, canonical(values), retrospective=retrospective, validate_only=validate_only)

    def _submit(self, batch_id, values, received, *, retrospective, validate_only):
        values = values if isinstance(values, list) else [values]
        supplied_modes = [v.get("mode") for v in values if isinstance(v, dict) and v.get("mode") in MODES]
        value_hash = digest(values)
        prepared, seen = [], set()
        try:
            packet = self.packet(batch_id)
            if not packet["candidates"]:
                raise ValueError("ai_experiment_not_started_empty_mechanical_pool")
            if not 1 <= len(values) <= len(packet["enabled_modes"]):
                raise ValueError("invalid_mode_count")
            self.db.execute("BEGIN IMMEDIATE")
            for value in values:
                checked = validate_review(value, packet)
                mode = checked["mode"]
                if mode in seen:
                    raise ValueError("duplicate_mode_in_submission")
                seen.add(mode)
                h = digest(checked)
                old = self.db.execute("SELECT * FROM research_submissions WHERE batch_id=? AND mode=?", (batch_id, mode)).fetchone()
                if old and not retrospective:
                    if old["digest"] != h:
                        raise ValueError("conflict_import_first_valid_submission_is_immutable")
                    prepared.append((mode, h, checked, strict_loads(old["arm"]), old["received_at"], True))
                    continue  # identical retries remain idempotent even after deadline
                generated = _dt(checked["generated_at"], "generated_at")
                if not _dt(packet["frozen_at"], "frozen_at") <= generated <= received:
                    raise ValueError("generated_time_outside_freeze_to_local_receipt")
                if not retrospective and received >= _dt(packet["submission_deadline"], "submission_deadline"):
                    raise ValueError("late_submission_rejected; use_explicit_retrospective_archive")
                arm = select_ai(packet, checked)
                prepared.append((mode, h, checked, arm, received.isoformat(), False))
            if validate_only:
                self.db.rollback()
            else:
                for mode, h, checked, arm, timestamp, duplicate in prepared:
                    if retrospective:
                        self.db.execute("INSERT OR IGNORE INTO research_retrospective(batch_id,mode,received_at,digest,body) VALUES(?,?,?,?,?)",
                                        (batch_id, mode, received.isoformat(), h, canonical(checked)))
                    elif not duplicate:
                        self.db.execute("INSERT INTO research_submissions VALUES(?,?,?,?,?,?)",
                                        (batch_id, mode, h, timestamp, canonical(checked), canonical(arm)))
                self.db.commit()
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            self.db.rollback()
            self._attempt(batch_id, supplied_modes, received.isoformat(), value_hash, "rejected", str(exc))
            raise ValueError(str(exc)) from exc
        status = "validated_not_submitted" if validate_only else "retrospective_only" if retrospective else "accepted"
        self._attempt(batch_id, sorted(seen), received.isoformat(), value_hash, status, "")
        return dict(status=status, batch_id=batch_id, modes=[dict(mode=m, received_at=t, idempotent=dup) for m, _, _, _, t, dup in prepared],
                    prospective=not retrospective and not validate_only, synthetic=self.demo,
                    no_fills=True, no_orders=True)

    def claim(self, batch_id, mode):
        packet = self.packet(batch_id)
        if mode not in packet["enabled_modes"]:
            raise ValueError("mode_not_preregistered")
        now = self.now()
        if now >= _dt(packet["submission_deadline"], "deadline"):
            raise ValueError("late_dispatch_not_allowed")
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO research_claims VALUES(?,?,?)", (batch_id, mode, now.isoformat()))
        return dict(status="claimed" if cursor.rowcount == 1 else "already_claimed_do_not_call_model_again",
                    batch_id=batch_id, mode=mode)

    def _mode_prompt(self, packet, mode):
        folder = self.folder / "outbox" / packet["batch_id"]
        text = PROMPT + f"\n本次仅处理 mode={mode}。项目根目录：{self.project_root}\n输入包：{folder / 'packet.json'}\nJSON Schema：{folder / (mode + '-result-schema.json')}\n"
        text += f"先运行一次：python -m ashare_agent research-claim --batch-id {packet['batch_id']} --mode {mode}\n"
        text += "仅 status=claimed 时开始模型判断；already_claimed 时保留已产生结果，禁止再次生成。\n"
        text += f"结果写至 {folder / 'results' / (mode + '.json')}\n"
        text += f"完成后在项目根目录运行：python -m ashare_agent research-import --batch-id {packet['batch_id']} --input \"{folder / 'results' / (mode + '.json')}\"\n"
        if self.demo:
            text = "合成演示，禁止进入真实研究数据库。此演示包不应交给模型做正式前瞻判断。\n" + text
        return text

    def export(self, batch_id=None, mode=None):
        packet = self.packet(batch_id or self.latest_batch())
        modes = [mode] if mode else packet["enabled_modes"]
        if any(m not in packet["enabled_modes"] for m in modes):
            raise ValueError("mode_not_preregistered")
        folder = self.folder / "outbox" / packet["batch_id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "results").mkdir(exist_ok=True)
        def immutable_write(path, text):
            if path.exists():
                if path.read_text(encoding="utf-8") != text:
                    raise ValueError("outbox_file_conflict:" + path.name)
                return
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text)
        immutable_write(folder / "packet.json", canonical(packet))
        for m in modes:
            schema = result_schema(packet, m)
            immutable_write(folder / f"{m}-result-schema.json", canonical(schema))
            text = packet["mode_prompts"][m]
            immutable_write(folder / f"{m}-prompt.txt", text)
        self.event("export", "complete", dict(batch_id=packet["batch_id"], modes=modes))
        return dict(status="exported", batch_id=packet["batch_id"], folder=str(folder), modes=modes,
                    deadline=packet["submission_deadline"], synthetic=self.demo,
                    automation="files_ready; model_dispatch_and_local_permissions_require_actual_Codex_run")

    def _effective_arms(self, batch):
        arms = strict_loads(batch["arms_json"])
        packet = self.packet(batch["batch_id"])
        for mode in packet["enabled_modes"]:
            row = self.db.execute("SELECT * FROM research_submissions WHERE batch_id=? AND mode=?", (batch["batch_id"], mode)).fetchone()
            if row:
                arm = strict_loads(row["arm"])
                effective = arm["effective_judgments"]
                arm.update(ai_status="valid" if effective else "valid_all_unknown", valid_ai_submission=True,
                           effective_ai_batch=effective > 0, fallback=effective == 0,
                           fallback_reason="all_judgments_unknown" if not effective else None,
                           received_at=row["received_at"], output_hash=row["digest"],
                           model_identity=strict_loads(row["body"])["model_identity"])
            else:
                arm = portfolio(packet["candidates"][:K], rule="preregistered_mechanical_fallback")
                issue = self.db.execute("SELECT status,reason FROM research_attempts WHERE batch_id=? AND (mode=? OR mode IS NULL) AND status IN ('rejected','invalid_json') ORDER BY id DESC LIMIT 1",
                                        (batch["batch_id"], mode)).fetchone()
                reason = issue["reason"] if issue else ("deadline_timeout_no_submission" if self.now() >= _dt(packet["submission_deadline"], "deadline") else "awaiting_submission")
                if not packet["candidates"]:
                    reason = "not_started_no_mechanical_candidates"
                arm.update(ai_status="fallback" if self.now() >= _dt(packet["submission_deadline"], "deadline") else "pending",
                           valid_ai_submission=False, effective_ai_batch=False, fallback=True,
                           fallback_reason=reason, effective_judgments=0, unknown_judgments=len(packet["candidates"]))
            claim = self.db.execute("SELECT claimed_at FROM research_claims WHERE batch_id=? AND mode=?", (batch["batch_id"],mode)).fetchone()
            arm["local_dispatch_claimed_at"] = claim[0] if claim else None
            arm["external_model_reruns_verifiable"] = False
            arm["changed_selection"] = (arm["weights"] != arms["mechanical"]["weights"] or arm["cash_weight"] != arms["mechanical"]["cash_weight"])
            denominator = len(packet["candidates"])
            arm["effective_ai_decision_coverage"] = arm["effective_judgments"] / denominator if denominator else None
            arm["decision_fallback_rate"] = arm["unknown_judgments"] / denominator if denominator else None
            arms["ai_" + mode] = arm
        return arms

    def _evaluate(self, batch, arm, item, horizon, as_of, expected_sessions, benchmark, histories):
        key = (batch["batch_id"], item["ts_code"], horizon)
        if key not in self._evaluation_cache:
            self._evaluation_cache[key] = ForwardLab._evaluate(batch, arm, item, horizon, as_of, expected_sessions, benchmark, histories)
        return deepcopy(self._evaluation_cache[key])

    def observe(self, as_of, benchmark, histories, observed_at=None):
        now = self.now()
        if as_of > now.date().isoformat():
            raise ValueError("future_observation_rejected")
        histories = dict(histories)
        invalid = {}
        for code, value in list(histories.items()):
            try:
                for field in ("raw", "qfq"):
                    frame = self._history(value[field], code, field)
                    if frame.index[-1] > as_of:
                        raise ValueError("future_history_not_accepted")
            except (ValueError, KeyError, TypeError) as exc:
                invalid[code] = str(exc)
                del histories[code]
        self._evaluation_cache = {}
        result = super().observe(as_of, benchmark, histories, now.isoformat())
        result["source_errors"] = invalid
        result["synthetic"] = self.demo
        result["price_observation_only"] = True
        normalized = self._benchmark(benchmark)
        for batch in self.db.execute("SELECT * FROM batches").fetchall():
            if batch["as_of"] > as_of:
                continue
            calendar = strict_loads(batch["selection_json"])["calendar_manifest"]
            for horizon in (1, 5, 20):
                try:
                    days = next_sessions(batch["as_of"], horizon, calendar)
                    if days[-1] > as_of:
                        body = dict(status="pending", price_return=None)
                    elif any(day not in normalized.index for day in days):
                        body = dict(status="unknown", price_return=None, reason="missing_benchmark_sessions")
                    else:
                        value = float(normalized.loc[days[-1], "close"] / normalized.loc[days[0], "open"] - 1)
                        body = dict(status="known", price_return=value, entry_day=days[0], exit_day=days[-1])
                except (ValueError, KeyError) as exc:
                    body = dict(status="unknown", price_return=None, reason=str(exc))
                body["source_observation_date"] = as_of
                with self.db:
                    self.db.execute("INSERT OR IGNORE INTO research_benchmarks(batch_id,horizon,observed_at,digest,body) VALUES(?,?,?,?,?)",
                                    (batch["batch_id"], horizon, now.isoformat(), digest(body), canonical(body)))
        self.event("observe", "partial" if invalid else "complete", dict(as_of=as_of, source_errors=invalid))
        self._evaluation_cache = {}
        return result

    def summary(self):
        from .research_statistics import study_statistics, summarize_exposures
        result = super().summary()
        result.pop("ai_overlay", None)  # v1 storage status is not v2 coverage
        result.update(research_version=VERSION, study=self.registry, synthetic=self.demo,
                      account_return_available=False, continuous_simulation_status=PROTOCOL["continuous_account"]["status"],
                      primary_strategy_changed=False, account_data_accessed=False, generated_at=self.now().isoformat())
        previous = {}
        for out in result["batches"]:
            batch = self.batch(out["batch_id"])
            packet = self.packet(out["batch_id"])
            arms = self._effective_arms(batch)
            latest = {}
            for row in self.db.execute("SELECT * FROM observations WHERE batch_id=? ORDER BY revision", (out["batch_id"],)):
                latest[(row["arm"], row["ts_code"], row["horizon"])] = dict(row)
            out.update(phase=packet["phase"], benchmark_diagnostics=packet["benchmark_diagnostics"],
                       evidence_quarantine=packet["evidence_quarantine"], evidence_budget_omissions=len(packet["evidence_budget_omissions"]),
                       candidate_count=len(packet["candidates"]), common_pool_count=packet["common_pool_count"],
                       synthetic=self.demo, submission_deadline=packet["submission_deadline"],
                       ai_deadline_passed=self.now() >= _dt(packet["submission_deadline"], "deadline"))
            selection = strict_loads(batch["selection_json"])
            pool = selection["research_universe"]
            out["universe_data_failure_rate"] = pool["data_failure_count"] / max(1, pool["source_universe_count"])
            for name, arm in arms.items():
                summary = out["arms"][name]
                summary.update({key: value for key, value in arm.items() if key not in {"rows", "codes", "full_ranking"}})
                summary["exposures"] = summarize_exposures(arm, previous.get(name))
                previous[name] = arm
                for horizon in (1, 5, 20):
                    metrics = summary["horizons"][str(horizon)]
                    known = [latest.get((name, code, horizon), {"status": "pending"}) for code in arm["codes"]]
                    end_day = next_sessions(batch["as_of"], horizon, selection["calendar_manifest"])[-1]
                    matured = end_day < self.now().date().isoformat() or (end_day == self.now().date().isoformat() and self.now().hour >= 16)
                    complete = matured and all(row["status"] == "known" for row in known)
                    metrics["full_weight_price_return"] = (sum(arm["weights"][row["ts_code"]] * row["price_return"] for row in known) if complete else None)
                    metrics["full_weight_return_status"] = "known" if complete else "incomplete_do_not_renormalize"
                    metrics["notional_cash_weight"] = arm["cash_weight"]
                    metrics["is_executable_return"] = False
                    b = self.db.execute("SELECT body FROM research_benchmarks WHERE batch_id=? AND horizon=? ORDER BY id DESC LIMIT 1", (out["batch_id"], horizon)).fetchone()
                    benchmark = strict_loads(b[0]) if b else dict(status="pending", price_return=None)
                    metrics["HS300_price_return"] = benchmark["price_return"]
                    metrics["full_weight_minus_HS300"] = metrics["full_weight_price_return"] - benchmark["price_return"] if complete and benchmark["price_return"] is not None else None
                    metrics["alpha"] = None
                summary["observed_price_risk"] = {"D1_executable": False,
                    "continuous_volatility_or_drawdown": None,
                    "reason": "cohort_observations_overlap; no_executable_daily_account_series"}
            mechanical = out["arms"]["mechanical"]["horizons"]
            common = out["arms"]["common_equal_weight"]["horizons"]
            for arm in out["arms"].values():
                for h, metric in arm["horizons"].items():
                    value = metric["full_weight_price_return"]
                    m, c = mechanical[h]["full_weight_price_return"], common[h]["full_weight_price_return"]
                    metric["paired_minus_mechanical"] = value - m if value is not None and m is not None else None
                    metric["paired_minus_common"] = value - c if value is not None and c is not None else None
        result["statistics"] = study_statistics(result, self.db)
        result["attempts"] = [dict(row) for row in self.db.execute("SELECT * FROM research_attempts ORDER BY id")]
        result["run_events"] = [dict(row) | {"body": strict_loads(row["body"])} for row in self.db.execute("SELECT * FROM research_runs ORDER BY id")]
        result["retrospective_count"] = self.db.execute("SELECT COUNT(*) FROM research_retrospective").fetchone()[0]
        return result

    def report(self):
        from .research_statistics import write_research_report
        return write_research_report(self.folder, self.summary())
