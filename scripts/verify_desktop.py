"""Exercise the real Tk event loop and demo subprocess chain without opening a browser."""

import json
import time
import tkinter as tk

from desktop import Desktop


def main():
    root = tk.Tk()
    root.withdraw()
    app = Desktop(root)
    opened = []
    app.open_path = lambda path: opened.append(str(path))
    app.demo()
    deadline = time.monotonic() + 180
    while app.busy and time.monotonic() < deadline:
        root.update()
        time.sleep(0.05)
    assert not app.busy, "Desktop demo did not complete"
    assert app.report and app.report.is_file(), app.log.get("1.0", "end")
    assert opened == [str(app.report)], "Completed demo did not open its report"
    assert (app.run_dir / "account.sqlite").exists()
    assert all(str(button.cget("state")) == "normal" for button in app.buttons)
    report = str(app.report)
    app.run_action("doctor")
    while app.busy and time.monotonic() < deadline:
        root.update()
        time.sleep(0.05)
    assert not app.busy
    quality = json.loads((app.run_dir / "quality.json").read_text(encoding="utf-8"))
    assert quality["tradable"] is True
    root.destroy()
    print(json.dumps({"desktop_demo": "passed", "desktop_doctor": "passed", "report": report}))


if __name__ == "__main__":
    main()
