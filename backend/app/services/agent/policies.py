"""Agent 话术策略（policies）：闲聊 / 超范围 / 无检索结果的固定应答。

固定话术而非模型生成的三个理由：
1. **零编造风险**：direct_qa 无材料、超范围问题由确定性文案应答，
   从机制上保证"不能编造"（用户 2026-10-13 明确的硬性要求）；
2. **零延迟**：寒暄与拒答不值得一次模型往返，即时响应体验更好；
3. **可测试**：文案即契约，回归测试直接断言。

流式化处理：``stream_text`` 把固定文案按小片段产出，保持与模型流式
一致的"逐字出现"体验，前端无需区分"这是固定话术"。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator

# 向量路 top1 余弦距离阈值：超过即认为语义上也不相关。
# 标定依据（Qwen3-Embedding-4B，30 篇公文语料实测）：
#   主题相关查询 ≈0.28；无关查询 ≈0.64~0.70；边缘查询 ≈0.59。
# 取 0.60：宁可将边缘查询放行给模型（提示词纪律兜底"依据不足"），
# 也不误杀可能有用的材料——直答拒答是硬行为，必须保守。
_VEC_DISTANCE_THRESHOLD = 0.60

# 任务型意图（draft / complex_task）在无检索材料时，由模型自身能力完成，
# 答案末尾必须追加的甄别声明（用户明确要求：需注明需用户自行甄别生成内容）。
TASK_DISCLAIMER = (
    "\n\n———\n"
    "以上内容基于 AI 通用能力生成，未经知识库材料印证，可能存在偏差，"
    "请自行甄别后再使用。"
)

# direct_qa 无检索结果：委婉说明没有相关内容，给出可操作建议，不编造
NO_RESULT_ANSWER = (
    "抱歉，我在当前知识库中暂未检索到与您问题相关的内容，因此无法给出确有依据的回答。\n\n"
    "建议您：\n"
    "1. 调整左上角「知识范围」，扩大或更换文档空间后重试；\n"
    "2. 换用更具体的关键词（如系统名称、文号、科室名称）再问一次；\n"
    "3. 若确有该方面制度文件，可联系知识库管理员补充入库。\n\n"
    "待相关知识文档入库后，我可以基于材料为您提供带引用的准确回答。"
)

# out_of_scope：明确拒答 + 说明能力边界，不编造
OUT_OF_SCOPE_ANSWER = (
    "抱歉，这个问题超出了企业公文知识库的能力范围，我无法为您回答。\n\n"
    "我可以帮您：\n"
    "1. 基于知识库文档回答制度、流程、规范类问题（带引用来源）；\n"
    "2. 按文种模板起草、修改公文（通知/通报/报告/请示/函/纪要/决定/意见）；\n"
    "3. 对多份文档做对比、汇总、排查等复杂任务（分步执行、过程可见）。"
)

# 闲聊：按模式选话术（零延迟、零成本）
_CHITCHAT_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"谢谢|感谢|多谢|辛苦|麻烦了"),
        "不客气！如果回答中有引用标注，您可以点开发文号核对原文。还有其他问题，随时问我。",
    ),
    (
        re.compile(r"再见|拜拜|bye"),
        "再见！会话记录已保存，随时可以继续。",
    ),
    (
        re.compile(r"你是谁|你叫什么|自我介绍|你能做什么|你会什么"),
        (
            "我是企业公文知识库的智能问答助手：基于库内制度文档回答制度、流程、规范类问题"
            "（回答附带引用来源），也可以按文种模板协助起草公文。\n"
            "复杂任务（对比、汇总、排查）我会拆解成步骤分步完成，执行过程对您可见。"
        ),
    ),
    (
        re.compile(r"你好|您好|hi|hello|嗨|喂"),
        (
            "您好！我是企业公文知识库助手。请直接提问，我会基于知识库文档给出带引用的回答；"
            "也可以让我按文种起草公文。"
        ),
    ),
]
_CHITCHAT_FALLBACK = "我在。有知识库相关的问题，随时问我。"


def pick_chitchat_reply(query: str) -> str:
    """按寒暄模式选固定话术（无命中用兜底文案）。"""
    text = (query or "").strip()
    for pattern, reply in _CHITCHAT_RULES:
        if pattern.search(text):
            return reply
    return _CHITCHAT_FALLBACK


def is_no_useful_info(retrieve_resp) -> bool:
    """判定一次检索是否"没有检索到有用/事实信息"（用户 2026-10-13 硬性要求）。

    双信号同时异常才判定为无有用信息（保守策略，防误杀）：
    1. 引用为空（空间无文档 / 全被密级权限过滤）；或
    2. 向量路 top1 距离 > 阈值 **且** 关键词路零命中——语义与词汇
       两个维度都指向"语料里没有这个东西"。

    单信号弱异常（如仅距离偏高但词汇有重叠，或反之）时放行：
    材料可能以 paraphrasing 覆盖问题，交给模型提示词纪律处理
    （"依据不足，建议核实"），避免把可能有用的材料一刀切拒答。

    该判定不依赖 rerank（距离与分词信号在 rerank 降级时依然有效）。
    """
    if not retrieve_resp.citations:
        return True
    distance = getattr(retrieve_resp, "top_vector_distance", None)
    keyword_hits = getattr(retrieve_resp, "keyword_hits", 0) or 0
    if distance is not None and keyword_hits == 0 and distance > _VEC_DISTANCE_THRESHOLD:
        return True
    return False


async def stream_text(text: str, chunk_size: int = 6, delay: float = 0.02) -> AsyncGenerator[str, None]:
    """把固定文案按小片段产出，模拟流式逐字效果（保持前端体验一致）。"""
    if not text:
        return
    for i in range(0, len(text), chunk_size):
        yield text[i : i + chunk_size]
        if delay > 0:
            await asyncio.sleep(delay)
