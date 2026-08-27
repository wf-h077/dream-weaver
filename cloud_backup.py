"""云端备份抽象层

设计目标：
1. 抽象 CloudBackend 接口，支持多种后端
2. 默认实现 LocalMirrorBackend（复制到本地镜像目录）
3. 条件实现 S3Backend / OSSBackend（依赖缺失则降级）
4. 通过 .env 的 CLOUD_BACKEND 选择后端

环境变量：
- CLOUD_BACKEND            "local" / "s3" / "oss" / "webdav"（默认 local）
- CLOUD_LOCAL_DIR          local backend 的目标目录
- CLOUD_S3_BUCKET          S3 桶名
- CLOUD_S3_REGION          S3 区域
- CLOUD_S3_ACCESS_KEY      S3 access key
- CLOUD_S3_SECRET_KEY      S3 secret key
- CLOUD_S3_ENDPOINT        S3 兼容端点（可选，如 MinIO）
- CLOUD_OSS_ENDPOINT       阿里云 OSS endpoint
- CLOUD_OSS_BUCKET         阿里云 OSS 桶
- CLOUD_OSS_ACCESS_KEY     阿里云 access key
- CLOUD_OSS_SECRET_KEY     阿里云 secret key
- CLOUD_WEBDAV_URL         WebDAV URL
- CLOUD_WEBDAV_USER        WebDAV 用户
- CLOUD_WEBDAV_PASSWORD    WebDAV 密码
- CLOUD_PREFIX             云端路径前缀
"""
from __future__ import annotations

import os
import shutil
import time
from abc import ABC, abstractmethod
from typing import Optional


# ═══════════════════════════════════════════════════════
# 抽象接口
# ═══════════════════════════════════════════════════════

class CloudBackend(ABC):
    """云端备份抽象接口"""

    name: str = "abstract"

    @abstractmethod
    def upload(self, local_path: str, remote_name: Optional[str] = None) -> str:
        """上传文件到云端。

        Args:
            local_path: 本地文件路径
            remote_name: 云端文件名（不传则用本地文件名）

        Returns:
            云端路径/标识
        """
        raise NotImplementedError

    @abstractmethod
    def upload_dir(self, local_dir: str, remote_name: Optional[str] = None) -> str:
        """上传整个目录到云端（递归）。

        Returns:
            云端路径/标识
        """
        raise NotImplementedError

    def is_available(self) -> bool:
        """检查后端是否可用（凭证是否齐全、依赖是否安装）。"""
        return True


# ═══════════════════════════════════════════════════════
# 本地镜像 backend（默认）
# ═══════════════════════════════════════════════════════

class LocalMirrorBackend(CloudBackend):
    """复制到本地其他目录，可作为"远程镜像"。

    适合：
    - 备份到独立硬盘
    - 备份到 NAS 挂载点
    - 测试环境
    """

    name = "local"

    def __init__(self, target_dir: str, prefix: str = "novel_backups"):
        self.target_dir = target_dir
        self.prefix = prefix
        os.makedirs(self.target_dir, exist_ok=True)

    def upload(self, local_path: str, remote_name: Optional[str] = None) -> str:
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"本地文件不存在: {local_path}")
        if remote_name is None:
            remote_name = os.path.basename(local_path)
        ts = time.strftime("%Y%m%d_%H%M%S")
        target = os.path.join(self.target_dir, self.prefix, f"{ts}_{remote_name}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(local_path, target)
        return target

    def upload_dir(self, local_dir: str, remote_name: Optional[str] = None) -> str:
        if not os.path.exists(local_dir):
            raise FileNotFoundError(f"本地目录不存在: {local_dir}")
        ts = time.strftime("%Y%m%d_%H%M%S")
        base_name = remote_name or os.path.basename(os.path.normpath(local_dir))
        target = os.path.join(self.target_dir, self.prefix, f"{ts}_{base_name}")
        if os.path.exists(target):
            shutil.rmtree(target)
        shutil.copytree(local_dir, target)
        return target


# ═══════════════════════════════════════════════════════
# S3 backend（依赖 boto3）
# ═══════════════════════════════════════════════════════

class S3Backend(CloudBackend):
    """AWS S3 / S3 兼容后端（依赖 boto3）。"""

    name = "s3"

    def __init__(self, bucket: str, region: str, access_key: str, secret_key: str,
                 endpoint: str = "", prefix: str = "novel_backups"):
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.endpoint = endpoint or None
        self.prefix = prefix
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore
        except ImportError:
            raise ImportError(
                "S3 backend 需要安装 boto3：pip install boto3"
            )
        self._client = boto3.client(
            "s3",
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            endpoint_url=self.endpoint,
        )
        return self._client

    def is_available(self) -> bool:
        try:
            import boto3  # type: ignore
            return bool(self.bucket and self.access_key and self.secret_key)
        except ImportError:
            return False

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def upload(self, local_path: str, remote_name: Optional[str] = None) -> str:
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"本地文件不存在: {local_path}")
        if remote_name is None:
            remote_name = os.path.basename(local_path)
        client = self._get_client()
        key = self._key(remote_name)
        client.upload_file(local_path, self.bucket, key)
        return f"s3://{self.bucket}/{key}"

    def upload_dir(self, local_dir: str, remote_name: Optional[str] = None) -> str:
        if not os.path.exists(local_dir):
            raise FileNotFoundError(f"本地目录不存在: {local_dir}")
        if remote_name is None:
            remote_name = os.path.basename(os.path.normpath(local_dir))
        client = self._get_client()
        prefix = self._key(remote_name)
        for root, _, files in os.walk(local_dir):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, local_dir).replace(os.sep, "/")
                key = f"{prefix}/{rel}"
                client.upload_file(full, self.bucket, key)
        return f"s3://{self.bucket}/{prefix}"


# ═══════════════════════════════════════════════════════
# 阿里云 OSS backend（依赖 oss2）
# ═══════════════════════════════════════════════════════

class OSSBackend(CloudBackend):
    """阿里云 OSS 后端（依赖 oss2）。"""

    name = "oss"

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str,
                 prefix: str = "novel_backups"):
        self.endpoint = endpoint
        self.bucket_name = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.prefix = prefix
        self._bucket = None

    def _get_bucket(self):
        if self._bucket is not None:
            return self._bucket
        try:
            import oss2  # type: ignore
        except ImportError:
            raise ImportError("OSS backend 需要安装 oss2：pip install oss2")
        auth = oss2.Auth(self.access_key, self.secret_key)
        self._bucket = oss2.Bucket(auth, self.endpoint, self.bucket_name)
        return self._bucket

    def is_available(self) -> bool:
        try:
            import oss2  # type: ignore
            return bool(self.bucket_name and self.access_key and self.secret_key and self.endpoint)
        except ImportError:
            return False

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def upload(self, local_path: str, remote_name: Optional[str] = None) -> str:
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"本地文件不存在: {local_path}")
        if remote_name is None:
            remote_name = os.path.basename(local_path)
        bucket = self._get_bucket()
        key = self._key(remote_name)
        bucket.put_object_from_file(key, local_path)
        return f"oss://{self.bucket_name}/{key}"

    def upload_dir(self, local_dir: str, remote_name: Optional[str] = None) -> str:
        if not os.path.exists(local_dir):
            raise FileNotFoundError(f"本地目录不存在: {local_dir}")
        if remote_name is None:
            remote_name = os.path.basename(os.path.normpath(local_dir))
        bucket = self._get_bucket()
        prefix = self._key(remote_name)
        for root, _, files in os.walk(local_dir):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, local_dir).replace(os.sep, "/")
                key = f"{prefix}/{rel}"
                bucket.put_object_from_file(key, full)
        return f"oss://{self.bucket_name}/{prefix}"


# ═══════════════════════════════════════════════════════
# 工厂 + 默认 backend 管理
# ═══════════════════════════════════════════════════════

_BACKEND_CACHE: dict[str, CloudBackend] = {}


def get_cloud_backend(force_new: bool = False) -> Optional[CloudBackend]:
    """从环境变量读取配置，构造并缓存 backend。

    Returns:
        配置缺失或不可用时返回 None。
    """
    backend_type = os.getenv("CLOUD_BACKEND", "local").strip().lower()
    cache_key = f"{backend_type}"
    if not force_new and cache_key in _BACKEND_CACHE:
        return _BACKEND_CACHE[cache_key]

    prefix = os.getenv("CLOUD_PREFIX", "novel_backups")

    if backend_type in ("local", ""):
        target = os.getenv(
            "CLOUD_LOCAL_DIR",
            os.path.join(os.path.dirname(__file__), ".runlogs", "cloud_mirror"),
        )
        backend = LocalMirrorBackend(target_dir=target, prefix=prefix)
    elif backend_type == "s3":
        backend = S3Backend(
            bucket=os.getenv("CLOUD_S3_BUCKET", ""),
            region=os.getenv("CLOUD_S3_REGION", "us-east-1"),
            access_key=os.getenv("CLOUD_S3_ACCESS_KEY", ""),
            secret_key=os.getenv("CLOUD_S3_SECRET_KEY", ""),
            endpoint=os.getenv("CLOUD_S3_ENDPOINT", ""),
            prefix=prefix,
        )
    elif backend_type == "oss":
        backend = OSSBackend(
            endpoint=os.getenv("CLOUD_OSS_ENDPOINT", ""),
            bucket=os.getenv("CLOUD_OSS_BUCKET", ""),
            access_key=os.getenv("CLOUD_OSS_ACCESS_KEY", ""),
            secret_key=os.getenv("CLOUD_OSS_SECRET_KEY", ""),
            prefix=prefix,
        )
    else:
        return None

    if not backend.is_available():
        return None

    _BACKEND_CACHE[cache_key] = backend
    return backend


def upload_to_cloud(local_path: str, remote_name: Optional[str] = None,
                    label: str = "") -> Optional[str]:
    """便捷函数：上传文件到云端，失败返回 None。"""
    backend = get_cloud_backend()
    if backend is None:
        return None
    try:
        return backend.upload(local_path, remote_name)
    except Exception as e:
        print(f"  [警告] 云端上传失败 ({backend.name}): {e}")
        return None


def upload_dir_to_cloud(local_dir: str, remote_name: Optional[str] = None,
                        label: str = "") -> Optional[str]:
    """便捷函数：上传目录到云端，失败返回 None。"""
    backend = get_cloud_backend()
    if backend is None:
        return None
    try:
        return backend.upload_dir(local_dir, remote_name)
    except Exception as e:
        print(f"  [警告] 云端目录上传失败 ({backend.name}): {e}")
        return None


def list_available_backends() -> list[dict]:
    """列出所有 backend 及其可用性。"""
    results = []
    for backend_type in ("local", "s3", "oss"):
        # 临时构造一个不缓存的 backend 来检查
        prefix = os.getenv("CLOUD_PREFIX", "novel_backups")
        if backend_type == "local":
            target = os.getenv("CLOUD_LOCAL_DIR", "")
            if not target:
                target = os.path.join(os.path.dirname(__file__), ".runlogs", "cloud_mirror")
            b = LocalMirrorBackend(target, prefix=prefix)
        elif backend_type == "s3":
            b = S3Backend(
                bucket=os.getenv("CLOUD_S3_BUCKET", ""),
                region=os.getenv("CLOUD_S3_REGION", "us-east-1"),
                access_key=os.getenv("CLOUD_S3_ACCESS_KEY", ""),
                secret_key=os.getenv("CLOUD_S3_SECRET_KEY", ""),
                endpoint=os.getenv("CLOUD_S3_ENDPOINT", ""),
                prefix=prefix,
            )
        else:
            b = OSSBackend(
                endpoint=os.getenv("CLOUD_OSS_ENDPOINT", ""),
                bucket=os.getenv("CLOUD_OSS_BUCKET", ""),
                access_key=os.getenv("CLOUD_OSS_ACCESS_KEY", ""),
                secret_key=os.getenv("CLOUD_OSS_SECRET_KEY", ""),
                prefix=prefix,
            )
        results.append({
            "name": b.name,
            "available": b.is_available(),
            "is_default": os.getenv("CLOUD_BACKEND", "local").lower() == backend_type,
        })
    return results
