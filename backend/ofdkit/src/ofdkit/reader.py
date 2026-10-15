"""OFD 包（ZIP）导航器：入口 → DocRoot → Document.xml → Page → Content.xml。

OFD（GB/T 33190）本质是一个 ZIP 包，典型结构：

```
OFD.xml                        # 入口：<DocBody><DocRoot>Doc_0/Document.xml</DocRoot>
Doc_0/Document.xml             # 文档：<Pages><Page BaseLoc="Page_0/Content.xml"/></Pages>
                                # + <CommonData><PageArea PhysicalBox="0 0 210 297"/>
Doc_0/Page_0/Content.xml       # 页内容：<Page><Content><Layer><TextObject>...
```

兼容策略（各厂商变体差异大，宁可多兜底不可漏页）：
- 入口缺失/损坏 → 回退按文件名找 `Document.xml`；
- Page 无 BaseLoc → 回退全局 `Content.xml` 按名排序（约定优于配置）；
- 命名空间前缀不一 → 一律按 XML **本地名**匹配，忽略命名空间；
- 相对路径 → 相对文档目录解析，失败时按包根 + 尾匹配兜底。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET


def _local(tag: object) -> str:
    """去 XML 命名空间前缀：'{http://...}TextCode' -> 'TextCode'。"""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _iter_local(root: ET.Element, name: str) -> list[ET.Element]:
    """遍历返回所有本地名等于 name 的元素（忽略命名空间）。"""
    return [el for el in root.iter() if _local(el.tag) == name]


@dataclass(frozen=True)
class PageRef:
    """一个页内容文件的引用：包内条目名 + 物理页尺寸（mm）。"""

    entry: str
    physical_box: tuple[float, float] | None  # (宽, 高)，None=未知（按 A4 兜底）


class OFDPackage:
    """OFD ZIP 包导航器（只读，构造时解析入口，不缓存 XML 树）。"""

    def __init__(self, data: bytes) -> None:
        self._zf = zipfile.ZipFile(io.BytesIO(data))
        # 统一分隔符：Windows 打包工具可能用反斜杠
        self._names: list[str] = [n.replace("\\", "/") for n in self._zf.namelist()]
        self._name_set: set[str] = set(self._names)

    def close(self) -> None:
        self._zf.close()

    def __enter__(self) -> OFDPackage:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------
    def _resolve(self, rel: str, base_dir: str) -> str | None:
        """把包内相对路径解析为实际条目名（相对文档目录 → 包根 → 尾匹配兜底）。"""
        rel = rel.lstrip("/")
        for cand in (f"{base_dir}/{rel}" if base_dir else rel, rel):
            if cand in self._name_set:
                return cand
        return next((n for n in self._names if n.endswith(rel)), None)

    # ------------------------------------------------------------------
    # 文档定位
    # ------------------------------------------------------------------
    def _doc_entries(self) -> list[str]:
        """定位全部 Document.xml 入口（支持一包多文档的 DocRoot 列表）。"""
        entry = next((n for n in self._names if n.split("/")[-1].lower() == "ofd.xml"), None)
        paths: list[str] = []
        if entry:
            try:
                root = ET.fromstring(self._zf.read(entry))
                for dr in _iter_local(root, "DocRoot"):
                    loc = (dr.text or dr.get("BaseLoc") or "").strip()
                    if loc:
                        paths.append(loc)
            except (ET.ParseError, KeyError, OSError):
                pass  # 入口损坏：走文件名兜底
        if not paths:
            paths = [n for n in self._names if n.split("/")[-1].lower() == "document.xml"]
        return paths

    def _physical_box(self, doc_entry: str) -> tuple[float, float] | None:
        """读取文档级物理页尺寸（CommonData/PageArea/@PhysicalBox），A4 兜底由调用方处理。"""
        try:
            root = ET.fromstring(self._zf.read(doc_entry))
        except (ET.ParseError, KeyError, OSError):
            return None
        for pa in _iter_local(root, "PageArea"):
            box = (pa.get("PhysicalBox") or "").split()
            if len(box) == 4:
                try:
                    return (float(box[2]), float(box[3]))
                except ValueError:
                    continue
        return None

    def pages(self) -> list[PageRef]:
        """按阅读顺序返回全部页内容文件引用。"""
        refs: list[PageRef] = []
        for doc_path in self._doc_entries():
            doc_entry = self._resolve(doc_path, "")
            if not doc_entry:
                continue
            try:
                doc_root = ET.fromstring(self._zf.read(doc_entry))
            except (ET.ParseError, KeyError, OSError):
                continue
            base_dir = doc_entry.rsplit("/", 1)[0] if "/" in doc_entry else ""
            box = self._physical_box(doc_entry)

            # 页引用：优先 Page/@BaseLoc；无则全局 Content.xml 排序兜底
            page_refs: list[str] = []
            for pg in _iter_local(doc_root, "Page"):
                loc = (pg.get("BaseLoc") or "").strip()
                if loc:
                    page_refs.append(loc)
            if not page_refs:
                page_refs = sorted(n for n in self._names if n.split("/")[-1].lower() == "content.xml")
                base_dir = ""  # 全局匹配时按包根解析
            for ref in page_refs:
                cand = self._resolve(ref, base_dir)
                if cand:
                    refs.append(PageRef(entry=cand, physical_box=box))
        return refs

    def read(self, entry: str) -> bytes:
        return self._zf.read(entry)


def looks_like_ofd(data: bytes) -> bool:
    """ZIP 包是否为 OFD（GB/T 33190）。

    DOCX 与 OFD 均为 PK 魔数的 ZIP，仅凭魔数无法区分；必须解包看内部
    结构：存在 OFD.xml 入口，或任一 XML 含 ofdspec 命名空间。主服务的
    格式嗅探（无扩展名文件分发）复用本函数，避免双份真相。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n.replace("\\", "/") for n in zf.namelist()]
            if any(n.split("/")[-1].lower() == "ofd.xml" for n in names):
                return True
            for n in names:
                if n.lower().endswith(".xml"):
                    return b"ofdspec.org" in zf.read(n)[:1024]
    except (zipfile.BadZipFile, KeyError, OSError):
        return False
    return False
