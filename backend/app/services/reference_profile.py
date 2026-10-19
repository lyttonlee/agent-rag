"""上传参考文档画像提取（2026-10-19，用户需求：按模板格式生成 / 仿写）。

设计动机
--------
用户在起草时上传一份文档，期望两种用法：
  ① **按模板格式生成**——新文稿遵循该文档的版式（字体/字号/红头/边距…）；
  ② **仿写**——新文稿模仿该文档的结构、语气与条款组织方式。
且允许不选已有文种/模板（参考文档取代文种模板的地位）。

画像内容（profile dict）：
- ``layout``：docx 专有的版式画像，键与模板库 layout 对齐
  （``font``/``title_font``/``heading_font``/``red_head``），
  成稿提示词与导出渲染（docx_render）共用；PDF/OFD/TXT 为 ``None``。
- ``outline``：结构大纲 [{level, text}]（标题层级 + 条目），
  仿写模式让模型按同一骨架组织新文稿。
- ``opening`` / ``closing`` / ``excerpt``：开头/结尾/代表性摘录
  （均截断，仿写模式据此模仿行文语气）。
- ``style_notes``：人读版式与行文摘要（直接注入提示词）。
- ``para_count`` / ``approx_words``：规模参考（控制新文稿篇幅）。

边界与取舍
----------
- 版式只提取**公文渲染器认得的键**（字体/红头）；页边距/页脚等进
  style_notes 供模型遵循，不改渲染器——避免为单一需求破坏 GB/T 9704
  默认版式的稳定性。
- 西文字体（Calibri/等线…）不做正文字体覆盖：中文渲染会掉回默认
  宋体，反而劣于公文默认；只有识别到中日韩字体名才覆盖。
- 解析失败不静默：向上抛 ValueError，由调用方落 FAILED 状态并原样
  告知用户（上传即得反馈，不留"传了却没用上"的困惑）。
"""

from __future__ import annotations

import re
from collections import Counter

from app.services.parser import parse_bytes

# 成稿/导出渲染共用的版式键（与 docx_render / 模板 layout 对齐）
_LAYOUT_KEYS = ("font", "title_font", "heading_font", "red_head")

# 常见中日韩字体名（用于过滤"西文默认字体冒充正文字体"）
_CJK_FONT_RE = re.compile(
    r"(宋体|黑体|楷体|仿宋|微软|方正|小标宋|姚体|隶书|行楷|舒体|圆体|"
    r"思源|SourceHan|Noto|等线|苹方|繁体|GB2312|GBK|STZhongsong|SimSun|"
    r"KaiTi|FangSong|SimHei|MicrosoftYaHei)"
)
# 标题样式名特征（Word 内置样式的多语言写法）
_HEADING_STYLE_RE = re.compile(r"(heading|标题|標題|題目|题目)", re.IGNORECASE)
# 疑似标题行：短、无句末标点（用于无样式信息的纯文本大纲抽取）
_HEADING_LIKE_RE = re.compile(r"^[^。！？；…]{2,40}$")
# 行尾标点（排除"标题行"误判：正文条目常以句号/分号收尾）
_END_PUNCT_RE = re.compile(r"[。！？；…]$")
# 大纲条目引导符：第X章/一、（一）/1./（1）等（中文序号必须允许 一、 无括号形态）
_OUTLINE_LEAD_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百]+[章节条]"
    r"|[（(]?[一二三四五六七八九十]+[)）、.]"
    r"|\d{1,2}[.、)]"
    r"|[（(]\d{1,2}[)）])"
)


def _cjk_font_or_none(font: str | None) -> str | None:
    """仅当中文字体名真实存在时返回它，否则 None（保持公文默认字体）。"""
    if not font:
        return None
    return font if _CJK_FONT_RE.search(font) else None


def _pt(size) -> str:
    """长度 → 磅数字符串。接受 Length（EMU，含 .pt）或已是磅数的 float。"""
    if size is None:
        return "默认"
    val = getattr(size, "pt", size)  # Length → 取其 pt；数字原样使用
    return f"{val:g}pt"


def _red(color) -> bool:
    """判断颜色是否偏红（红头检测：R 显著高于 G/B）。"""
    try:
        rgb = str(color.rgb)
    except Exception:
        return False
    if len(rgb) != 6:
        return False
    r, g, b = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
    return r >= 150 and r - g >= 60 and r - b >= 60


def _para_xml_attrs(p) -> dict:
    """读取段落 XML 里的对齐/缩进属性（python-docx 高层 API 覆盖不全）。"""
    from docx.oxml.ns import qn

    attrs: dict = {}
    pPr = p._p.pPr
    if pPr is None:
        return attrs
    jc = pPr.find(qn("w:jc"))
    if jc is not None:
        attrs["jc"] = jc.get(qn("w:val"))
    ind = pPr.find(qn("w:ind"))
    if ind is not None:
        for key in ("w:firstLineChars", "w:firstLine"):
            v = ind.get(qn(key))
            if v:
                attrs[key] = v
                break
    return attrs


def _run_profile(p) -> dict:
    """聚合段落首个有效 run 的字体特征（size/bold/color/name/eastasia）。"""
    from docx.oxml.ns import qn

    for run in p.runs:
        rPr = run._r.rPr
        east = None
        if rPr is not None:
            rFonts = rPr.find(qn("w:rFonts"))
            if rFonts is not None:
                east = rFonts.get(qn("w:eastAsia")) or rFonts.get(qn("w:ascii"))
        return {
            "eastasia": _cjk_font_or_none(east),
            "raw_font": east,
            "size_pt": run.font.size.pt if run.font.size else None,
            "bold": bool(run.font.bold),
            "red": _red(run.font.color) if run.font.color else False,
        }
    return {"eastasia": None, "raw_font": None, "size_pt": None,
            "bold": False, "red": False}


def _docx_profile(data: bytes) -> dict:
    """DOCX 版式画像：页面、标题、正文、标题字体、红头、落款、结构大纲。"""
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = docx.Document(io_bytes(data))
    paras = [p for p in doc.paragraphs]
    non_empty = [p for p in paras if (p.text or "").strip()]
    if not non_empty:
        raise ValueError("文档为空（未解析到任何文本段落）")

    # ---- 页面设置（第一个 section；公文通常全文统一）----
    margins = {}
    try:
        sec = doc.sections[0]
        margins = {
            "top_cm": round(sec.top_margin.cm, 2),
            "bottom_cm": round(sec.bottom_margin.cm, 2),
            "left_cm": round(sec.left_margin.cm, 2),
            "right_cm": round(sec.right_margin.cm, 2),
        }
    except Exception:
        margins = {}

    # ---- 逐段画像（限前 400 段，超长公文不逐段扫）----
    infos: list[dict] = []
    for p in non_empty[:400]:
        attrs = _para_xml_attrs(p)
        rp = _run_profile(p)
        infos.append({
            "text": (p.text or "").strip(),
            "style": (p.style.name if p.style else "") or "",
            "jc": attrs.get("jc"),
            "first_line_indent": attrs.get("w:firstLineChars") or attrs.get("w:firstLine"),
            **rp,
        })

    # ---- 标题：首个非空段；若首段是红头（机关标志，红且大），题名顺延到第二段 ----
    first = infos[0]
    body_sizes = [i["size_pt"] for i in infos[1:] if i["size_pt"]]
    body_size_mode = Counter(body_sizes).most_common(1)[0][0] if body_sizes else None
    # 红头判定：首段偏红且字号显著大于正文众数（"XXX文件"式发文机关标志）
    red_head = bool(
        first["red"]
        and first["size_pt"]
        and (body_size_mode is None or first["size_pt"] >= body_size_mode * 1.2)
        and first["text"]
    )
    title = infos[1] if (red_head and len(infos) > 1) else first
    # ---- 正文字体：众数字体（仅中文字体名参选）----
    body_font = None
    cjk_fonts = Counter(i["eastasia"] for i in infos if i["eastasia"])
    if cjk_fonts:
        body_font = cjk_fonts.most_common(1)[0][0]
    # ---- 标题字体：标题段中文字体；否则正文众数 ----
    title_font = _cjk_font_or_none(title["raw_font"]) or body_font
    # ---- 标题层字体：样式含 heading 且尺寸>正文的段 ----
    heading_fonts = [
        i["eastasia"] for i in infos
        if _HEADING_STYLE_RE.search(i["style"]) and i["eastasia"]
    ]
    heading_font = Counter(heading_fonts).most_common(1)[0][0] if heading_fonts else None

    layout: dict = {}
    if body_font:
        layout["font"] = body_font
    if title_font:
        layout["title_font"] = title_font
    if heading_font and heading_font != title_font:
        layout["heading_font"] = heading_font
    if red_head:
        layout["red_head"] = True

    # ---- 结构大纲：样式名判定优先，其次"引导符 + 短行"启发式 ----
    outline: list[dict] = []
    for i in infos[:120]:
        text = i["text"]
        if len(text) > 40:
            continue
        style_level = None
        if _HEADING_STYLE_RE.search(i["style"]):
            m = re.search(r"(\d)", i["style"])
            style_level = min(int(m.group(1)), 6) if m else 1
        elif _HEADING_LIKE_RE.match(text) and _OUTLINE_LEAD_RE.match(text):
            # 二级结构线索：一、/（一）/1. 开头的条目视为大纲项
            style_level = 2 if re.match(r"^\s*第[一二三四五六七八九十]+[章节条]", text) else 3
        if style_level and text and not _END_PUNCT_RE.search(text):
            outline.append({"level": style_level, "text": text[:60]})
        if len(outline) >= 40:
            break

    full_text = "\n".join(i["text"] for i in infos)
    opening = full_text[:300]
    closing = full_text[-300:]

    # ---- 人读版式摘要（注入提示词）----
    notes = [
        f"标题：{title['raw_font'] or '默认'} {_pt(title['size_pt'])}"
        + ("（居中）" if title["jc"] == "center" else ""),
        f"正文：{body_font or '默认'} {_pt(body_size_mode)}",
    ]
    if margins:
        notes.append(
            "页边距：上{} 下{} 左{} 右{} cm".format(
                margins.get("top_cm"), margins.get("bottom_cm"),
                margins.get("left_cm"), margins.get("right_cm"),
            )
        )
    if red_head:
        notes.append("含红头（发文机关标志为红色大字）")
    if heading_font:
        notes.append(f"层级标题字体：{heading_font}")
    if layout.get("font") and body_size_mode and abs(body_size_mode - 16) > 2:
        notes.append(f"正文字号{body_size_mode:g}磅（与公文三号16磅不同，请遵循参考文档）")

    return {
        "layout": layout or None,
        "outline": outline,
        "opening": opening,
        "closing": closing,
        "excerpt": full_text[:1500],
        "style_notes": "；".join(notes),
        "para_count": len(infos),
        "approx_words": len(re.sub(r"\s", "", full_text)),
        "has_real_layout": bool(layout),
    }


def _text_profile(fmt: str, text: str) -> dict:
    """PDF/OFD/TXT/MD 的文本画像（无版式，只有结构 + 行文风格）。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    outline: list[dict] = []
    for ln in lines[:200]:
        if len(ln) > 30:
            continue
        if _OUTLINE_LEAD_RE.match(ln):
            level = 2 if re.match(r"^\s*第[一二三四五六七八九十]+[章节条]", ln) else 3
            outline.append({"level": level, "text": ln[:60]})
        elif fmt == "md" and ln.startswith("#"):
            level = min(len(ln) - len(ln.lstrip("#")), 6)
            outline.append({"level": level, "text": ln.lstrip("# ").strip()[:60]})
        if len(outline) >= 40:
            break
    return {
        "layout": None,
        "outline": outline,
        "opening": text[:300],
        "closing": text[-300:],
        "excerpt": text[:1500],
        "style_notes": (
            f"参考文档为 {fmt.upper()} 格式，无法提取字体/边距等版式信息；"
            "成稿按公文标准版式（GB/T 9704）渲染，结构与行文风格参考下文摘录。"
        ),
        "para_count": len(lines),
        "approx_words": len(re.sub(r"\s", "", text)),
        "has_real_layout": False,
    }


def io_bytes(data: bytes):
    """bytes → 类文件对象（python-docx 需要 seekable stream）。"""
    import io

    return io.BytesIO(data)


def extract_reference_profile(filename: str, data: bytes) -> tuple[str, dict]:
    """解析上传参考文档，返回 (format, profile)。

    Raises:
        ValueError: 无法解析（空文件 / 格式不支持 / 解析异常）——调用方
            应落 FAILED 状态并把 message 透给用户。
    """
    from app.services.parser import _sniff_format as sniff_format

    lower = (filename or "").lower()
    if lower.endswith(".docx") or lower.endswith(".doc"):
        try:
            return "docx", _docx_profile(data)
        except ValueError:
            raise
        except Exception as exc:  # python-docx 解析失败等
            raise ValueError(f"DOCX 解析失败：{exc}") from exc
    if lower.endswith((".pdf", ".ofd", ".txt", ".md")):
        # 扩展名撒谎（如 .txt 实为 docx/ofd）时按内容纠偏
        if sniff_format(data) == "docx":
            return "docx", _docx_profile(data)
        try:
            parsed = parse_bytes(filename, data)
        except Exception as exc:
            raise ValueError(f"参考文档解析失败：{exc}") from exc
        if not (parsed.text or "").strip():
            raise ValueError("文档为空（未提取到文本）")
        # 规范格式名：txt/md 统一 text；pdf/ofd 取扩展名
        fmt = "text" if lower.endswith((".txt", ".md")) else lower.rsplit(".", 1)[-1]
        return fmt, _text_profile(fmt, parsed.text)
    # 无扩展名：按文件头嗅探（与入库路径同口径）
    fmt = sniff_format(data)
    if fmt == "docx":
        return "docx", _docx_profile(data)
    parsed = parse_bytes(filename or "doc", data)
    if not (parsed.text or "").strip():
        raise ValueError("文档为空（未提取到文本）")
    return fmt, _text_profile(fmt, parsed.text)


def render_reference_instructions(profile: dict, mode: str) -> str:
    """把画像渲染成成稿提示词里的【参考文档】指令块。

    mode:
        format —— 按参考文档的模板格式生成（版式 + 结构）；
        imitate —— 仿写（结构 + 语气 + 组织方式，行文摘录为主）；
        both —— 格式 + 仿写双重要求。
    """
    lines: list[str] = []
    outline = profile.get("outline") or []
    if mode in ("format", "both"):
        if profile.get("has_real_layout"):
            lines.append("【版式要求】严格遵循参考文档版式：")
            if profile.get("style_notes"):
                lines.append(f"- 版式摘要：{profile['style_notes']}")
            if outline:
                items = " → ".join(o["text"] for o in outline[:12])
                lines.append(f"- 结构骨架：{items}")
            lines.append("- 导出渲染将按上述字体/红头设置执行，正文请按对应层级组织标题。")
        else:
            # PDF/OFD/TXT 无版式画像：明示按公文标准版式，避免"选了格式却
            # 没有任何指令"的静默无操作
            lines.append(
                "【版式说明】参考文档无可提取的版式信息（非 DOCX），"
                "按公文标准版式（GB/T 9704）执行；结构与行文参考下文摘录。"
            )
    if mode in ("imitate", "both"):
        lines.append("【仿写要求】模仿参考文档的结构、语气与条款组织方式：")
        if profile.get("style_notes") and mode == "imitate":
            lines.append(f"- 风格参考：{profile['style_notes']}")
        if outline:
            items = "\n".join(
                f"  {'  ' * (o['level'] - 1)}- {o['text']}" for o in outline[:20]
            )
            lines.append(f"- 结构大纲：\n{items}")
        if profile.get("opening"):
            lines.append(f"- 开头写法：{profile['opening'][:200]}")
        if profile.get("closing"):
            lines.append(f"- 结尾写法：{profile['closing'][:200]}")
        if profile.get("excerpt"):
            lines.append(f"- 代表性段落：\n{profile['excerpt'][:900]}")
        lines.append("- 保持与参考文档一致的语体分寸（严肃/平实），不得照抄其具体事实内容。")
    if not lines:
        return ""
    return "\n".join(lines)
