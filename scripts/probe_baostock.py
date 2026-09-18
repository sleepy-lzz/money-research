"""Read-only provider assessment; downloaded rows are NOT certified PIT inputs."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime/baostock-libs"))
import baostock as bs  # noqa: E402


def main():
    now = datetime.now().astimezone()
    end = now.date().isoformat()
    start = (now.date() - timedelta(days=16)).isoformat()
    output = ROOT / "runtime/baostock-probe" / now.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True)
    report = {"provider": "baostock", "fetched_at": now.isoformat(), "checks": [], "pit_verified": False}

    def save():
        (output / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def deadline():
        report["timeout"] = True
        save()
        os._exit(2)

    timer = threading.Timer(150, deadline)
    timer.daemon = True
    timer.start()
    socket.setdefaulttimeout(12)

    def query(name, function, **params):
        print("REQUEST", name, flush=True)
        item = {"name": name, "params": params, "fetched_at": datetime.now().astimezone().isoformat()}
        rows = []
        try:
            result = function(**params)
            while result.error_code == "0" and result.next():
                rows.append(dict(zip(result.fields, result.get_row_data(), strict=True)))
            item.update(
                code=result.error_code, message=result.error_msg, rows=len(rows), fields=result.fields
            )
            raw = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            (output / f"{name}.json").write_bytes(raw)
            item["sha256"] = hashlib.sha256(raw).hexdigest()
            if rows and "date" in rows[0]:
                item["first_date"], item["last_date"] = rows[0]["date"], rows[-1]["date"]
                item["st_rows"] = sum(r.get("isST") == "1" for r in rows)
                item["halt_rows"] = sum(r.get("tradestatus") == "0" for r in rows)
                item["duplicate_dates"] = len(rows) - len({r["date"] for r in rows})
        except Exception as exc:
            item["error"] = str(exc)
        report["checks"].append(item)
        save()
        print(json.dumps(item, ensure_ascii=True), flush=True)
        return rows

    try:
        login = bs.login()
        report["login"] = {"code": login.error_code, "message": login.error_msg}
        save()
        if login.error_code != "0":
            return
        days = query("calendar", bs.query_trade_dates, start_date=start, end_date=end)
        open_days = [
            r["calendar_date"] for r in days if r.get("is_trading_day") == "1" and r["calendar_date"] < end
        ]
        if not open_days:
            report["blocked"] = "No prior trading session returned"
            save()
            return
        last = max(open_days)
        universe = query("universe_recent", bs.query_all_stock, day=last)
        query("universe_2020", bs.query_all_stock, day="2020-09-10")
        basic = query("security_master", bs.query_stock_basic)
        query(
            "adjustment_events",
            bs.query_adjust_factor,
            code="sh.600000",
            start_date="2024-01-01",
            end_date=end,
        )
        fields = (
            "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
        )
        codes = ["sh.600000", "sz.000001", "sh.600519"]
        for predicate in (lambda r: r.get("tradeStatus") == "0", lambda r: "ST" in r.get("code_name", "")):
            candidates = [
                r["code"] for r in universe if r["code"].startswith(("sh.60", "sz.00")) and predicate(r)
            ]
            if candidates:
                codes.append(candidates[0])
        for code in dict.fromkeys(codes):
            query(
                "daily_" + code,
                bs.query_history_k_data_plus,
                code=code,
                fields=fields,
                start_date="2025-09-01",
                end_date=end,
                frequency="d",
                adjustflag="3",
            )
        query(
            "adjusted_sh.600000",
            bs.query_history_k_data_plus,
            code="sh.600000",
            fields=fields,
            start_date="2025-09-01",
            end_date=end,
            frequency="d",
            adjustflag="2",
        )
        delisted = [r for r in basic if r.get("outDate") and r.get("type") == "1"]
        report["delisted_master_count"] = len(delisted)
        if delisted:
            sec = delisted[0]
            end_delisted = sec["outDate"]
            start_delisted = (datetime.fromisoformat(end_delisted).date() - timedelta(days=45)).isoformat()
            query(
                "delisted_history",
                bs.query_history_k_data_plus,
                code=sec["code"],
                fields=fields,
                start_date=start_delisted,
                end_date=end_delisted,
                frequency="d",
                adjustflag="3",
            )
        report["latest_prior_session"] = last
        save()
    finally:
        timer.cancel()
        print("OUTPUT", output, flush=True)
        # Avoid an unbounded remote logout on a failed connection.
        import baostock.common.context as context

        sock = getattr(context, "default_socket", None)
        if sock:
            sock.close()


if __name__ == "__main__":
    main()
