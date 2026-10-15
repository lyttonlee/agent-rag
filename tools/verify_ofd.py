"""OFD 解析入库验证（方案 A+D）。

构造最小 OFD（GB/T 33190，ZIP+XML）样本，验证：
1. parse_ofd_bytes 正确提取文字层、页数、阅读顺序（按 Y 坐标）；
2. parse_bytes 对 .ofd 扩展名、无扩展名（嗅探）均正确分发到 ofd；
3. 回归：真实 DOCX 字节仍被判为 docx（未被误判为 ofd）；
4. 无文字层页计入 scanned（方案 D 标记）。

用法：python tools/verify_ofd.py
"""

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.parser import _looks_like_ofd, _sniff_format, parse_bytes, parse_ofd_bytes  # noqa: E402

NS = "http://www.ofdspec.org/2016"

OFD_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<ofd:OFD xmlns:ofd="{NS}" Version="1.1">
  <ofd:DocBody><ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot></ofd:DocBody>
</ofd:OFD>'''

DOC_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<ofd:Document xmlns:ofd="{NS}">
  <ofd:Pages>
    <ofd:Page ID="1" BaseLoc="Page_0/Content.xml"/>
    <ofd:Page ID="2" BaseLoc="Page_1/Content.xml"/>
  </ofd:Pages>
</ofd:Document>'''

# 第 1 页：乱序的 TextCode（Y 坐标应重排为阅读顺序：标题→主送→正文）
PAGE0 = f'''<?xml version="1.0" encoding="UTF-8"?>
<ofd:Page xmlns:ofd="{NS}"><ofd:Content>
  <ofd:TextObject Boundary="70 160 400 40"><ofd:TextCode X="70" Y="160">一、总体要求。坚持人民至上、生命至上。</ofd:TextCode></ofd:TextObject>
  <ofd:TextObject Boundary="70 80 400 24"><ofd:TextCode X="70" Y="80">关于开展安全生产大检查的通知</ofd:TextCode></ofd:TextObject>
  <ofd:TextObject Boundary="70 120 400 20"><ofd:TextCode X="70" Y="120">各省、自治区、直辖市人民政府：</ofd:TextCode></ofd:TextObject>
</ofd:Content></ofd:Page>'''

# 第 2 页：无文字层（模拟签章/扫描页，应计入 scanned）
PAGE1 = f'''<?xml version="1.0" encoding="UTF-8"?>
<ofd:Page xmlns:ofd="{NS}"><ofd:Content>
  <ofd:ImageObject ResourceID="1"/>
</ofd:Content></ofd:Page>'''


def build_ofd() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("OFD.xml", OFD_XML)
        zf.writestr("Doc_0/Document.xml", DOC_XML)
        zf.writestr("Doc_0/Page_0/Content.xml", PAGE0)
        zf.writestr("Doc_0/Page_1/Content.xml", PAGE1)
    return buf.getvalue()


def build_docx() -> bytes:
    from docx import Document as DocxDocument

    d = DocxDocument()
    d.add_paragraph("这是一个 DOCX 回归样本。")
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def main() -> None:
    ofd = build_ofd()
    docx = build_docx()
    ok = True

    # 1) parse_ofd_bytes：文字提取 + 页数 + scanned 标记
    doc = parse_ofd_bytes(ofd)
    has_title = "关于开展安全生产大检查的通知" in doc.text
    has_body = "一、总体要求" in doc.text
    # 阅读顺序：标题(Y80) 应出现在 正文(Y160) 之前
    order_ok = doc.text.find("通知") < doc.text.find("总体要求")
    print(f"[{'PASS' if has_title else 'FAIL'}] 提取到标题")
    print(f"[{'PASS' if has_body else 'FAIL'}] 提取到正文")
    print(f"[{'PASS' if order_ok else 'FAIL'}] 阅读顺序（按 Y 重排正确）")
    print(f"[{'PASS' if doc.pages == 2 else 'FAIL'}] 页数 = 2（实际 {doc.pages}）")
    print(f"[{'PASS' if doc.scanned_pages == 1 else 'FAIL'}] 无文字层页计入 scanned=1（实际 {doc.scanned_pages}）")
    ok &= has_title and has_body and order_ok and doc.pages == 2 and doc.scanned_pages == 1

    # 2) 分发：.ofd 扩展名 + 无扩展名嗅探
    d1 = parse_bytes("gov.ofd", ofd)
    print(f"[{'PASS' if '通知' in d1.text else 'FAIL'}] parse_bytes('.ofd') 分发到 ofd")
    d2 = parse_bytes("noext", ofd)
    print(f"[{'PASS' if '通知' in d2.text else 'FAIL'}] parse_bytes(无扩展名) 嗅探为 ofd")
    ok &= "通知" in d1.text and "通知" in d2.text

    # 3) 回归：DOCX 不被误判为 ofd
    print(f"[{'PASS' if _sniff_format(docx) == 'docx' else 'FAIL'}] DOCX 嗅探仍为 docx（实际 {_sniff_format(docx)}）")
    print(f"[{'PASS' if not _looks_like_ofd(docx) else 'FAIL'}] DOCX 判定为非 ofd")
    d3 = parse_bytes("sample.docx", docx)
    print(f"[{'PASS' if 'DOCX' in d3.text else 'FAIL'}] parse_bytes('.docx') 正常解析 docx")
    ok &= _sniff_format(docx) == "docx" and "DOCX" in d3.text

    print("\n结果：", "ALL PASS ✅" if ok else "存在 FAIL ❌")


if __name__ == "__main__":
    main()
