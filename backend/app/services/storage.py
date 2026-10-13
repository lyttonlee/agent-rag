"""统一对象存储抽象层（去 MinIO 化）。

设计动机
--------
原方案使用 MinIO 作为对象存储；因开源协议 / 镜像获取受限，改为**存储抽象层**：
- 业务代码只依赖 `StorageProvider` 接口，不感知底层实现；
- 通过 `settings.storage_provider` 在部署时切换后端，零业务改动。

两种后端
--------
1. `local`  —— 本地文件系统（**默认**）：零镜像、零协议风险、信创最友好；
              适合本项目规模（20 万文档 / ~44GB 原文）。
2. `s3`     —— S3 协议兼容：可指向 SeaweedFS（Apache 2.0）/ Ceph RGW /
              国产私有化对象存储（均兼容 S3 API）；业务代码无需改动。

`content_ref` 字段语义随之统一为"存储对象键"：
- local 模式：相对 `storage_root` 的路径，如 `raw/ab12cd34.pdf`；
- s3 模式：对象键，如 `raw/ab12cd34.pdf`。

S3 模式联调要点（SeaweedFS，2026-10-13 加固）：
- **path-style 寻址**：内网以 IP + 端口暴露端点（如 `127.0.0.1:8333`）时，
  virtual-hosted 风格（`bucket.ip:port`）无法解析，必须显式使用 path-style
  （`endpoint/bucket/key`）；可用 `STORAGE_S3_ADDRESSING_STYLE` 覆盖。
- **bucket 自动创建**：初始化时 head_bucket，不存在则 create_bucket，
  联调时无需手工建桶。
- **错误分类**：404 类（键不存在）才转 FileNotFoundError/False；
  认证失败、网络不通等原样抛出，避免"联调报错被吞成不存在"。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from app.core.config import get_settings

_settings = get_settings()


class StorageProvider(ABC):
    """存储抽象基类。所有后端必须实现以下接口。"""

    @abstractmethod
    def save(self, key: str, data: bytes) -> str:
        """保存二进制数据，返回对象键（content_ref）。"""
        raise NotImplementedError

    @abstractmethod
    def get(self, key: str) -> bytes:
        """按对象键读取二进制数据；不存在抛 FileNotFoundError。"""
        raise NotImplementedError

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除对象键对应的数据。"""
        raise NotImplementedError

    @abstractmethod
    def exists(self, key: str) -> bool:
        """对象键是否存在。"""
        raise NotImplementedError

    @abstractmethod
    def size(self, key: str) -> int:
        """对象字节大小。"""
        raise NotImplementedError

    def ping(self) -> bool:
        """连通性探测（默认 True；各后端可覆写为真实探测）。"""
        return True


class LocalFSStorage(StorageProvider):
    """本地文件系统实现（默认）。

    安全约束：
    - `key` 必须为相对路径，禁止任何形式的目录穿越（..、绝对路径、scheme）；
    - 实际落盘路径被严格限制在 `storage_root` 之内。
    """

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, key: str) -> Path:
        # 规范化并阻止越权：解析后的绝对路径必须仍在 root 之内
        if not key or key.startswith(("/", "\\", ".")) or ".." in key.replace("\\", "/"):
            raise ValueError(f"非法的存储键（禁止绝对路径/上级目录）：{key!r}")
        target = (self.root / key).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError(f"存储键越权，超出根目录：{key!r}")
        return target

    def save(self, key: str, data: bytes) -> str:
        path = self._safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写临时文件再 rename，避免半截文件
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return key

    def get(self, key: str) -> bytes:
        path = self._safe_path(key)
        if not path.exists():
            raise FileNotFoundError(f"存储键不存在：{key!r}")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        try:
            self._safe_path(key).unlink()
        except FileNotFoundError:
            pass

    def exists(self, key: str) -> bool:
        try:
            return self._safe_path(key).exists()
        except ValueError:
            return False

    def size(self, key: str) -> int:
        path = self._safe_path(key)
        if not path.exists():
            raise FileNotFoundError(f"存储键不存在：{key!r}")
        return path.stat().st_size

    def ping(self) -> bool:
        """轻量连通性探测（根目录可写），供健康检查使用。"""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".ping"
            probe.write_bytes(b"ok")
            probe.unlink()
            return True
        except Exception:
            return False


class S3Storage(StorageProvider):
    """S3 协议兼容实现（可选）。

    可指向 SeaweedFS / Ceph RGW / 国产私有化对象存储等任意 S3 端点。
    `boto3` 为懒加载依赖（仅 s3 模式需要），避免对默认 local 模式造成负担。

    联调关键点：
    - path-style 寻址（内网 IP 端点必须，见模块 docstring）；
    - 初始化即 ensure_bucket（不存在则创建），联调免手工建桶；
    - 404 类错误才视为"不存在"，认证/网络错误原样抛出不吞错。
    """

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        secure: bool = False,
        addressing_style: str = "path",
    ) -> None:
        try:
            import boto3  # 懒加载
            from botocore.config import Config as BotoConfig
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "使用 s3 存储后端需安装 boto3：uv add boto3"
            ) from e
        self.bucket = bucket
        self.endpoint_url = ("https://" if secure else "http://") + endpoint
        # path-style：SeaweedFS/Ceph 等以 IP+端口暴露时唯一可靠寻址方式；
        # s3v4：SeaweedFS 支持的签名版本（v2 已不推荐）。
        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": addressing_style},
            ),
        )
        self.ensure_bucket()

    # ---- 桶管理 / 连通性 ----

    def ensure_bucket(self) -> None:
        """确保 bucket 存在（不存在则创建）。

        SeaweedFS S3 支持 CreateBucket；已存在（或匿名可写）时静默跳过。
        联调场景下免去手工建桶步骤；创建失败（如只读凭证）不阻塞启动，
        首次 save 时会以明确错误暴露。
        """
        import logging

        from botocore.exceptions import ClientError

        logger = logging.getLogger("gov_doc_rag")
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            # 404/NoSuchBucket = 桶不存在，继续创建；其余（403 等）先尝试创建再失败
            if code not in ("404", "NoSuchBucket", "NotFound"):
                logger.warning("S3 head_bucket 异常（%s），仍尝试创建：%s", code, e)
        except Exception as e:  # 网络不通等：尝试创建，让错误在创建时暴露
            logger.warning("S3 head_bucket 网络异常，仍尝试创建：%s", e)
        try:
            self.client.create_bucket(Bucket=self.bucket)
            logger.info("S3 bucket 已自动创建：%s（%s）", self.bucket, self.endpoint_url)
        except Exception as e:  # pragma: no cover - 取决于部署方权限
            logger.warning(
                "S3 bucket 创建失败（可手工创建后重试）：%s / %s", self.bucket, e
            )

    def ping(self) -> bool:
        """轻量连通性探测（head_bucket），供健康检查/启动自检使用。"""
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            return False

    # ---- 基础读写 ----

    def save(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key

    def get(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NoSuchBucket", "NotFound"):
                raise FileNotFoundError(f"存储键不存在：{key!r}") from e
            raise

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            # 认证失败 / 网络异常等：原样抛出，避免联调问题被吞成"不存在"
            raise
        except Exception:
            # 非 ClientError（如连接层错误）同样不吞：存在性未知优于假阴性
            raise

    def size(self, key: str) -> int:
        from botocore.exceptions import ClientError

        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=key)
            return int(resp["ContentLength"])
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NoSuchBucket", "NotFound"):
                raise FileNotFoundError(f"存储键不存在：{key!r}") from e
            raise


_PROVIDER: StorageProvider | None = None


def get_storage() -> StorageProvider:
    """返回进程内单例存储后端（按 settings.storage_provider 切换）。"""
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    kind: Literal["local", "s3"] = _settings.storage_provider
    if kind == "s3":
        _PROVIDER = S3Storage(
            endpoint=_settings.storage_s3_endpoint,
            bucket=_settings.storage_s3_bucket,
            access_key=_settings.storage_s3_access_key,
            secret_key=_settings.storage_s3_secret_key,
            region=_settings.storage_s3_region,
            secure=_settings.storage_s3_secure,
            addressing_style=_settings.storage_s3_addressing_style,
        )
    else:
        _PROVIDER = LocalFSStorage(root=_settings.storage_root)
    return _PROVIDER


def storage_info() -> dict:
    """当前存储后端摘要（健康检查/启动日志用，不做网络探测）。

    返回：{"provider": "local|s3", "ok": bool, "detail": {...}}
    ok 为 ping() 结果（local 探根目录可写；s3 探 head_bucket）。
    """
    kind = _settings.storage_provider
    if kind == "s3":
        detail = {
            "endpoint": _settings.storage_s3_endpoint,
            "bucket": _settings.storage_s3_bucket,
            "secure": _settings.storage_s3_secure,
            "addressing_style": _settings.storage_s3_addressing_style,
        }
    else:
        detail = {"root": _settings.storage_root}
    try:
        ok = get_storage().ping()
    except Exception:
        ok = False
    return {"provider": kind, "ok": ok, "detail": detail}


def reset_storage() -> None:
    """测试用：重置单例，便于切换后端或 root。"""
    global _PROVIDER
    _PROVIDER = None
