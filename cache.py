"""轻量级 TTL 内存缓存

- 线程安全（threading.Lock）
- 过期自动失效
- 支持手动 invalidate
- 不会无限增长（每个 key 都带过期时间）

用法：
    from cache import ttl_cache

    @ttl_cache(ttl=5.0, key="status")
    async def get_status():
        ...

    # 在写操作时手动清空
    ttl_cache.invalidate("status")
"""
import time
import threading
import hashlib
import json
from typing import Any, Callable, Optional


class TtlCache:
    """简单 TTL 缓存：dict[key] = (value, expire_at)
    
    - get(key) -> None if expired
    - set(key, value, ttl)
    - invalidate(key) 清单个
    - invalidate_prefix(prefix) 清所有 prefix 匹配
    - invalidate_all() 全清
    """
    def __init__(self):
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expire_at = entry
            if expire_at > 0 and time.time() >= expire_at:
                self._store.pop(key, None)
                self._misses += 1
                return None
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._store[key] = (value, time.time() + ttl if ttl > 0 else 0)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        with self._lock:
            keys_to_del = [k for k in self._store if k.startswith(prefix)]
            for k in keys_to_del:
                self._store.pop(k, None)

    def invalidate_all(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": f"{(self._hits / max(self._hits + self._misses, 1) * 100):.1f}%",
            }


# 全局单例
ttl_cache = TtlCache()


def cache_key_for(namespace: str, **parts) -> str:
    """根据 namespace + 参数生成稳定 key
    
    例：cache_key_for("status", user_id=2) -> "status:u2"
        cache_key_for("chapters_stats", user_id=2, novel_id="abc") -> "chapters_stats:u2:abc"
    """
    if not parts:
        return namespace
    sorted_parts = sorted(parts.items())
    raw = json.dumps(sorted_parts, sort_keys=True, default=str, ensure_ascii=False)
    h = hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
    detail = ":".join(f"{k}={v}" for k, v in sorted_parts)
    return f"{namespace}:{detail}:{h}" if detail else f"{namespace}:{h}"


def cached(namespace: str, ttl: float):
    """装饰器：缓存 endpoint 响应
    
    用法：
        @cached("status", ttl=5.0)
        async def get_status():
            ...
    
    注意：被装饰函数的第一个参数如果是 Request，会被自动剔除（避免不可序列化）
    """
    def decorator(fn: Callable) -> Callable:
        async def wrapper(*args, **kwargs):
            # 过滤 Request 类参数（FastAPI 自动注入）
            from fastapi import Request
            cache_parts = {}
            sig_args = []
            for a in args:
                if isinstance(a, Request):
                    continue
                # 简单转 str 作为 key
                cache_parts[f"a{len(sig_args)}"] = str(a)[:50]
                sig_args.append(a)
            for k, v in kwargs.items():
                if isinstance(v, Request):
                    continue
                cache_parts[k] = str(v)[:50]
            
            key = cache_key_for(namespace, **cache_parts)
            hit = ttl_cache.get(key)
            if hit is not None:
                return hit
            # 没缓存，执行
            result = await fn(*args, **kwargs)
            # 存（只缓存 dict / list 等可序列化结果）
            try:
                json.dumps(result, default=str)
                ttl_cache.set(key, result, ttl)
            except (TypeError, ValueError):
                pass
            return result
        wrapper.__wrapped__ = fn
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


def invalidate_for_novel(novel_id: str) -> None:
    """章节/作品写操作时调用：清空所有跟该 novel 相关的缓存"""
    ttl_cache.invalidate_prefix("chapters_stats")
    ttl_cache.invalidate("status")
    ttl_cache.invalidate("novels")
    ttl_cache.invalidate("usage")
