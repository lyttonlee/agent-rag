"""远程联调冒烟测试：直连 192.168.6.49 的 PG / Redis，验证核心链路。

覆盖范围：
  1) 存储抽象层（local 模式）：save/get/delete/exists/size + 目录穿越防护
  2) 入库：解析 → 切片 → 向量化(模拟) → 落库，且原文持久化到存储后端
  3) 检索权限下推（修正后语义：公开/空标签全局可见，带标签需相交）
  4) 中文分词 search_vec（zhparser 触发器 / 入库生成）真实可用
  5) 生成敏感词掩码（sensitive_hits 回报）

模型以纯 Python 桩替换（关键词 one-hot 向量 + 余弦打分 + 模板生成），
不依赖真实 GPU 模型，专注验证逻辑链路与数据库行为。

运行：uv run python scripts/smoke_remote.py
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import tempfile

# ---- 必须在 import app 之前注入环境变量（settings 顶层缓存）----
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

_TMP = tempfile.mkdtemp(prefix="govdoc_smoke_")
os.environ.update(
    {
        "PG_HOST": "192.168.6.49",
        "PG_PORT": "5432",
        "PG_USER": "rag",
        "PG_PASSWORD": "rag123456",
        "PG_DATABASE": "gov_doc_rag",
        "STORAGE_PROVIDER": "local",
        "STORAGE_ROOT": _TMP,
        "ENVIRONMENT": "test",
        "REDIS_URL": "redis://192.168.6.49:6379/0",
    }
)

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.db.session import AsyncSessionLocal  # noqa: E402
from app.core.security import redact_sensitive  # noqa: E402
from app.services import generation, ingestion, model_provider, retrieval  # noqa: E402
from app.services.storage import LocalFSStorage, get_storage  # noqa: E402
from app.schemas.contracts import GenerateRequest, RetrieveRequest  # noqa: E402

# ---------------------------------------------------------------------------
# 模型桩：关键词 one-hot 向量（语义可控，便于断言权限下推而非语义）
# ---------------------------------------------------------------------------
KEYWORDS = ["汛期", "安全", "内部", "值班", "保密", "项目", "通知", "报告", "请示"]
# 必须与 chunks.embedding 列 Vector(embedding_dim) 维度一致（当前 1024），否则 HNSW 索引拒绝插入
_EMB_DIM = get_settings().embedding_dim


def _vec(text: str) -> list[float]:
    v = [text.count(k) for k in KEYWORDS]
    full = [0.0] * _EMB_DIM
    for i, x in enumerate(v):
        if i < _EMB_DIM:
            full[i] = x
    norm = math.sqrt(sum(x * x for x in full)) or 1.0
    return [x / norm for x in full]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


async def fake_embed(texts):
    return [_vec(t) for t in texts]


async def fake_rerank(query, docs, top_n=None):
    qv = _vec(query)
    return [_cosine(qv, _vec(d)) for d in docs]


async def fake_generate(messages, temperature=0.3, max_tokens=2048):
    # 故意包含敏感词以验证后处理掩码
    return (
        "根据检索资料，保密项目须严格管理并控制知悉范围【1】。"
        "汛期安全防范工作通知如下：各单位应加强隐患排查【2】。"
    )


model_provider.ModelProvider.embed = staticmethod(fake_embed)  # type: ignore[assignment]
model_provider.ModelProvider.rerank = staticmethod(fake_rerank)  # type: ignore[assignment]
model_provider.ModelProvider.generate = staticmethod(fake_generate)  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 测试主体
# ---------------------------------------------------------------------------
_RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name}" + (f" -- {detail}" if detail else ""))


async def test_storage() -> None:
    s = LocalFSStorage(root=os.path.join(_TMP, "storage_test"))
    key = "raw/test.pdf"
    s.save(key, b"hello-minio-replacement")
    assert s.get(key) == b"hello-minio-replacement"
    assert s.exists(key)
    assert s.size(key) == len(b"hello-minio-replacement")
    s.delete(key)
    assert not s.exists(key)
    # 目录穿越防护
    for bad in ["../evil.pdf", "/abs/path.pdf", "a/../../b"]:
        try:
            s._safe_path(bad)
            _check("storage-traversal-guard", False, f"未拦截非法键 {bad!r}")
            return
        except ValueError:
            pass
    _check("storage-local-ops", True, f"root={_TMP}")
    _check("storage-traversal-guard", True)


async def test_permission_downpush(db) -> None:
    # 清空业务表（asyncpg 不支持单语句多命令，分两条执行）
    await db.execute(text("DELETE FROM chunks"))
    await db.execute(text("DELETE FROM documents"))
    await db.commit()

    docs = [
        {
            "external_id": "A", "title": "A 汛期安全通知", "doc_type": "通知",
            "security_level": "PUBLIC", "permission_tags": [],
            "owner_org": "总部", "source_system": "smoke",
            "content": "关于汛期安全的通知，各部门做好安全防范和应急准备。汛期安全事关重大。",
        },
        {
            "external_id": "B", "title": "B 内部值班", "doc_type": "通知",
            "security_level": "INTERNAL", "permission_tags": ["scope:dept:总部"],
            "owner_org": "总部", "source_system": "smoke",
            "content": "内部值班安排，内部人员值班表已下发。内部值班需严格遵守纪律。",
        },
        {
            "external_id": "C", "title": "C 保密项目", "doc_type": "报告",
            "security_level": "RESTRICTED", "permission_tags": ["scope:dept:保密处"],
            "owner_org": "保密处", "source_system": "smoke",
            "content": "保密项目实施方案，保密项目管理要求严格。保密项目不得外泄。",
        },
    ]
    mapping = {}
    for d in docs:
        res = await ingestion.ingest_document(db, d, raw_bytes=d["content"].encode("utf-8"))
        _check(
            f"ingest-{d['external_id']}",
            res["status"] == "INDEXED" and res["chunk_count"] >= 1,
            f"chunks={res['chunk_count']}",
        )
        mapping[d["external_id"]] = res["document_id"]

    # 原文持久化校验（storage 抽象层）
    a_ref = (
        await db.execute(text("SELECT content_ref FROM documents WHERE external_id='A'"))
    ).fetchone()[0]
    try:
        raw_back = get_storage().get(a_ref)
        _check("storage-persist-raw", raw_back == docs[0]["content"].encode("utf-8"), a_ref)
    except Exception as e:  # noqa: BLE001
        _check("storage-persist-raw", False, str(e))

    # 中文分词 search_vec 真实可用（zhparser 触发器 / 入库生成）
    cnt = (
        await db.execute(
            text("SELECT count(*) FROM chunks WHERE search_vec @@ plainto_tsquery('chinese','汛期')")
        )
    ).fetchone()[0]
    _check("search_vec-zhparser", cnt >= 1, f"命中中文分词 {cnt} 片")

    # 检索权限下推
    async def retrieve(clearance, scope):
        req = RetrieveRequest(
            query="汛期安全通知", caller_scope=scope, max_clearance=clearance, rerank=True
        )
        resp = await retrieval.hybrid_retrieve(db, req)
        seen = {c.document_id for c in resp.citations}
        return resp, seen

    r1, seen1 = await retrieve("PUBLIC", [])
    _check(
        "downpush-ctx1-public",
        seen1 <= {mapping["A"]} and bool(seen1),
        f"可见={seen1}",
    )

    r2, seen2 = await retrieve("INTERNAL", ["scope:dept:总部"])
    _check(
        "downpush-ctx2-internal",
        seen2 <= {mapping["A"], mapping["B"]} and mapping["C"] not in seen2,
        f"可见={seen2}",
    )

    r3, seen3 = await retrieve("SECRET", ["scope:dept:保密处"])
    _check(
        "downpush-ctx3-secret",
        seen3 <= {mapping["A"], mapping["B"], mapping["C"]} and mapping["B"] not in seen3,
        f"可见={seen3}",
    )


async def test_generation_redact() -> None:
    # 单元：敏感词掩码
    text = "涉及保密项目的内部安排"
    out, hits = redact_sensitive(text, ["保密项目"])
    _check("redact-unit", "保密项目" not in out and "保密项目" in hits, f"out={out} hits={hits}")
    # 端到端（经生成链路）
    req = GenerateRequest(
        query="汛期安全通知", caller_scope=["scope:dept:总部"], max_clearance="INTERNAL",
        sensitive_words=["保密项目"],
    )
    async with AsyncSessionLocal() as db:
        resp = await generation.generate_answer(db, req)
        ok = "保密项目" not in resp.answer and "保密项目" in resp.sensitive_hits
        _check("generate-redact-e2e", ok, f"hits={resp.sensitive_hits}")


async def main() -> int:
    await test_storage()
    async with AsyncSessionLocal() as db:
        await test_permission_downpush(db)
    await test_generation_redact()

    failed = [n for n, ok, _ in _RESULTS if not ok]
    print("\n==================== 冒烟结果 ====================")
    print(f"总计 {len(_RESULTS)}，通过 {len(_RESULTS) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        import shutil

        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
