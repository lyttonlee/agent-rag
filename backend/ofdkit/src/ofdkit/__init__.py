"""ofdkit 公开 API。

OFD（GB/T 33190 开放版式文档）文本内容提取库，零三方依赖。

主用法::

    from ofdkit import extract_text
    result = extract_text(open("doc.ofd", "rb").read())
    print(result.text)

格式无关组件（主服务 PDF/DOCX 链路复用，避免双份真相）::

    from ofdkit.normalize import normalize_text, FONT_GLYPH_FIXES
    from ofdkit.noise import filter_noise_lines, is_page_number
"""

from ofdkit.extract import extract_pages, extract_text
from ofdkit.model import ExtractResult, OFDPage, TextRun
from ofdkit.noise import Line, filter_noise_lines, is_page_number
from ofdkit.normalize import (
    FONT_GLYPH_FIXES,
    SUSPECT_RANGES,
    fix_font_glyphs,
    normalize_text,
    to_halfwidth_alnum,
)
from ofdkit.reader import OFDPackage, PageRef, looks_like_ofd
from ofdkit.testing import make_ofd
from ofdkit.textorder import DEFAULT_Y_TOLERANCE, runs_to_lines

__version__ = "1.0.0"

__all__ = [
    # 主编排
    "extract_text",
    "extract_pages",
    # 模型
    "ExtractResult",
    "OFDPage",
    "TextRun",
    # 包导航
    "OFDPackage",
    "PageRef",
    "looks_like_ofd",
    # 测试样本构造（公开测试工具，主服务测试套件共用）
    "make_ofd",
    # 阅读顺序
    "runs_to_lines",
    "DEFAULT_Y_TOLERANCE",
    # 噪音剔除（格式无关）
    "Line",
    "filter_noise_lines",
    "is_page_number",
    # 规范化（格式无关）
    "normalize_text",
    "fix_font_glyphs",
    "to_halfwidth_alnum",
    "FONT_GLYPH_FIXES",
    "SUSPECT_RANGES",
    "__version__",
]
