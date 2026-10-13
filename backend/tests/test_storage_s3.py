"""存储抽象层单测：LocalFS 与 S3（SeaweedFS 兼容）双后端。

S3 部分使用 moto（AWS 生态标准 mock 库）在内存中模拟 S3 服务，
无需真实 SeaweedFS 即可验证：读写往返、bucket 自动创建、404 语义、
path-style 寻址配置。联调环境下的真实 SeaweedFS 验证见
docs/SeaweedFS部署与联调指南.md。

运行：uv run pytest tests/test_storage_s3.py -v
"""

from __future__ import annotations

import pytest

from app.services import storage as storage_mod
from app.services.storage import LocalFSStorage, S3Storage, reset_storage

# moto 对 S3 兼容存储（SeaweedFS/Ceph 等）的官方支持方式：
# 通过 MOTO_S3_CUSTOM_ENDPOINTS 声明自定义端点，moto 会将其改写为
# s3.amazonaws.com 并拦截请求（否则带 endpoint_url 的请求会直连真实网络）。
# 本环境设有 http_proxy 且 NO_PROXY 为空，未拦截的请求会穿代理 502。
S3_ENDPOINT = "http://127.0.0.1:8333"


@pytest.fixture(autouse=True)
def _moto_s3_endpoint(monkeypatch):
    """模块内所有用例：向 moto 注册我们的 S3 端点。"""
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", S3_ENDPOINT)


# =========================================================================
# LocalFS
# =========================================================================

class TestLocalFS:
    def test_roundtrip(self, tmp_path):
        """保存→读取→存在→大小→删除 全往返。"""
        s = LocalFSStorage(root=str(tmp_path))
        key = "raw/abc123.pdf"
        assert s.save(key, b"%PDF-1.4 fake") == key
        assert s.get(key) == b"%PDF-1.4 fake"
        assert s.exists(key) is True
        assert s.size(key) == len(b"%PDF-1.4 fake")
        s.delete(key)
        assert s.exists(key) is False

    def test_missing_key_raises(self, tmp_path):
        """不存在的键：get/size 抛 FileNotFoundError，exists 返回 False。"""
        s = LocalFSStorage(root=str(tmp_path))
        with pytest.raises(FileNotFoundError):
            s.get("raw/nope.pdf")
        assert s.exists("raw/nope.pdf") is False

    def test_path_traversal_rejected(self, tmp_path):
        """目录穿越（..、绝对路径）必须拒绝——防越权读写。"""
        s = LocalFSStorage(root=str(tmp_path))
        for bad in ("../escape.txt", "/etc/passwd", "raw/../../x"):
            with pytest.raises(ValueError):
                s.save(bad, b"x")

    def test_ping(self, tmp_path):
        """ping 探测根目录可写。"""
        s = LocalFSStorage(root=str(tmp_path))
        assert s.ping() is True

    def test_factory_local(self, tmp_path, monkeypatch):
        """工厂按 settings 返回 local 后端；storage_info 摘要正确。"""
        monkeypatch.setattr(storage_mod, "_settings", type(
            "S", (), {"storage_provider": "local", "storage_root": str(tmp_path)},
        )())
        reset_storage()
        s = storage_mod.get_storage()
        assert isinstance(s, LocalFSStorage)
        info = storage_mod.storage_info()
        assert info["provider"] == "local"
        assert info["ok"] is True
        assert info["detail"]["root"] == str(tmp_path)
        reset_storage()


# =========================================================================
# S3（moto 内存 mock，模拟 SeaweedFS S3 行为）
# =========================================================================

@pytest.fixture()
def s3_storage():
    """在 moto mock 的 S3 上构造 S3Storage（模拟 SeaweedFS 端点）。

    注意：fixture 内直接实例化 S3Storage（不经工厂单例），
    确保 bucket 自动创建逻辑在无预建桶的场景下被覆盖。
    """
    from moto import mock_aws

    with mock_aws():
        s = S3Storage(
            endpoint="127.0.0.1:8333",
            bucket="gov-doc",
            access_key="admin",
            secret_key="admin123",
            region="us-east-1",
            secure=False,
            addressing_style="path",
        )
        yield s

class TestS3:
    def test_bucket_auto_created(self, s3_storage):
        """构造时桶不存在 → ensure_bucket 自动创建（联调免手工建桶）。"""
        import boto3

        client = boto3.client(
            "s3", endpoint_url="http://127.0.0.1:8333", region_name="us-east-1"
        )
        resp = client.list_buckets()
        assert "gov-doc" in [b["Name"] for b in resp["Buckets"]]

    def test_roundtrip(self, s3_storage):
        """保存→读取→存在→大小→删除 全往返（与 LocalFS 语义一致）。"""
        key = "raw/abc123.pdf"
        assert s3_storage.save(key, b"%PDF-1.4 fake") == key
        assert s3_storage.get(key) == b"%PDF-1.4 fake"
        assert s3_storage.exists(key) is True
        assert s3_storage.size(key) == len(b"%PDF-1.4 fake")
        s3_storage.delete(key)
        assert s3_storage.exists(key) is False

    def test_missing_key_semantics(self, s3_storage):
        """404 语义：get/size 抛 FileNotFoundError，exists 返回 False。"""
        with pytest.raises(FileNotFoundError):
            s3_storage.get("raw/nope.pdf")
        assert s3_storage.exists("raw/nope.pdf") is False

    def test_path_style_addressing(self, s3_storage):
        """内网 IP 端点必须 path-style（SeaweedFS/Ceph 联调关键）。"""
        cfg = s3_storage.client.meta.config
        assert cfg.s3["addressing_style"] == "path"
        assert cfg.signature_version == "s3v4"

    def test_ping(self, s3_storage):
        """ping 探测 bucket 可达。"""
        assert s3_storage.ping() is True

    def test_factory_s3(self, monkeypatch):
        """工厂按 settings 返回 S3 后端；storage_info 摘要正确。"""
        from moto import mock_aws

        fake = type("S", (), {
            "storage_provider": "s3",
            "storage_s3_endpoint": "127.0.0.1:8333",
            "storage_s3_bucket": "gov-doc",
            "storage_s3_access_key": "admin",
            "storage_s3_secret_key": "admin123",
            "storage_s3_region": "us-east-1",
            "storage_s3_secure": False,
            "storage_s3_addressing_style": "path",
        })()
        monkeypatch.setattr(storage_mod, "_settings", fake)
        with mock_aws():
            reset_storage()
            s = storage_mod.get_storage()
            assert isinstance(s, S3Storage)
            info = storage_mod.storage_info()
            assert info["provider"] == "s3"
            assert info["ok"] is True
            assert info["detail"]["bucket"] == "gov-doc"
            assert info["detail"]["addressing_style"] == "path"
            reset_storage()
