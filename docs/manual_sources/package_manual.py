"""Package only the public manual artifacts, never user runtime data."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from pypdf import PdfReader

root = Path(__file__).resolve().parents[2]
out = root / "output" / "pdf"
names = ["Ashare_User_Guide.pdf", "Ashare_User_Guide.html"]
assert len(PdfReader(out / names[0]).pages) == 24
assert (root / "查看使用手册.cmd").exists()
target = out / "使用手册分享包.zip"
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for name in names:
        archive.write(out / name, name)
    archive.writestr("先读我.txt", "A股研究工作台 · 配图使用手册\n\n请先解压全部文件，再双击 Ashare_User_Guide.html 阅读；点击图片可以放大。\nAshare_User_Guide.pdf 为24页打印版。\n无需联网或启动工作台即可阅读。\n本包只含手册，不含应用程序、Token、个人账户或真实运行记录。\n如需安装软件，请向项目维护者取得干净源码，按手册第3页操作。\n版本：0.1.1 / 后台版本6；编写日期：2026-09-11。\n")
with ZipFile(target) as archive:
    assert archive.testzip() is None
    assert set(archive.namelist()) == set(names + ["先读我.txt"])
print(f"Verified 24-page PDF and share package: {target}")
for name in names + [target.name]:
    print(f"{name}: {(out / name).stat().st_size:,} bytes")
