"""Evidence-only research records for the deterministic strategy.

The mechanical default is intentionally useful for reports while making clear
that no model, news search, or investment recommendation was used. HTTP
research is opt-in and bounded; it can never change rankings or portfolio
fields because its response schema forbids those fields.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)


def _parse_aware_timestamp(value: Any, field_name: str) -> datetime:
    """Parse an ISO timestamp and reject date-only or timezone-naive values."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        raise ValueError(f"{field_name} must include an explicit timezone")
    else:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{field_name} is required")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit timezone")
    return parsed


def _canonical_timestamp(value: Any, field_name: str) -> str:
    return _parse_aware_timestamp(value, field_name).isoformat()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _default_cutoff(as_of: str) -> datetime:
    # Asia/Shanghai is UTC+08:00 without daylight-saving transitions.
    return datetime.combine(date.fromisoformat(as_of), time(23, 59, 59), tzinfo=timezone(timedelta(hours=8)))


def _cutoff_text(as_of: str, decision_cutoff: Any | None) -> str:
    if decision_cutoff is None:
        parsed = _default_cutoff(as_of)
    else:
        parsed = _parse_aware_timestamp(decision_cutoff, "decision_cutoff")
    return parsed.isoformat()


class SourceRecord(BaseModel):
    """A provider citation that must be an exact copy of ingested evidence."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: StrictStr = Field(min_length=1)
    url: StrictStr = Field(min_length=1)
    published_at: str
    available_at: str
    fetched_at: str
    content_hash: StrictStr = Field(min_length=64, max_length=64)
    content: StrictStr
    evidence_quality: str
    title: str | None = None

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("evidence url must use http or https")
        return value

    @field_validator("published_at", "available_at", "fetched_at", mode="before")
    @classmethod
    def aware_timestamps(cls, value: Any, info) -> str:
        return _canonical_timestamp(value, info.field_name)

    @field_validator("content_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        text = value.lower()
        if any(character not in "0123456789abcdef" for character in text):
            raise ValueError("content_hash must be hexadecimal SHA-256")
        return text

    @model_validator(mode="after")
    def content_matches_hash(self):
        if _content_hash(self.content) != self.content_hash:
            raise ValueError("content_hash does not match content")
        return self


class EvidenceRecord(SourceRecord):
    """Immutable evidence normalized at the provider ingestion boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str | None = None
    ts_code: str | None = None


def ingest_evidence(evidence: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize evidence and verify its immutable content hash.

    ``text`` is accepted only as an ingestion alias for ``content``. Missing
    IDs are deterministically constructed from immutable fields so downstream
    records always have an auditable evidence identity.
    """

    normalized: list[dict[str, Any]] = []
    for item in evidence or []:
        if not isinstance(item, dict):
            raise ValueError("evidence must be JSON objects")
        data = dict(item)
        if "content" not in data and "text" in data:
            data["content"] = data.pop("text")
        if "content" not in data:
            raise ValueError("evidence content is required")
        if "content_hash" not in data:
            data["content_hash"] = _content_hash(str(data["content"]))
        expected = _content_hash(str(data["content"]))
        if str(data["content_hash"]).lower() != expected:
            raise ValueError("evidence content_hash does not match content")
        if not data.get("evidence_id"):
            identity = json.dumps(
                {
                    "url": data.get("url"),
                    "published_at": data.get("published_at"),
                    "available_at": data.get("available_at"),
                    "fetched_at": data.get("fetched_at"),
                    "content_hash": expected,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            data["evidence_id"] = hashlib.sha256(identity).hexdigest()
        record = EvidenceRecord.model_validate(data)
        published = _parse_aware_timestamp(record.published_at, "published_at")
        available = _parse_aware_timestamp(record.available_at, "available_at")
        fetched = _parse_aware_timestamp(record.fetched_at, "fetched_at")
        if available < published or available < fetched:
            raise ValueError("evidence availability must follow publication and local acquisition")
        normalized.append(record.model_dump(mode="json"))
    by_id = {}
    for item in normalized:
        if item["evidence_id"] in by_id and by_id[item["evidence_id"]] != item:
            raise ValueError("conflicting evidence identity")
        by_id[item["evidence_id"]] = item
    return list(by_id.values())


def capture_evidence(*, url: str, published_at: str, content: str, symbol: str | None = None) -> dict:
    """Ingestion owns local acquisition time; providers cannot backdate this call."""
    now = datetime.now(timezone.utc).isoformat()
    if _parse_aware_timestamp(published_at, "published_at") > _parse_aware_timestamp(now, "now"):
        raise ValueError("future publication")
    return ingest_evidence(
        [
            dict(
                url=url,
                published_at=published_at,
                fetched_at=now,
                available_at=now,
                content=content,
                symbol=symbol,
                evidence_quality="source_supplied",
            )
        ]
    )[0]


class ResearchRecord(BaseModel):
    """Strict, non-executable research output."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    as_of: str
    decision_cutoff: str | None = None
    status: str
    thesis: str
    catalyst: str
    invalidation: str
    risk_flags: list[str] = Field(default_factory=list)
    sources: list[SourceRecord] = Field(default_factory=list)
    evidence_quality: str
    model_version: str
    prompt_version: str


class ResearchProvider(Protocol):
    def research(
        self,
        candidate: dict[str, Any],
        as_of: str,
        evidence: list[dict[str, Any]],
        *,
        decision_cutoff: str | None = None,
    ) -> Any:
        """Return a ResearchRecord or a dictionary accepted by that model."""


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    value = str(value)
    return date.fromisoformat(value[:10]).isoformat()


def _jsonable(value: Any) -> Any:
    if value is None or value.__class__.__name__ in {"NAType", "NaTType"}:
        return None
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    try:
        if value != value:  # NaN
            return None
    except Exception:
        pass
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "to_dict"):
        row = row.to_dict()
    result = _jsonable(dict(row))
    result.pop("_rank", None)
    return result


def _candidate_rows(ranking: Any, candidate_count: int) -> list[dict[str, Any]]:
    if ranking is None:
        return []
    if hasattr(ranking, "iterrows"):
        frame = ranking.copy()
        if "rank" in frame.columns:
            import pandas as pd

            rank = pd.to_numeric(frame["rank"], errors="coerce")
            frame = frame.loc[rank.notna() & (rank <= candidate_count)].copy()
            frame["_rank"] = rank.loc[frame.index]
            frame = frame.sort_values(["_rank", "ts_code"], kind="mergesort")
        return [_row_dict(row) for _, row in frame.iterrows()]
    rows = list(ranking)
    return [_row_dict(row) for row in rows[:candidate_count]]


def _evidence_for(
    evidence: Iterable[dict[str, Any]] | None,
    symbol: str,
    as_of: str,
    decision_cutoff: str | None = None,
) -> list[dict[str, Any]]:
    """Filter validated evidence by symbol and all business timestamps."""

    result: list[dict[str, Any]] = []
    cutoff = _parse_aware_timestamp(decision_cutoff or _cutoff_text(as_of, None), "decision_cutoff")
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        item_symbol = item.get("symbol", item.get("ts_code"))
        if item_symbol is not None and str(item_symbol) != symbol:
            continue
        try:
            published = _parse_aware_timestamp(item["published_at"], "published_at")
            available = _parse_aware_timestamp(item["available_at"], "available_at")
            fetched = _parse_aware_timestamp(item["fetched_at"], "fetched_at")
            if max(published, available, fetched) > cutoff:
                continue
        except (KeyError, TypeError, ValueError):
            # Missing or malformed business time is unusable evidence.
            continue
        result.append(_jsonable(item))
    return result


def _mechanical_record(
    candidate: dict[str, Any], as_of: str, prompt_version: str, decision_cutoff: str
) -> ResearchRecord:
    symbol = str(candidate.get("ts_code", candidate.get("symbol", "")))
    momentum60 = candidate.get("momentum60")
    momentum120 = candidate.get("momentum120")
    score = candidate.get("score")
    thesis = (
        "Mechanical baseline only: ranked by the equal-weight cross-sectional "
        f"percentiles of 60D and 120D adjusted-price momentum (score={score!r}, "
        f"momentum60={momentum60!r}, momentum120={momentum120!r}). No LLM or external evidence was used."
    )
    return ResearchRecord(
        symbol=symbol,
        as_of=as_of,
        decision_cutoff=decision_cutoff,
        status="unavailable",
        thesis=thesis,
        catalyst="No external catalyst was supplied; this record is not a trade instruction.",
        invalidation="The deterministic ranking, data quality, or trend/liquidity guards may change.",
        risk_flags=["research_disabled", "no_external_evidence", "not_a_trade_instruction"],
        sources=[],
        evidence_quality="none",
        model_version="mechanical-disabled",
        prompt_version=prompt_version,
    )


class HTTPResearchProvider:
    """Small generic JSON provider, explicitly opt-in and bounded by budget."""

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 10.0,
        budget: int = 5,
        model_version: str | None = None,
        prompt_version: str = "research-v1",
        client: Any | None = None,
    ) -> None:
        self.api_url = api_url or os.getenv("ASHARE_RESEARCH_API_URL") or os.getenv("RESEARCH_API_URL")
        self.api_key = api_key or os.getenv("ASHARE_RESEARCH_API_KEY") or os.getenv("RESEARCH_API_KEY")
        self.timeout = max(0.1, float(timeout))
        self.budget = max(0, int(budget))
        self.calls = 0
        self.model_version = (
            model_version
            or os.getenv("ASHARE_RESEARCH_MODEL")
            or os.getenv("RESEARCH_MODEL", "http-research-v1")
        )
        self.prompt_version = prompt_version
        self.client = client

    def research(
        self,
        candidate: dict[str, Any],
        as_of: str,
        evidence: list[dict[str, Any]],
        *,
        decision_cutoff: str | None = None,
    ) -> ResearchRecord:
        symbol = str(candidate.get("ts_code", candidate.get("symbol", "")))
        if not self.api_url or not self.api_key:
            raise RuntimeError("research provider is unavailable: API URL/key not configured")
        if self.calls >= self.budget:
            raise RuntimeError("research provider budget exhausted")
        self.calls += 1
        payload = {
            "symbol": symbol,
            "as_of": as_of,
            "decision_cutoff": decision_cutoff or _cutoff_text(as_of, None),
            "factors": candidate,
            "evidence": evidence,
            "output_schema": "ResearchRecord-v1",
            "instruction": "Return evidence-only JSON. Do not include orders, quantities, weights, or execution fields.",
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if self.client is None:
            import httpx

            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self.api_url, json=payload, headers=headers)
        else:
            response = self.client.post(self.api_url, json=payload, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("record"), dict):
            body = body["record"]
        if not isinstance(body, dict):
            raise ValueError("provider response must be a JSON object")
        return ResearchRecord.model_validate(body)


def _cache_key(
    symbol: str,
    as_of: str,
    decision_cutoff: str,
    model_version: str,
    prompt_version: str,
    candidate: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> str:
    # Factors and evidence are inputs to the provider. A same-day update must
    # never read a record generated for a different input set.
    input_hash = hashlib.sha256(
        json.dumps(
            _jsonable({"candidate": candidate or {}, "evidence": evidence or []}),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()[:32]
    raw = "|".join((symbol, as_of, decision_cutoff, model_version, prompt_version, input_hash)).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()[:32]


def _read_cache(path: Path) -> ResearchRecord | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ResearchRecord.model_validate(payload)
    except (OSError, ValueError, TypeError, ValidationError):
        return None


def _write_cache(path: Path, record: ResearchRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _unavailable(
    symbol: str,
    as_of: str,
    prompt_version: str,
    reason: str,
    model_version: str = "research-unavailable",
    decision_cutoff: str | None = None,
) -> ResearchRecord:
    return ResearchRecord(
        symbol=symbol,
        as_of=as_of,
        decision_cutoff=decision_cutoff,
        status="unavailable",
        thesis=f"Research unavailable ({reason}); no claim is made about future returns.",
        catalyst="Unavailable.",
        invalidation="Unavailable; use deterministic strategy guards and primary evidence.",
        risk_flags=["research_unavailable", reason, "not_a_trade_instruction"],
        sources=[],
        evidence_quality="none",
        model_version=model_version,
        prompt_version=prompt_version,
    )


def _validate_record(
    record: ResearchRecord,
    symbol: str,
    as_of: str,
    evidence: list[dict[str, Any]],
    decision_cutoff: str,
) -> ResearchRecord:
    if record.symbol != symbol or _iso(record.as_of) != as_of:
        raise ValueError("provider record symbol/as_of does not match the request")
    if record.decision_cutoff is None:
        record.decision_cutoff = decision_cutoff
    elif _parse_aware_timestamp(record.decision_cutoff, "decision_cutoff") != _parse_aware_timestamp(
        decision_cutoff, "decision_cutoff"
    ):
        raise ValueError("provider record decision_cutoff does not match the request")
    if (
        record.status not in {"unavailable", "disabled", "insufficient_evidence", "no_evidence"}
        and not record.sources
    ):
        raise ValueError("available provider record must cite supplied evidence")
    for source in record.sources:
        timestamps = [source.published_at, source.available_at, source.fetched_at]
        if max(
            _parse_aware_timestamp(item, "source_timestamp") for item in timestamps
        ) > _parse_aware_timestamp(decision_cutoff, "decision_cutoff"):
            raise ValueError("provider returned future-dated evidence")
        matches = [item for item in evidence if item.get("evidence_id") == source.evidence_id]
        if not matches:
            raise ValueError("provider cited a source absent from supplied evidence")
        source_values = source.model_dump(mode="json")
        immutable_fields = (
            "url",
            "published_at",
            "available_at",
            "fetched_at",
            "content_hash",
            "content",
        )
        if any(source_values[field] != matches[0].get(field) for field in immutable_fields):
            raise ValueError("provider changed immutable evidence fields")
    return record


def _call_provider(
    provider: ResearchProvider,
    candidate: dict[str, Any],
    as_of: str,
    evidence: list[dict[str, Any]],
    decision_cutoff: str,
) -> Any:
    method = provider.research
    try:
        parameters = inspect.signature(method).parameters
        accepts_cutoff = "decision_cutoff" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )
    except (TypeError, ValueError):
        accepts_cutoff = False
    if accepts_cutoff:
        return method(deepcopy(candidate), as_of, deepcopy(evidence), decision_cutoff=decision_cutoff)
    # Preserve the small pre-v0.1.1 provider interface for local test/fallback
    # providers; the built-in HTTP provider receives the exact cutoff above.
    return method(deepcopy(candidate), as_of, deepcopy(evidence))


def _validated_evidence(evidence: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Keep usable evidence records while isolating malformed source rows."""

    result: list[dict[str, Any]] = []
    for item in evidence or []:
        try:
            result.extend(ingest_evidence([item]))
        except (TypeError, ValueError, ValidationError):
            continue
    return ingest_evidence(result)


def research_candidates(
    ranking: Any,
    as_of: str,
    config: dict[str, Any] | None,
    cache_dir: Path,
    evidence: list[dict[str, Any]] | None = None,
    *,
    decision_cutoff: str | datetime | None = None,
) -> list[dict[str, Any]]:
    """Return strict records for ranked candidates; never portfolio actions."""

    as_of = _iso(as_of)
    cutoff = _cutoff_text(as_of, decision_cutoff)
    config = dict(config or {})
    candidate_count = int(config.get("candidate_count", config.get("max_candidates", 20)))
    prompt_version = str(config.get("prompt_version", "research-v1"))
    configured_model_version = config.get("model_version")
    model_version = str(configured_model_version) if configured_model_version else "mechanical-disabled"
    provider: ResearchProvider | None = config.get("research_provider")
    research_cfg = config.get("research", {})
    if isinstance(research_cfg, dict) and provider is None and bool(research_cfg.get("enabled", False)):
        provider = HTTPResearchProvider(
            api_url=research_cfg.get("api_url"),
            api_key=research_cfg.get("api_key"),
            timeout=float(research_cfg.get("timeout", 10.0)),
            budget=int(research_cfg.get("budget", 5)),
            model_version=research_cfg.get("model_version") or None,
            prompt_version=str(research_cfg.get("prompt_version", prompt_version)),
        )
    if provider is None and bool(config.get("research_enabled", False)):
        provider = HTTPResearchProvider(
            api_url=config.get("research_api_url"),
            api_key=config.get("research_api_key"),
            timeout=float(config.get("research_timeout", 10.0)),
            budget=int(config.get("research_budget", 5)),
            model_version=config.get("model_version") or None,
            prompt_version=prompt_version,
        )
    if provider is None and bool(config.get("enabled", False)):
        # Settings.research.model_dump() is commonly passed directly by the
        # root engine, so accept its names without requiring a second nesting
        # layer. URL/key remain environment-backed unless explicitly added by
        # a caller.
        provider = HTTPResearchProvider(
            api_url=config.get("api_url"),
            api_key=config.get("api_key"),
            timeout=float(config.get("timeout_seconds", 10.0)),
            budget=int(config.get("max_calls", 5)),
            model_version=config.get("model_version") or None,
            prompt_version=prompt_version,
        )

    records: list[dict[str, Any]] = []
    try:
        validated_evidence = _validated_evidence(evidence)
    except ValueError:
        validated_evidence = []
    for candidate in _candidate_rows(ranking, candidate_count):
        symbol = str(candidate.get("ts_code", candidate.get("symbol", "")))
        candidate_evidence = _evidence_for(validated_evidence, symbol, as_of, cutoff)
        cache_model = (
            getattr(provider, "model_version", model_version) if provider is not None else model_version
        )
        cache_prompt = (
            getattr(provider, "prompt_version", prompt_version) if provider is not None else prompt_version
        )
        cache_path = (
            Path(cache_dir)
            / f"{_cache_key(symbol, as_of, cutoff, str(cache_model), str(cache_prompt), candidate, candidate_evidence)}.json"
        )
        record = _read_cache(cache_path)
        if record is not None and provider is not None:
            try:
                _validate_record(record, symbol, as_of, candidate_evidence, cutoff)
            except (TypeError, ValueError):
                # A stale or externally modified cache is treated as a miss;
                # the provider must satisfy the same evidence checks again.
                record = None
        if record is None:
            if provider is None:
                record = _mechanical_record(candidate, as_of, prompt_version, cutoff)
            elif not candidate_evidence:
                record = _unavailable(
                    symbol, as_of, cache_prompt, "no_valid_evidence", str(cache_model), cutoff
                )
            else:
                try:
                    candidate_result = _call_provider(provider, candidate, as_of, candidate_evidence, cutoff)
                    record = ResearchRecord.model_validate(candidate_result)
                    _validate_record(record, symbol, as_of, candidate_evidence, cutoff)
                except Exception as exc:  # provider failure is an unavailable record
                    reason = type(exc).__name__.lower()
                    record = _unavailable(symbol, as_of, cache_prompt, reason, str(cache_model), cutoff)
            try:
                _write_cache(cache_path, record)
            except OSError:
                # Cache failure cannot make a research record executable.
                pass
        records.append(record.model_dump(mode="json"))
    return records


__all__ = [
    "EvidenceRecord",
    "HTTPResearchProvider",
    "HttpResearchProvider",
    "ResearchRecord",
    "ResearchProvider",
    "SourceRecord",
    "ingest_evidence",
    "research_candidates",
]

# Accept the spelling used by a few lightweight callers while keeping the
# descriptive all-caps name in the public documentation.
HttpResearchProvider = HTTPResearchProvider
