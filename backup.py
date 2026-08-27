"""数据库与作品目录的自动备份

提供：
- backup_database()          备份 story_bible.db
- backup_novels_dir()        备份整个 novels/ 目录
- backup_all()               一次性备份 db + novels
- list_backups()             列出所有备份
- restore_database(path)     从备份恢复
- cleanup_old_backups(keep)  只保留最近 N 份备份
- should_auto_backup()       判断是否需要自动备份（每 N 章）
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Optional

from config import SQLITE_DB_PATH

# 备份根目录
_BASE_DIR = os.path.dirname(os.path.abspath(SQLITE_DB_PATH))
BACKUP_DIR = os.path.join(_BASE_DIR, ".runlogs", "backups")
# novels/ 目录
NOVELS_DIR = os.path.join(_BASE_DIR, "novels")

# 自动备份间隔（每 N 章触发一次）
AUTO_BACKUP_EVERY_N_CHAPTERS = 10
# 保留最近多少份备份（旧的自动清理）
MAX_BACKUPS_TO_KEEP = 30


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def backup_database(label: str = "") -> str:
    """备份 story_bible.db 到 .runlogs/backups/。

    如果配置了 CLOUD_BACKEND，会自动同步到云端（失败不阻塞本地备份）。

    Returns:
        备份文件的绝对路径。
    """
    if not os.path.exists(SQLITE_DB_PATH):
        raise FileNotFoundError(f"数据库不存在: {SQLITE_DB_PATH}")

    _ensure_dir(BACKUP_DIR)
    ts = _timestamp()
    suffix = f"_{label}" if label else ""
    dst = os.path.join(BACKUP_DIR, f"story_bible_{ts}{suffix}.db")
    shutil.copy2(SQLITE_DB_PATH, dst)

    # 异步触发云端上传（最佳努力，失败不抛错）
    _try_cloud_upload(dst, label=label)

    return dst


def backup_novels_dir(label: str = "") -> str:
    """备份 novels/ 目录到 .runlogs/backups/novels_YYYYMMDD_HHMMSS/。

    如果配置了 CLOUD_BACKEND，会自动同步到云端（失败不阻塞本地备份）。

    Returns:
        备份目录的绝对路径。
    """
    if not os.path.exists(NOVELS_DIR):
        raise FileNotFoundError(f"novels 目录不存在: {NOVELS_DIR}")

    _ensure_dir(BACKUP_DIR)
    ts = _timestamp()
    suffix = f"_{label}" if label else ""
    dst = os.path.join(BACKUP_DIR, f"novels_{ts}{suffix}")
    # 使用 copytree 的 dirs_exist_ok=False 保证不覆盖
    shutil.copytree(NOVELS_DIR, dst)

    _try_cloud_upload(dst, label=label)

    return dst


def backup_all(label: str = "") -> dict:
    """一次性备份 db + novels。

    Returns:
        {"db": "...", "novels": "...", "timestamp": "..."}
    """
    db_path = backup_database(label=label)
    novels_path = backup_novels_dir(label=label)
    return {
        "db": db_path,
        "novels": novels_path,
        "timestamp": _timestamp(),
        "label": label,
    }


def _try_cloud_upload(local_path: str, label: str = "") -> None:
    """尝试云端上传（最佳努力，失败只 print warning）。"""
    try:
        import cloud_backup
        remote_name = os.path.basename(local_path)
        if os.path.isdir(local_path):
            result = cloud_backup.upload_dir_to_cloud(local_path, remote_name=remote_name, label=label)
        else:
            result = cloud_backup.upload_to_cloud(local_path, remote_name=remote_name, label=label)
        if result:
            print(f"  [云端] 已同步到 {result}")
    except ImportError:
        pass
    except Exception as e:
        print(f"  [警告] 云端上传失败: {e}")


def list_backups() -> list[dict]:
    """列出所有备份（按时间倒序）。"""
    if not os.path.exists(BACKUP_DIR):
        return []
    items = []
    for name in os.listdir(BACKUP_DIR):
        full = os.path.join(BACKUP_DIR, name)
        if not os.path.isfile(full) and not os.path.isdir(full):
            continue
        st = os.stat(full)
        items.append({
            "name": name,
            "path": full,
            "type": "db" if name.endswith(".db") else "dir",
            "size_bytes": st.st_size,
            "mtime": st.st_mtime,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def restore_database(backup_path: str) -> str:
    """从备份恢复数据库。会先自动备份当前数据库到 backups/pre_restore_xxx.db。

    Returns:
        恢复后数据库的绝对路径（即原 SQLITE_DB_PATH）。
    """
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"备份文件不存在: {backup_path}")
    if not backup_path.endswith(".db"):
        raise ValueError("只能恢复 .db 文件")

    # 安全起见：恢复前先备份当前 db
    if os.path.exists(SQLITE_DB_PATH):
        pre_restore_path = backup_database(label="pre_restore")
    else:
        pre_restore_path = None

    shutil.copy2(backup_path, SQLITE_DB_PATH)
    return SQLITE_DB_PATH


def cleanup_old_backups(keep: int = MAX_BACKUPS_TO_KEEP) -> list[str]:
    """只保留最近 N 份备份，删除更早的。

    Returns:
        被删除的备份路径列表。
    """
    if keep <= 0 or not os.path.exists(BACKUP_DIR):
        return []
    backups = list_backups()
    if len(backups) <= keep:
        return []
    to_delete = backups[keep:]
    deleted = []
    for item in to_delete:
        try:
            if os.path.isdir(item["path"]):
                shutil.rmtree(item["path"])
            else:
                os.remove(item["path"])
            deleted.append(item["path"])
        except Exception:
            pass
    return deleted


def should_auto_backup(current_chapter: int) -> bool:
    """判断是否需要自动备份。规则：每 N 章一次。"""
    if current_chapter <= 0:
        return False
    return current_chapter % AUTO_BACKUP_EVERY_N_CHAPTERS == 0


def get_backup_stats() -> dict:
    """获取备份统计信息。"""
    backups = list_backups()
    total_size = sum(b["size_bytes"] for b in backups)
    db_count = sum(1 for b in backups if b["type"] == "db")
    dir_count = sum(1 for b in backups if b["type"] == "dir")
    return {
        "backup_dir": BACKUP_DIR,
        "total_count": len(backups),
        "db_count": db_count,
        "dir_count": dir_count,
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / 1024 / 1024, 2),
        "latest": backups[0] if backups else None,
        "auto_backup_every_n_chapters": AUTO_BACKUP_EVERY_N_CHAPTERS,
        "max_backups_to_keep": MAX_BACKUPS_TO_KEEP,
    }
