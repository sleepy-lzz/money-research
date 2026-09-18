from copy import deepcopy

import pandas as pd
import pytest

from ashare_agent.research import ResearchRecord, capture_evidence, ingest_evidence, research_candidates


def ranking():
    return pd.DataFrame([dict(ts_code="AAA.SZ", rank=1, eligible=True, score=0.9)])


def evidence(**changes):
    record = dict(
        symbol="AAA.SZ",
        url="https://example.invalid/filing",
        published_at="2024-06-01T09:00:00+08:00",
        fetched_at="2024-06-01T09:01:00+08:00",
        available_at="2024-06-01T09:01:00+08:00",
        content="archived source text",
        evidence_quality="primary",
    )
    record.update(changes)
    return record


class Provider:
    model_version = "fixture"
    prompt_version = "v2"

    def __init__(self, mutate=None):
        self.calls = 0
        self.mutate = mutate

    def research(self, candidate, as_of, evidence, **kwargs):
        self.calls += 1
        if self.mutate:
            self.mutate(evidence[0])
        source = {k: v for k, v in evidence[0].items() if k not in {"symbol", "ts_code"}}
        return dict(
            symbol=candidate["ts_code"],
            as_of=as_of,
            status="available",
            thesis="Source-based summary",
            catalyst="fixture",
            invalidation="fixture",
            risk_flags=[],
            sources=[source],
            evidence_quality="primary",
            model_version=self.model_version,
            prompt_version=self.prompt_version,
        )


def test_disabled_is_mechanical_and_never_calls_model(tmp_path):
    result = research_candidates(ranking(), "2024-06-28", {}, tmp_path)
    ResearchRecord.model_validate(result[0])
    assert result[0]["model_version"] == "mechanical-disabled" and not result[0]["sources"]


def test_valid_source_cached_and_revision_invalidates_cache(tmp_path):
    provider = Provider()
    args = (ranking(), "2024-06-28", {"research_provider": provider}, tmp_path)
    first = research_candidates(*args, [evidence()])
    assert first[0]["status"] == "available"
    assert research_candidates(*args, [evidence()]) == first and provider.calls == 1
    second = research_candidates(*args, [evidence(content="corrected content")])
    assert (
        provider.calls == 2
        and second[0]["sources"][0]["content_hash"] != first[0]["sources"][0]["content_hash"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("published_at", None),
        ("published_at", "2024-06-01"),
        ("available_at", None),
        ("fetched_at", None),
        ("content_hash", "0" * 64),
        ("available_at", "2024-06-28T20:00:00+08:00"),
        ("fetched_at", "2024-06-29T09:00:00+08:00"),
    ],
)
def test_bad_missing_or_future_evidence_never_reaches_provider(tmp_path, field, value):
    provider = Provider()
    result = research_candidates(
        ranking(),
        "2024-06-28",
        {"research_provider": provider},
        tmp_path,
        [evidence(**{field: value})],
        decision_cutoff="2024-06-28T18:00:00+08:00",
    )
    assert provider.calls == 0 and result[0]["status"] == "unavailable"


@pytest.mark.parametrize(
    "field,value",
    [
        ("published_at", "2024-05-01T09:00:00+08:00"),
        ("url", "https://example.invalid/invented"),
        ("content", "fabricated"),
    ],
)
def test_provider_cannot_mutate_the_authoritative_reference(tmp_path, field, value):
    original = [evidence()]
    saved = deepcopy(original)
    provider = Provider(lambda row: row.update({field: value}))
    result = research_candidates(ranking(), "2024-06-28", {"research_provider": provider}, tmp_path, original)
    assert provider.calls == 1 and result[0]["status"] == "unavailable" and original == saved


def test_ingestion_stamps_now_and_conflicting_ids_fail(tmp_path):
    captured = capture_evidence(
        url="https://example.invalid/news", published_at="2024-01-01T00:00:00Z", content="old news"
    )
    assert captured["fetched_at"] == captured["available_at"]
    assert pd.Timestamp(captured["available_at"]) > pd.Timestamp("2024-06-28T23:59:59+08:00")
    a, b = evidence(evidence_id="same"), evidence(evidence_id="same", content="different")
    with pytest.raises(ValueError, match="conflicting"):
        ingest_evidence([a, b])
    provider = Provider()
    result = research_candidates(ranking(), "2024-06-28", {"research_provider": provider}, tmp_path, [a, b])
    assert provider.calls == 0 and result[0]["status"] == "unavailable"


def test_pd_na_rank_ignored(tmp_path):
    rows = ranking()
    rows.loc[0, "rank"] = pd.NA
    assert research_candidates(rows, "2024-06-28", {}, tmp_path) == []
