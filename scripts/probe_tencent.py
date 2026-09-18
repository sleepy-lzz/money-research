"""Probe the Tencent HTTP route documented in AKShare, without changing production adapters."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def main():
    now = datetime.now().astimezone()
    output = ROOT / "runtime/tencent-probe" / now.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True)
    report = {
        "fetched_at": now.isoformat(),
        "url": URL,
        "checks": [],
        "pit_verified": False,
        "mode": "direct HTTP reproduction of AKShare upstream request, not installed SDK",
    }

    def save():
        (output / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    for symbol, adjust, env in [
        ("sh600000", "", True),
        ("sh600000", "", False),
        ("sz000001", "", False),
        ("sh600519", "", False),
        ("sh600000", "qfq", False),
    ]:
        label = f"{symbol}-{adjust or 'raw'}-{'configured-network' if env else 'direct'}"
        params = {
            "_var": f"kline_day{adjust}{now.year - 1}",
            "param": f"{symbol},day,{now.year - 1}-01-01,{now.date().isoformat()},640,{adjust}",
        }
        item = {"name": label, "params": params, "fetched_at": datetime.now().astimezone().isoformat()}
        print("REQUEST", label, flush=True)
        try:
            with httpx.Client(trust_env=env, timeout=15, follow_redirects=False) as client:
                response = client.get(URL, params=params)
            item["http_status"] = response.status_code
            response.raise_for_status()
            raw = response.content
            (output / f"{label}.txt").write_bytes(raw)
            item["sha256"] = hashlib.sha256(raw).hexdigest()
            body = json.loads(response.text[response.text.find("={") + 1 :])
            item["provider_code"] = body.get("code")
            data = body.get("data", {}).get(symbol, {})
            key = "qfqday" if adjust else "day"
            rows = data.get(key, [])
            item.update(rows=len(rows), series_key=key)
            if rows:
                item.update(
                    first_date=rows[0][0],
                    last_date=rows[-1][0],
                    last_row=rows[-1],
                    unique_dates=len({r[0] for r in rows}),
                    date_ordered=all(a[0] < b[0] for a, b in zip(rows, rows[1:])),
                )
                bad, unit_votes = 0, {"raw_volume_is_shares": 0, "raw_volume_is_lots": 0, "neither": 0}
                for row in rows:
                    op, close, hi, lo = map(float, row[1:5])
                    if not (0 < lo <= min(op, close) <= max(op, close) <= hi):
                        bad += 1
                    if not adjust and len(row) > 8 and float(row[5]) > 0:
                        amount = float(row[8]) * 10000
                        vol = float(row[5])
                        share_ok = lo * 0.99 <= amount / vol <= hi * 1.01
                        lot_ok = lo * 0.99 <= amount / (vol * 100) <= hi * 1.01
                        vote = (
                            "raw_volume_is_shares"
                            if share_ok
                            else "raw_volume_is_lots"
                            if lot_ok
                            else "neither"
                        )
                        unit_votes[vote] += 1
                item["invalid_ohlc_rows"] = bad
                item["unit_sanity_votes_assuming_amount_wan_cny"] = unit_votes
            else:
                item["data_keys"] = list(data)
        except Exception as exc:
            item["error"] = str(exc)
        report["checks"].append(item)
        save()
        print(json.dumps(item, ensure_ascii=True), flush=True)
    print("OUTPUT", output, flush=True)


if __name__ == "__main__":
    main()
