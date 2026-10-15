"""OFD 测试样本构造器：把 (x, y, text) 运行序列打包成合法的最小 OFD 文件。

作为库的公开测试工具（``ofdkit.testing``）随包分发：主服务测试套件与
库自有测试共用同一构造器，避免双份真相。

覆盖真实 OFD 结构要点：
- OFD.xml 入口 → DocRoot → Document.xml；
- Document.xml 的 CommonData/PageArea（物理页尺寸，页眉脚分区用）；
- Pages/Page@BaseLoc → Content.xml；
- Content.xml 的 Page/Content/Layer/TextObject/TextCode；
- 可选变体：无命名空间、无入口文件、TextCode 无坐标（回退 TextObject Boundary）。
"""

from __future__ import annotations

import io
import zipfile

OFD_NS = "http://www.ofdspec.org/2016"


def _decls(no_namespace: bool) -> tuple[str, str]:
    """返回 (前缀, xmlns声明)。no_namespace 时前缀为空、无 xmlns 声明。"""
    if no_namespace:
        return "", ""
    return "ofd:", f' xmlns:ofd="{OFD_NS}"'


def _ofd_xml(no_namespace: bool) -> str:
    p, d = _decls(no_namespace)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<{p}OFD{d}>\n"
        f"  <{p}DocBody>\n"
        f"    <{p}DocRoot>Doc_0/Document.xml</{p}DocRoot>\n"
        f"  </{p}DocBody>\n"
        f"</{p}OFD>\n"
    )


def _document_xml(n_pages: int, box: tuple[float, float, float, float], no_namespace: bool) -> str:
    p, d = _decls(no_namespace)
    pages = "\n".join(f'    <{p}Page BaseLoc="Page_{i}/Content.xml"/>' for i in range(n_pages))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<{p}Document{d}>\n"
        f"  <{p}CommonData>\n"
        f'    <{p}PageArea PhysicalBox="{box[0]} {box[1]} {box[2]} {box[3]}"/>\n'
        f"  </{p}CommonData>\n"
        f"  <{p}Pages>\n{pages}\n  </{p}Pages>\n"
        f"</{p}Document>\n"
    )


def _content_xml(
    runs: list[tuple[float, float, str]],
    *,
    with_coords: bool,
    no_namespace: bool,
    local_offset: tuple[float, float] = (0.0, 0.0),
) -> str:
    """runs: [(x, y, text)]，按文档顺序写入（可用于模拟乱序来源）。

    **真实语义**（与 GB/T 33190 及实测公文一致）：
    TextObject Boundary 的左上角 = 片段的页面**绝对**位置 (x, y)；
    TextCode 的 X/Y = 相对 Boundary 的**局部**偏移（真实公文普遍写 0.0）。
    ``local_offset`` 可注入非零局部偏移，用于验证"局部坐标叠加"逻辑。
    """
    p, d = _decls(no_namespace)
    objs = []
    for i, (x, y, text) in enumerate(runs):
        if with_coords:
            coord = f' X="{local_offset[0]}" Y="{local_offset[1]}"'
        else:
            coord = ""
        objs.append(
            f'    <{p}TextObject ID="{i}" Boundary="{x} {y} 100 10">\n'
            f"      <{p}TextCode{coord}>{text}</{p}TextCode>\n"
            f"    </{p}TextObject>"
        )
    body = "\n".join(objs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<{p}Page{d}>\n"
        f"  <{p}Content>\n"
        f'    <{p}Layer Type="Body">\n{body}\n    </{p}Layer>\n'
        f"  </{p}Content>\n"
        f"</{p}Page>\n"
    )


def make_ofd(
    pages: list[list[tuple[float, float, str]]],
    *,
    box: tuple[float, float, float, float] = (0.0, 0.0, 210.0, 297.0),
    with_entry: bool = True,
    with_coords: bool = True,
    no_namespace: bool = False,
    local_offset: tuple[float, float] = (0.0, 0.0),
) -> bytes:
    """构造最小 OFD ZIP 包字节流。

    Args:
        pages: 每页的 [(x, y, text)] 运行序列——(x, y) 作为 TextObject
            Boundary 左上角，即片段的**页面绝对位置**。
        box: 物理页尺寸 (x, y, 宽, 高)，默认 A4。
        with_entry: 是否写 OFD.xml 入口（False 测文件名兜底）。
        with_coords: TextCode 是否带 X/Y（False 测 Boundary 回退）。
        no_namespace: 是否去命名空间（测本地名匹配）。
        local_offset: TextCode X/Y 写入的**局部偏移**（相对 Boundary），
            默认 (0, 0) —— 与真实公文一致；传非零值可验证坐标叠加逻辑。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if with_entry:
            zf.writestr("OFD.xml", _ofd_xml(no_namespace))
        zf.writestr("Doc_0/Document.xml", _document_xml(len(pages), box, no_namespace))
        for i, runs in enumerate(pages):
            zf.writestr(
                f"Doc_0/Page_{i}/Content.xml",
                _content_xml(
                    runs,
                    with_coords=with_coords,
                    no_namespace=no_namespace,
                    local_offset=local_offset,
                ),
            )
    return buf.getvalue()
