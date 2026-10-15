"""CLI：python -m ofdkit <file.ofd> [--no-clean] [--raw]

调试用途：打印提取正文 + 统计信息，或输出未清洗的原始行对照。
"""

from __future__ import annotations

import argparse
import sys

from ofdkit import extract_pages, extract_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ofdkit", description="OFD (GB/T 33190) 文本内容提取")
    parser.add_argument("file", help="OFD 文件路径")
    parser.add_argument("--no-clean", action="store_true", help="关闭页码/页眉清洗")
    parser.add_argument("--no-fix", action="store_true", help="关闭乱码字符修复")
    parser.add_argument("--raw", action="store_true", help="打印未清洗的逐页原始行")
    args = parser.parse_args(argv)

    data = open(args.file, "rb").read()

    if args.raw:
        for pg in extract_pages(data):
            print(f"===== Page {pg.index + 1} =====")
            for ln in pg.lines:
                print(ln)
        return 0

    result = extract_text(data, clean_noise=not args.no_clean, fix_glyphs=not args.no_fix)
    print(result.text)
    print(
        f"---- pages={result.pages} scanned_pages={result.scanned_pages} "
        f"is_scanned={result.is_scanned} removed_lines={len(result.removed_lines)} ----",
        file=sys.stderr,
    )
    for ln in result.removed_lines:
        print(f"  [removed] {ln!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
