"""文档解析：PDF / DOCX / TXT / OFD 文本提取 + 扫描件判定。

设计（详见《详细功能设计文档》）：
- 优先提取文字层；无文字层（扫描件/无文字层 OFD 页）标记为需 OCR（MVP 留后路，非必做）。
- 真实样本体检显示 96.7% 为带文字层电子版，OCR 非瓶颈。
- TXT/MD 为纯文本直读（兼容 UTF-8 / GBK），主要满足模板文件上传场景。
- OFD（GB/T 33190 开放版式文档，公文主流格式）为标准库 zipfile+ElementTree
  轻量解析（Python 生态无成熟可用的 GB/T 33190 库，OFD 为开放标准）。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import pymupdf  # PyMuPDF（新入口，fitz 已弃用）
from docx import Document as DocxDocument


@dataclass
class ParsedDoc:
    text: str
    pages: int
    is_scanned: bool          # 是否纯扫描件（无文字层）
    scanned_pages: int


def parse_pdf_bytes(data: bytes) -> ParsedDoc:
    """解析 PDF 字节流，判定扫描件（逐页检测文字层）。"""
    doc = pymupdf.open(stream=data, filetype="pdf")
    texts: list[str] = []
    scanned_pages = 0
    for page in doc:
        t = page.get_text("text").strip()
        if len(t) < 5:  # 阈值：近乎无文字视为扫描页
            scanned_pages += 1
            t = ""  # 留待 OCR
        texts.append(t)
    full = "\n".join(texts)
    is_scanned = scanned_pages >= max(1, doc.page_count // 2)
    return ParsedDoc(text=full, pages=doc.page_count, is_scanned=is_scanned, scanned_pages=scanned_pages)


def parse_docx_bytes(data: bytes) -> ParsedDoc:
    """解析 DOCX 字节流。"""
    doc = DocxDocument(io.BytesIO(data))
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    return ParsedDoc(text="\n".join(paras), pages=1, is_scanned=False, scanned_pages=0)


def parse_text_bytes(data: bytes) -> ParsedDoc:
    """解析纯文本（TXT/MD）：兼容 UTF-8 与 GBK 两种常见公文导出编码。"""
    for enc in ("utf-8", "gbk"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    # 统一换行符，便于后续章节结构识别
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return ParsedDoc(text=text, pages=1, is_scanned=False, scanned_pages=0)


def _local(tag: object) -> str:
    """去 XML 命名空间前缀：'{http://...}TextCode' -> 'TextCode'（OFD 命名空间无关匹配）。"""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _iter_local(root: ET.Element, name: str) -> list[ET.Element]:
    """遍历返回所有本地名等于 name 的元素（忽略命名空间，兼容各厂商 OFD 变体）。"""
    return [el for el in root.iter() if _local(el.tag) == name]


def _looks_like_ofd(data: bytes) -> bool:
    """ZIP 包是否为 OFD（GB/T 33190）：解包探测 OFD.xml 或 ofdspec 命名空间。

    DOCX 与 OFD 均为 PK 魔数的 ZIP，仅凭魔数无法区分；必须解包看内部结构，
    否则 OFD 会被当作 docx 交给 python-docx 而解析失败/内容错乱。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n.replace("\\", "/") for n in zf.namelist()]
            # 1) 存在 OFD.xml 入口即判定为 OFD
            if any(n.split("/")[-1].lower() == "ofd.xml" for n in names):
                return True
            # 2) 无入口时抽查首个 xml 是否含 ofdspec 命名空间
            for n in names:
                if n.lower().endswith(".xml"):
                    return b"ofdspec.org" in zf.read(n)[:1024]
    except (zipfile.BadZipFile, KeyError, OSError):
        return False
    return False


def _ofd_page_text(croot: ET.Element) -> str:
    """从单个页面 Content.xml 提取文字，按坐标 (Y,X) 重排为阅读顺序。

    OFD 文字位于 <TextCode>，其文档顺序未必等于阅读顺序（分栏/右起排版），
    故优先按 TextCode/TextObject 的 Y、X 坐标排序；缺坐标时退化为文档顺序。
    同一行（Y 相近）的文本片段拼接，异行换行。
    """
    runs: list[tuple[float, float, int, str]] = []
    for idx, el in enumerate(_iter_local(croot, "TextCode")):
        text = (el.text or "").strip()
        if not text:
            continue
        # 坐标优先取 TextCode 自身，其次父级 TextObject
        def _num(node: ET.Element, key: str) -> float | None:
            v = node.get(key)
            try:
                return float(v) if v is not None else None
            except ValueError:
                return None

        y = _num(el, "Y")
        x = _num(el, "X")
        parent = next((p for p in croot.iter() if el in list(p)), None)
        if (y is None or x is None) and parent is not None:
            y = y if y is not None else _num(parent, "Y")
            x = x if x is not None else _num(parent, "X")
        runs.append((y if y is not None else 0.0, x if x is not None else 0.0, idx, text))

    if not runs:
        return ""
    # 无任何坐标时保留文档顺序（idx 稳定排序键）
    runs.sort(key=lambda r: (round(r[0], 1), r[1], r[2]))
    lines: list[str] = []
    cur_y = runs[0][0]
    cur: list[str] = []
    for y, _x, _idx, text in runs:
        if abs(y - cur_y) > 1.5 and cur:  # Y 跳变视为换行
            lines.append("".join(cur))
            cur = []
            cur_y = y
        cur.append(text)
    if cur:
        lines.append("".join(cur))
    return "\n".join(lines)


def parse_ofd_bytes(data: bytes) -> ParsedDoc:
    """解析 OFD（GB/T 33190 开放版式文档）字节流，提取文字层。

    设计：OFD 本质是 ZIP 包——OFD.xml(入口) → DocRoot 指向 Document.xml →
    Page 的 BaseLoc 指向 Content.xml，文字在 Content.xml 的 <TextCode>。
    用标准库 zipfile + ElementTree 解析（零三方依赖，内网离线可用；
    Python 生态无成熟可用的 GB/T 33190 库，OFD 为开放标准故自研轻量解析）。

    方案 D（OCR 兜底）：无文字层的页（签章/扫描/图片页）计入 scanned_pages，
    与 PDF 扫描件策略一致，OCR 引擎复用项目二期预留（当前仅标记，不接引擎）。
    """
    pages_text: list[str] = []
    scanned = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
        name_set = set(names)

        # 1) 定位 Document.xml：优先 OFD.xml → DocRoot，其次直接按文件名
        doc_paths: list[str] = []
        entry = next((n for n in names if n.split("/")[-1].lower() == "ofd.xml"), None)
        if entry:
            try:
                root = ET.fromstring(zf.read(entry))
                for dr in _iter_local(root, "DocRoot"):
                    loc = (dr.text or dr.get("BaseLoc") or "").strip()
                    if loc:
                        doc_paths.append(loc)
            except ET.ParseError:
                pass
        if not doc_paths:
            doc_paths = [n for n in names if n.split("/")[-1].lower() == "document.xml"]

        def _resolve(rel: str, base_dir: str) -> str | None:
            """把包内相对路径解析为实际条目名（相对 Document 目录或包根，含兜底尾匹配）。"""
            rel = rel.lstrip("/")
            for cand in (f"{base_dir}/{rel}" if base_dir else rel, rel):
                if cand in name_set:
                    return cand
            return next((n for n in names if n.endswith(rel)), None)

        for dp in doc_paths:
            real = _resolve(dp, "")
            if not real:
                continue
            try:
                doc_root = ET.fromstring(zf.read(real))
            except (ET.ParseError, KeyError):
                continue
            base_dir = real.rsplit("/", 1)[0] if "/" in real else ""

            # 2) 定位各页 Content.xml：优先 Page 的 BaseLoc，兜底全局 Content.xml 排序
            content_refs: list[str] = []
            for pg in _iter_local(doc_root, "Page"):
                loc = (pg.get("BaseLoc") or "").strip()
                if loc:
                    content_refs.append(loc)
            if not content_refs:
                content_refs = sorted(
                    n for n in names if n.split("/")[-1].lower() == "content.xml"
                )
                base_dir = ""  # 全局匹配时按包根解析

            for ref in content_refs:
                cand = _resolve(ref, base_dir)
                if not cand:
                    continue
                try:
                    croot = ET.fromstring(zf.read(cand))
                except (ET.ParseError, KeyError):
                    continue
                text = _ofd_page_text(croot)
                if len(text.strip()) < 5:  # 近乎无文字：视为无文字层页（待 OCR）
                    scanned += 1
                    text = ""
                pages_text.append(text)

    full = "\n".join(pages_text)
    page_count = len(pages_text)
    if page_count == 0:
        is_scanned = len(full.strip()) < 5
    else:
        is_scanned = scanned >= max(1, page_count // 2)
    return ParsedDoc(
        text=full, pages=page_count or 1, is_scanned=is_scanned, scanned_pages=scanned
    )


def _sniff_format(data: bytes) -> str:
    """按文件头（magic bytes）嗅探真实格式，兼容「无扩展名」文件。"""
    head = data[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    # DOCX / OFD 本质都是 ZIP 包（PK\x03\x04）——需解包看内部 XML 命名空间区分，
    # 不能仅凭魔数判定为 docx（否则 OFD 被 python-docx 误解析）。
    if head.startswith(b"PK\x03\x04"):
        return "ofd" if _looks_like_ofd(data) else "docx"
    return ""


def parse_bytes(filename: str, data: bytes) -> ParsedDoc:
    """按扩展名（优先）或内容嗅探分发解析。

    说明：公文系统导出的文件常不带扩展名，故在无扩展名时回退到
    文件头嗅探，保证 PDF / DOCX 仍可被正确解析；TXT/MD 按扩展名直读。
    """
    lower = filename.lower()
    fmt = ""
    if lower.endswith(".pdf"):
        fmt = "pdf"
    elif lower.endswith((".docx", ".doc")):
        fmt = "docx"
    elif lower.endswith(".ofd"):
        fmt = "ofd"
    elif lower.endswith((".txt", ".md")):
        fmt = "text"
    else:
        fmt = _sniff_format(data)  # 无扩展名或未知后缀时嗅探
    if fmt == "pdf":
        return parse_pdf_bytes(data)
    if fmt == "docx":
        return parse_docx_bytes(data)
    if fmt == "ofd":
        return parse_ofd_bytes(data)
    if fmt == "text":
        return parse_text_bytes(data)
    # 其它格式（wps 等）二期或经格式适配器预处理
    raise ValueError(f"暂不支持的解析格式：{filename}")
