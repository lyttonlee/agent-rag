"""按 .env 的 S3 配置验证 SeaweedFS 是否可用（只读/写探针，不污染业务数据）。

用法：uv run python scripts/verify_seaweedfs.py
- 复用 app.core.config 读取 STORAGE_S3_* 系列配置；
- 实测：head_bucket（桶可达/自动建桶）→ put_object → get_object 回读比对 → delete_object；
- 区分「连通性/认证/网络」类错误与「不存在」类错误，给出明确结论。
"""

from __future__ import annotations

import sys
import traceback

from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.core.config import get_settings


def main() -> int:
    s = get_settings()
    if s.storage_provider not in ("s3", "SeaweedFS"):
        print(f"[跳过] 当前 storage_provider={s.storage_provider!r}，非 s3/SeaweedFS，无需验证")
        return 0

    endpoint_url = ("https://" if s.storage_s3_secure else "http://") + s.storage_s3_endpoint
    print("=== SeaweedFS / S3 可用性验证 ===")
    print(f"endpoint    : {endpoint_url}")
    print(f"bucket      : {s.storage_s3_bucket}")
    print(f"access_key  : {s.storage_s3_access_key}")
    print(f"region      : {s.storage_s3_region}")
    print(f"secure      : {s.storage_s3_secure}")
    print(f"addr_style  : {s.storage_s3_addressing_style}")
    print("--------------------------------")

    try:
        import boto3
    except ImportError as e:
        print(f"[失败] 未安装 boto3：{e}")
        return 2

    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=s.storage_s3_access_key,
        aws_secret_access_key=s.storage_s3_secret_key,
        region_name=s.storage_s3_region,
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": s.storage_s3_addressing_style},
        ),
    )

    probe_key = "_verify_seaweedfs_probe.bin"
    payload = b"gov-doc-rag-seawedfs-probe-" + b"ok"
    code = 0
    try:
        # 1) 桶可达 / 自动建桶
        try:
            client.head_bucket(Bucket=s.storage_s3_bucket)
            print("[1/4] head_bucket          : 桶已存在且可达")
        except ClientError as e:
            c = e.response.get("Error", {}).get("Code", "")
            if c in ("404", "NoSuchBucket", "NotFound"):
                client.create_bucket(Bucket=s.storage_s3_bucket)
                print("[1/4] head_bucket -> create : 桶不存在，已自动创建")
            else:
                raise
        # 2) 写探针
        client.put_object(Bucket=s.storage_s3_bucket, Key=probe_key, Body=payload)
        print("[2/4] put_object           : 写入探针成功")
        # 3) 读回比对
        resp = client.get_object(Bucket=s.storage_s3_bucket, Key=probe_key)
        got = resp["Body"].read()
        if got == payload:
            print("[3/4] get_object 回读比对  : 一致 ✓")
        else:
            print("[3/4] get_object 回读比对  : ❌ 内容不一致")
            code = 1
        # 4) 删除探针
        client.delete_object(Bucket=s.storage_s3_bucket, Key=probe_key)
        print("[4/4] delete_object        : 删除探针成功")
    except ClientError as e:
        code = 1
        err = e.response.get("Error", {})
        print(f"[失败] S3 ClientError: Code={err.get('Code')} Message={err.get('Message')}")
        print(f"       可能原因：endpoint 不可达 / 凭证错误 / 桶无权限 / 寻址风格不匹配")
    except Exception as e:  # 网络不通、DNS 失败等
        code = 1
        print(f"[失败] 非 S3 错误（多为网络/DNS）：{e}")
        traceback.print_exc()

    if code == 0:
        print("\n✅ SeaweedFS 可用：连通 + 认证 + 读写均通过。可将 storage_provider 设为 s3 或 SeaweedFS。")
    else:
        print("\n❌ SeaweedFS 不可用：请检查 endpoint / 凭证 / 寻址风格 / 网络。")
    return code


if __name__ == "__main__":
    sys.exit(main())
