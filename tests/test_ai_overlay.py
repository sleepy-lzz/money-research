import pytest

from ashare_agent.ai_overlay import OverlayLedger, validate_result


def payload(mode="balanced"):
    return {"schema_version": 1, "mode": mode, "batch_id": "b1", "as_of": "2026-09-11", "model_version": "gpt-6-astra", "prompt_version": "overlay-v1", "available_at": "2026-09-11T17:00:00+08:00", "input_hash": "input", "decisions": [{"ts_code": "600000.SH", "allowed_action": "deprioritize", "reason": "verified risk", "evidence_ids": ["e1"]}]}

@pytest.mark.parametrize("mode", ["conservative", "balanced", "aggressive"])
def test_all_modes_are_structured_non_executable(mode):
    result = validate_result(payload(mode), batch_id="b1", as_of="2026-09-11")
    assert result["trading_weight"] == 0
    assert result["mechanical_fallback"] is True

def test_rejects_wrong_batch_and_order_math():
    with pytest.raises(ValueError):
        validate_result(payload(), batch_id="b2", as_of="2026-09-11")
    bad = payload()
    bad["decisions"][0]["quantity"] = 100
    with pytest.raises(ValueError):
        validate_result(bad, batch_id="b1", as_of="2026-09-11")

def test_validates_batch_codes_evidence_and_context_hash():
    with pytest.raises(ValueError):
        validate_result(payload(), batch_id="b1", as_of="2026-09-11", batch_codes={"000001.SZ"})
    with pytest.raises(ValueError):
        validate_result(payload(), batch_id="b1", as_of="2026-09-11", evidence_ids={"other"})
    with pytest.raises(ValueError):
        validate_result(payload(), batch_id="b1", as_of="2026-09-11", expected_input_hash="different")

def test_context_changes_output_hash():
    a = validate_result(payload(), batch_id="b1", as_of="2026-09-11")
    b = payload()
    b["prompt_version"] = "overlay-v2"
    assert a["output_hash"] != validate_result(b, batch_id="b1", as_of="2026-09-11")["output_hash"]

def test_ledger_idempotent(tmp_path):
    ledger = OverlayLedger(tmp_path / "overlay.sqlite")
    a = ledger.record(payload(), batch_id="b1", as_of="2026-09-11")
    b = ledger.record(payload(), batch_id="b1", as_of="2026-09-11")
    assert a["result_id"] == b["result_id"]
    assert ledger.db.execute("select count(*) from overlay_results").fetchone()[0] == 1
    ledger.close()
