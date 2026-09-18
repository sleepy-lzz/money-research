"""Build a self-contained illustrated PDF and HTML manual with verified page budgets."""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Flowable, Image as RLImage, Paragraph, Table, TableStyle

from manual_content import PAGES

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output/pdf"
SCREENS = ROOT / "tmp/pdfs/manual/screens"
QA = ROOT / "tmp/pdfs/manual/qa"
for folder in (OUT, QA):
    folder.mkdir(parents=True, exist_ok=True)
pdfmetrics.registerFont(TTFont("Yahei", "C:/Windows/Fonts/msyh.ttc", subfontIndex=0))
pdfmetrics.registerFont(TTFont("YaheiBold", "C:/Windows/Fonts/msyhbd.ttc", subfontIndex=0))
pdfmetrics.registerFontFamily("Yahei", normal="Yahei", bold="YaheiBold")
TEAL = colors.HexColor("#087e83")
INK = colors.HexColor("#182635")
GRAY = colors.HexColor("#526271")
W, H = A4
M = 42
WIDTH = W - M * 2
BASE = ParagraphStyle("body", fontName="Yahei", fontSize=10.2, leading=16.2, textColor=INK,
                      wordWrap="CJK", splitLongWords=True)
SMALL = ParagraphStyle("small", parent=BASE, fontSize=8.1, leading=12, textColor=GRAY)
HEADING = ParagraphStyle("h", parent=BASE, fontName="YaheiBold", fontSize=12.5, leading=21,
                         textColor=colors.black)
CELL = ParagraphStyle("cell", parent=BASE, fontSize=9.5, leading=14.4)
WHITE = ParagraphStyle("white", parent=CELL, fontName="YaheiBold", textColor=colors.white)
CODE = ParagraphStyle("code", parent=BASE, fontSize=9.0, leading=14.5)
CAPTION = "界面演示 使用虚构资料 非真实账户或交易结果"


def para(text, style=BASE):
    return Paragraph(html.escape(text).replace("\n", "<br/>"), style)


class Diagram(Flowable):
    def __init__(self, nodes):
        Flowable.__init__(self)
        self.nodes = nodes
        self.width = WIDTH
        self.height = 82

    def draw(self):
        c = self.canv
        gap = 16
        box = (WIDTH - gap * (len(self.nodes) - 1)) / len(self.nodes)
        for i, node in enumerate(self.nodes):
            x = i * (box + gap)
            c.setFillColor(colors.HexColor("#eef7f7"))
            c.setStrokeColor(colors.HexColor("#9dc9ca"))
            c.roundRect(x, 10, box, 61, 6, stroke=1, fill=1)
            c.setFillColor(TEAL)
            c.setFont("YaheiBold", 9)
            c.drawString(x + 10, 55, f"0{i + 1}")
            pp = para(node, ParagraphStyle("diagram", parent=BASE, fontSize=9.1, leading=14))
            _, height = pp.wrap(box - 18, 40)
            pp.drawOn(c, x + 9, 44 - height)
            if i < len(self.nodes) - 1:
                y = 40
                c.setStrokeColor(TEAL)
                c.line(x + box + 3, y, x + box + gap - 3, y)
                c.line(x + box + gap - 6, y + 3, x + box + gap - 3, y)
                c.line(x + box + gap - 6, y - 3, x + box + gap - 3, y)


def materialize(block):
    kind = block[0]
    if kind == "p": return [(para(block[1]), 8)]
    if kind == "h": return [(para(block[1], HEADING), 7)]
    if kind == "steps": return [(para(f"{i + 1}. {text}"), 7) for i, text in enumerate(block[1])]
    if kind == "code": return [(para(block[1], CODE), 12)]
    if kind == "flow": return [(Diagram(block[1]), 8)]
    if kind == "pic":
        path = SCREENS / f"{block[1]}.png"
        iw, ih = Image.open(path).size
        scale = min(WIDTH / iw, block[3] / ih)
        image = RLImage(str(path), width=iw * scale, height=ih * scale)
        image.hAlign = "CENTER"
        return [(image, 5), (para(block[2] + "\n" + CAPTION, SMALL), 10)]
    if kind == "table":
        widths = block[3] or [WIDTH / len(block[1])] * len(block[1])
        widths = [w * WIDTH / sum(widths) for w in widths]
        data = [[para(v, WHITE) for v in block[1]]] + [[para(v, CELL) for v in row] for row in block[2]]
        table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24515e")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f6f7")]),
            ("GRID", (0, 0), (-1, -1), .45, colors.HexColor("#d9d9d9")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        return [(table, 12)]
    raise ValueError(kind)


def build_pdf():
    target = OUT / "Ashare_User_Guide.pdf"
    c = canvas.Canvas(str(target), pagesize=A4, pageCompression=1)
    c.setTitle("A股研究工作台配图使用手册")
    c.setAuthor("A股研究工作台")
    budgets = []
    for i, page in enumerate(PAGES, 1):
        c.bookmarkPage(f"p{i}")
        c.addOutlineEntry(page["title"], f"p{i}", 0)
        c.setFillColor(GRAY); c.setFont("Yahei", 8)
        c.drawString(M, H - 27, "A股研究工作台  |  配图使用手册")
        c.setFillColor(TEAL); c.drawRightString(W - M, H - 27, f"{i:02d}")
        c.setFillColor(colors.black); c.setFont("YaheiBold", 22 if i == 1 else 20)
        c.drawString(M, H - 67, page["title"])
        c.setFillColor(GRAY); c.setFont("Yahei", 10)
        c.drawString(M, H - 87, page["subtitle"])
        y = H - 108
        for block in page["blocks"]:
            for item, gap in materialize(block):
                width, height = item.wrap(WIDTH, 2000)
                if y - height < 43:
                    raise ValueError(f"Page {i} overflow: {block[0]} remaining={y-43:.1f}, needs={height:.1f}")
                x = M + ((WIDTH - width) / 2 if isinstance(item, RLImage) else 0)
                item.drawOn(c, x, y - height)
                y -= height + gap
        budgets.append(dict(page=i, bottom=y))
        c.setStrokeColor(colors.HexColor("#d9e3e5")); c.line(M, 32, W-M, 32)
        c.setFont("Yahei", 7.7); c.setFillColor(GRAY)
        c.drawString(M, 20, "0.1.1  |  后台版本6  |  2026年9月11日")
        c.drawRightString(W-M, 20, f"第{i}页 / 共{len(PAGES)}页")
        c.showPage()
    c.save()
    (QA / "page_budgets.json").write_text(json.dumps(budgets, indent=2), encoding="utf-8")


def build_html():
    parts = []
    def esc(text): return html.escape(str(text))
    for i, page in enumerate(PAGES, 1):
        parts.append(f'<section id="p{i}"><div class="kicker">{i:02d} / {len(PAGES)}</div><h1>{esc(page["title"])}</h1><p class="subtitle">{esc(page["subtitle"])}</p>')
        for block in page["blocks"]:
            kind = block[0]
            if kind == "p": parts.append(f'<p>{esc(block[1])}</p>')
            elif kind == "h": parts.append(f'<h2>{esc(block[1])}</h2>')
            elif kind == "steps": parts.append('<ol>'+''.join(f'<li>{esc(v)}</li>' for v in block[1])+'</ol>')
            elif kind == "code": parts.append(f'<pre><code>{esc(block[1])}</code></pre>')
            elif kind == "table": parts.append('<div class="table"><table><thead><tr>'+''.join(f'<th>{esc(v)}</th>' for v in block[1])+'</tr></thead><tbody>'+''.join('<tr>'+''.join(f'<td>{esc(v)}</td>' for v in row)+'</tr>' for row in block[2])+'</tbody></table></div>')
            elif kind == "flow": parts.append('<div class="flow">'+''.join(f'<div><b>0{j+1}</b><span>{esc(v).replace(chr(10),"<br>")}</span></div>' for j,v in enumerate(block[1]))+'</div>')
            elif kind == "pic":
                data = base64.b64encode((SCREENS / f"{block[1]}.png").read_bytes()).decode()
                parts.append(f'<figure><img tabindex="0" src="data:image/png;base64,{data}" alt="{esc(block[2])}" title="点击放大" style="max-height:{int(block[3]*1.65)}px"><figcaption>{esc(block[2])}<br>{CAPTION} 点击图片可放大</figcaption></figure>')
        parts.append('</section>')
    toc=''.join(f'<a href="#p{i}"><span>{i:02d}</span>{esc(page["title"])}</a>' for i,page in enumerate(PAGES,1))
    style='''*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:#182635;background:#eef3f5;font:16px/1.85 "Microsoft YaHei",sans-serif}aside{position:fixed;left:0;top:0;bottom:0;width:260px;background:#122f3e;color:white;padding:24px 18px;overflow:auto}aside strong{font-size:19px}aside p{font-size:12px;color:#b5cdd6}aside a{display:flex;gap:10px;color:#d3e5ec;text-decoration:none;padding:7px 6px;font-size:13px}aside a:hover{background:#24515e}aside a span{color:#85c9cf}main{margin:0 30px 0 290px;max-width:960px;padding:30px 0 60px}section{background:white;border:1px solid #dce5e8;border-radius:12px;padding:42px 46px;margin-bottom:28px;scroll-margin:20px}h1{font-size:29px;line-height:1.4;margin:5px 0;color:#000}h2{font-size:20px;margin:26px 0 10px;color:#000}.kicker{color:#087e83;font-size:12px;letter-spacing:2px}.subtitle{color:#526271;margin-top:0}p{margin:14px 0}li{padding-left:3px;margin-bottom:9px}table{width:100%;border-collapse:collapse;margin:16px 0;font-size:14px}th{background:#24515e;color:white;text-align:left}td,th{padding:11px;border:1px solid #d9d9d9;vertical-align:middle}tr:nth-child(even){background:#f2f6f7}.table{overflow:auto}figure{margin:20px 0;text-align:center}figure img{max-width:100%;object-fit:contain;cursor:zoom-in;border:1px solid #dbe4e8;border-radius:5px}figcaption{font-size:12px;color:#526271;margin-top:8px}.flow{display:flex;gap:14px;margin:22px 0}.flow div{flex:1;border:1px solid #9dc9ca;border-radius:7px;background:#eef7f7;padding:13px;font-size:13px}.flow b{display:block;color:#087e83}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f6f7;padding:16px;border-radius:6px;line-height:1.7;font-size:13px}button,.download{border:0;border-radius:5px;background:#087e83;color:white;padding:9px 16px;font:inherit;cursor:pointer;text-decoration:none}header{margin-bottom:20px;display:flex;gap:14px;align-items:center}dialog{max-width:96vw;max-height:96vh;padding:15px;border:0;border-radius:8px}dialog img{display:block;max-width:none}dialog button{position:sticky;top:0}dialog::backdrop{background:#000a}@media(max-width:1000px){aside{position:static;width:auto;max-height:260px}main{margin:0;padding:18px}section{padding:25px}.flow{flex-wrap:wrap}.flow div{min-width:130px}}@media print{aside,header,dialog{display:none!important}main{margin:0;padding:0;max-width:none}body{background:white;font-size:11px}section{break-before:page;border:0;padding:0;margin:0}section:first-child{break-before:auto}figure img{max-height:360px!important}h1{font-size:21px}table{font-size:10px}}'''
    script='''const d=document.querySelector('dialog');document.querySelectorAll('figure img').forEach(img=>{const open=()=>{d.querySelector('img').src=img.src;d.showModal()};img.addEventListener('click',open);img.addEventListener('keydown',e=>{if(e.key==='Enter')open()})});d.querySelector('button').onclick=()=>d.close();'''
    document=f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>A股研究工作台配图使用手册</title><style>{style}</style></head><body><aside><strong>A股工作台使用手册</strong><p>0.1.1 · 后台版本6<br>图中资料均为演示<br>浏览器 Ctrl+F 搜索全文</p>{toc}</aside><main><header><a class="download" href="Ashare_User_Guide.pdf">打开PDF版本</a><span>离线阅读 · 点击图片放大</span></header>{"".join(parts)}</main><dialog><button>关闭大图</button><img alt="界面配图放大"></dialog><script>{script}</script></body></html>'
    (OUT / "Ashare_User_Guide.html").write_text(document, encoding="utf-8")


if __name__ == "__main__":
    build_pdf()
    build_html()
    print(f"Built {len(PAGES)} pages in {OUT}")
