import json
from datetime import datetime

import pytest

from ashare_agent.financial_context import METRICS, fetch_financial_context

NOW = datetime.fromisoformat("2026-09-11T16:30:00+08:00")


def report(**kwargs):
    return (
        dict(
            SECUCODE="600519.SH",
            SECURITY_CODE="600519",
            SECURITY_NAME_ABBR="样本公司",
            REPORT_DATE="2026-06-30 00:00:00",
            NOTICE_DATE="2026-08-15 00:00:00",
            UPDATE_DATE="2026-08-15 00:00:00",
            REPORT_TYPE="中报",
            ORG_TYPE="通用",
            CURRENCY="CNY",
            TOTALOPERATEREVE="123456789012.12",
            PARENTNETPROFIT="-10.25",
            MGJYXJJE="0",
            ROEJQ=None,
        )
        | kwargs
    )


class Client:
    def __init__(self, rows=None, fetched="2026-09-11T16:20:00+08:00", available=None, success=True):
        self.rows = rows if rows is not None else [report()]
        self.fetched, self.available, self.success = fetched, available or fetched, success
        self.receipts, self.calls = [], []

    def get(self, url, params=None, refresh=False):
        self.calls.append((url, params, refresh))
        self.receipts.append(
            dict(url=url, params=params, fetched_at=self.fetched, available_at=self.available)
        )
        return json.dumps(dict(success=self.success, code=0, result=dict(data=self.rows))).encode()


def test_financial_dates_decimals_and_missing_fields_preserve_semantics():
    client = Client()
    result = fetch_financial_context(["600519.SH"], client, NOW)
    (row,) = result["evidence"]
    assert row["published_at"] is None and row["publication_precision"] == "day"
    assert row["source_announced_date"] == "2026-08-15" and row["available_at"] == client.available
    assert row["metrics"]["TOTALOPERATEREVE"] == "123456789012.12"
    assert row["metrics"]["PARENTNETPROFIT"] == "-10.25" and row["metrics"]["MGJYXJJE"] == "0"
    assert row["metrics"]["ROEJQ"] is None and "ROEJQ" in row["missing_fields"]
    assert row["trading_eligible"] is False and row["quality"] == "aggregator_unverified"
    assert client.calls[0][2] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"SECUCODE": "000001.SZ"},
        {"SECURITY_CODE": "000001"},
        {"CURRENCY": "USD"},
        {"NOTICE_DATE": None},
        {"UPDATE_DATE": "2026-09-12 00:00:00"},
        {"NOTICE_DATE": "2026-09-12 00:00:00"},
        {"REPORT_DATE": "2027-06-30 00:00:00"},
        {"REPORT_DATE": "2026-06-29 00:00:00"},
        {"TOTALOPERATEREVE": "NaN"},
        {"TOTALOPERATEREVE": "Infinity"},
        {"TOTALOPERATEREVE": True},
        {"ORG_TYPE": None},
        {key: None for key in METRICS},
    ],
)
def test_invalid_rows_do_not_fall_back_to_old_report(changes):
    client = Client([report(**changes), report(REPORT_DATE="2026-03-31 00:00:00")])
    result = fetch_financial_context(["600519.SH"], client, NOW)
    assert result["evidence"] == []
    assert result["coverage"]["600519.SH"]["status"] == "unverified"


def test_same_period_conflict_rejected_but_acquired_revision_gets_new_id():
    result = fetch_financial_context(["600519.SH"], Client([report(), report(PARENTNETPROFIT="20")]), NOW)
    assert not result["evidence"] and "冲突" in result["coverage"]["600519.SH"]["reason"]
    first = fetch_financial_context(["600519.SH"], Client(), NOW)["evidence"][0]
    changed = fetch_financial_context(["600519.SH"], Client([report(PARENTNETPROFIT="20")]), NOW)["evidence"][
        0
    ]
    assert first["evidence_id"] != changed["evidence_id"] and first["metrics"]["PARENTNETPROFIT"] == "-10.25"


def test_future_receipts_and_error_payload_rejected():
    for client in [
        Client(fetched="2026-09-12T10:00:00+08:00"),
        Client(available="2026-09-12T10:00:00+08:00"),
        Client(success=False),
    ]:
        assert not fetch_financial_context(["600519.SH"], client, NOW)["evidence"]


def test_history_bounded_duplicate_collapsed_and_wrong_codes_rejected():
    rows = [
        report(REPORT_DATE=f"{year}-{end} 00:00:00")
        for year in (2025, 2024)
        for end in ("12-31", "09-30", "06-30", "03-31")
    ]
    result = fetch_financial_context(["600519.SH"], Client(rows), NOW)
    assert len(result["evidence"]) == 4 and result["evidence"][0]["report_period"] == "2025-12-31"
    assert len(fetch_financial_context(["600519.SH"], Client([report(), report()]), NOW)["evidence"]) == 1
    for code in ('600519.SH")', "sh600519", "600519.sh", "830001.BJ"):
        with pytest.raises(ValueError):
            fetch_financial_context([code], Client(), NOW)
