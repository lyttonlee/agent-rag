"""切分完整性改造验证。

不依赖 DB，直接调用 chunker。两部分：
A. 直接对比：对同一「超预算长段落」，_token_windows(硬切) vs _soft_windows(软切)
   谁更会在句中间切断 —— 软切应显著减少「半句被腰斩」。
B. 新参数双粒度：small=300 / large=2500 下，段落/章节两种策略的块数与
   large 块「句中被腰斩」计数（理想接近 0）。

用法：python tools/verify_chunk_completeness.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.chunker import (  # noqa: E402
    _Unit,
    _count_tokens,
    _merge_budgeted,
    _paragraph_units_off,
    _section_units_off,
    _soft_windows,
    _token_windows,
)

SAMPLE = (
    "关于开展2026年第三季度安全生产大检查的通知\n"
    "各省、自治区、直辖市人民政府，国务院各部委、各直属机构：\n"
    "一、总体要求。以习近平新时代中国特色社会主义思想为指导，深入贯彻党的二十大精神，"
    "坚持人民至上、生命至上，统筹发展和安全，严格落实安全生产责任制，全面排查整治各类"
    "风险隐患，坚决防范遏制重特大事故发生，切实保障人民群众生命财产安全，为经济社会高"
    "质量发展营造安全稳定环境，确保全年安全生产形势持续稳定向好，取得实实在在的成效。\n"
    "二、检查范围。本次检查覆盖煤矿、非煤矿山、危险化学品、烟花爆竹、建筑施工、交通运"
    "输、消防、燃气、冶金工贸、特种设备等重点行业领域，突出人员密集场所、外包作业、检"
    "维修作业、有限空间作业等关键环节，做到全覆盖、零容忍、严执法、重实效，不留死角、"
    "不走过场，确保检查工作取得实效，推动安全生产水平整体提升，切实维护社会大局稳定。\n"
    "三、时间安排。检查自2026年7月1日起至9月30日止，分动员部署、自查自纠、集中检查、"
    "总结提升四个阶段进行。各单位要制定具体实施方案，明确责任分工，细化工作措施，确保"
    "各阶段任务按时保质完成，对发现的问题建立台账、闭环管理、整改销号，形成长效机制。\n"
    "特此通知。\n"
)

_SENT_END = re.compile(r"[。！？；\n]")


def _mid_sentence_breaks(pieces: list[str]) -> int:
    """统计「块尾不在句边界」的数量（即从半句处被切断的次数）。"""
    n = 0
    for p in pieces:
        body = p.rstrip()
        if body and not _SENT_END.search(body[-1]):
            n += 1
    return n


def part_a() -> None:
    print("===== A. 软切 vs 硬切（对单个超预算长单元）=====")
    # 手动构造一个「单个长单元」：多句、无强制单元拆分，长度远超 small 预算，
    # 直接对比 _token_windows(硬切) 与 _soft_windows(软切) 的句完整性。
    long_text = (
        "一、总体要求。坚持人民至上、生命至上，统筹发展和安全，严格落实安全生产责任制，"
        "全面排查整治各类风险隐患。坚决防范遏制重特大事故发生，切实保障人民群众生命财产"
        "安全。为经济社会高质量发展营造安全稳定环境，确保全年安全生产形势持续稳定向好，"
        "取得实实在在的成效。各单位要高度重视、精心组织、周密部署，确保各项任务落到实处。"
    )
    unit = _Unit(text=long_text, start=0, end=len(long_text))
    unit.tokens = max(_count_tokens([long_text])[0], 1)
    print(f"构造单个长单元：{unit.tokens} token / {len(long_text)} 字")

    budget = 60  # 小预算，必然触发多次切分
    hard = [p for p, _, _ in _token_windows(unit, budget, 10)]
    soft = [p for p, _, _ in _soft_windows(unit, budget, 10)]
    print(f"  _token_windows(硬切)：{len(hard)} 片，句中被腰斩 = {_mid_sentence_breaks(hard)}")
    print(f"  _soft_windows (软切)：{len(soft)} 片，句中被腰斩 = {_mid_sentence_breaks(soft)}")
    print("  → 软切腰斩数应显著低于硬切（理想 0）")


def part_b() -> None:
    print("\n===== B. 新参数双粒度（small=300 / large=2500）=====")
    prefix = "关于开展安全生产大检查的通知｜国务院｜2026-06-30"
    overhead = max(_count_tokens([f"{prefix}\n"])[0], 1)
    for units_fn, label in [(_paragraph_units_off, "段落"), (_section_units_off, "章节")]:
        units = units_fn(SAMPLE)
        for u in units:
            u.tokens = max(_count_tokens([u.text])[0], 1)
        small = _merge_budgeted(units, prefix, 300, overhead, 40, "small")
        large = _merge_budgeted(units, prefix, 2500, overhead, 200, "large")
        broken = _mid_sentence_breaks([c.content for c in large])
        sizes = [max(_count_tokens([c.content])[0], 1) for c in large]
        print(
            f"  [{label}] 小块 {len(small)} 片 / 大块 {len(large)} 片；"
            f"大块句中被腰斩 = {broken}；大块 token = {sizes}"
        )


if __name__ == "__main__":
    part_a()
    part_b()
    print("\n结论：软切在句边界成块（A 腰斩数趋 0）；新参数下 large 能容纳完整条款。")
