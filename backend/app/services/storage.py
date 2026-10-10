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


class S3Storage(StorageProvider):
    """S3 协议兼容实现（可选）。

    可指向 SeaweedFS / Ceph RGW / 国产私有化对象存储等任意 S3 端点。
    `boto3` 为懒加载依赖（仅 s3 模式需要），避免对默认 local 模式造成负担。
    """

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        secure: bool = True,
    ) -> None:
        try:
            import boto3  # 懒加载
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "使用 s3 存储后端需安装 boto3：uv add boto3"
            ) from e
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=("https://" if secure else "http://") + endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    def save(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key

    def get(self, key: str) -> bytes:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except self.client.exceptions.NoSuchKey:  # type: ignore[attr-defined]
            raise FileNotFoundError(f"存储键不存在：{key!r}")

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def size(self, key: str) -> int:
        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=key)
            return int(resp["ContentLength"])
        except Exception:
            raise FileNotFoundError(f"存储键不存在：{key!r}")


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
        )
    else:
        _PROVIDER = LocalFSStorage(root=_settings.storage_root)
    return _PROVIDER


def reset_storage() -> None:
    """测试用：重置单例，便于切换后端或 root。"""
    global _PROVIDER
    _PROVIDER = None
