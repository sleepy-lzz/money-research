"""Double-click entry point; startup failures remain visible in a local log."""

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
# When launched by pythonw from Explorer, sys.path starts at scripts/ and may
# resolve a stale globally installed ashare_agent. Always use this checkout.
sys.path.insert(0, str(ROOT / "src"))
try:
    from ashare_agent.webapp import main

    main()
except Exception:
    log = ROOT / "runtime/web-startup-error.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text(traceback.format_exc(), encoding="utf-8")
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, f"启动失败，详情见：{log}", "选股工作台", 16)
