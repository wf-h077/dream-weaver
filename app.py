from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
import os
import json
import uuid
import time
import traceback
import threading
import difflib
import contextvars
import shutil
import re
import io
import zipfile
from html import escape as escape_xml
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Any, Optional
import uvicorn

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import OUTPUT_DIR, get_chapter_word_range
from graph import build_full_pipeline, build_chapter_graph
from memory import StoryBible, DEFAULT_NOVEL_ID, MCP_SERVER_URL
from task_progress import set_progress_callback, reset_progress_callback
from auth import AuthStore, SESSION_COOKIE_NAME, SESSION_TTL_SECONDS
from publish_uploader import FanqieDraftUploader, PublishUploaderError
from cache import ttl_cache, cached, invalidate_for_novel
import backup
import models as model_layer
from agents.text_quality import (
    clean_ai_format_artifacts,
    detect_repetition_issues,
    ensure_chapter_title,
    format_repetition_issues,
    looks_like_broken_opening,
    looks_like_fake_fix,
    normalize_chapter_text,
)

BASE_DIR = os.path.dirname(__file__)
NOVELS_DIR = os.path.join(BASE_DIR, "novels")
CURRENT_NOVEL_PATH = os.path.join(BASE_DIR, "current_novel.json")

app = FastAPI(title="网文创作智能体 API")


# 中间件：所有请求自动设置 CURRENT_NOVEL_ID，让 UsageTracker 把
# LLM 调用按书归类，跨重启不丢。
@app.middleware("http")
async def _set_current_novel_id_middleware(request: Request, call_next):
    try:
        state = load_checkpoint()
        novel_id = get_state_novel_id(state) if state else ""
        if novel_id:
            model_layer.CURRENT_NOVEL_ID.set(novel_id)
    except Exception:
        pass
    return await call_next(request)

TASKS: dict[str, dict[str, Any]] = {}
TASK_LOCK = threading.Lock()
TASK_EXECUTOR = ThreadPoolExecutor(max_workers=1)
TASK_TTL_SECONDS = 6 * 60 * 60
AUTH = AuthStore()
CURRENT_USER_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_user_id", default=None)
PUBLISH_UPLOADER = FanqieDraftUploader(os.path.join(BASE_DIR, ".runlogs", "publish_browser"))
BOOTSTRAP_INVITE = AUTH.ensure_bootstrap_invite()
if BOOTSTRAP_INVITE:
    print(f"🔐 初始邀请码已生成：{BOOTSTRAP_INVITE}（也已写入 .runlogs/bootstrap_invite.txt）")

# 允许跨域
ALLOWED_ORIGINS = [
    item.strip()
    for item in os.getenv("APP_ALLOWED_ORIGINS", "http://127.0.0.1:8050,http://localhost:8050").split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# gzip 压缩：app.js 从 290KB → ~50KB
app.add_middleware(GZipMiddleware, minimum_size=500)


def get_active_user_id() -> int | None:
    return CURRENT_USER_ID.get()


def user_public(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "is_admin": bool(user.get("is_admin")),
    }


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    user = AUTH.get_user_by_session(request.cookies.get(SESSION_COOKIE_NAME))
    request.state.user = user
    token = CURRENT_USER_ID.set(user["id"] if user else None)
    try:
        if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/auth/"):
            if not user:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)
    finally:
        CURRENT_USER_ID.reset(token)

# 挂载静态文件
STATIC_DIR = os.path.join(BASE_DIR, "static")
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 检查点文件路径和当前小说的工作目录
CURRENT_NOVEL_DIR = None

def get_user_workspace(user_id: int | None = None) -> str | None:
    user_id = user_id or get_active_user_id()
    if not user_id:
        return None
    path = os.path.join(NOVELS_DIR, f"user_{user_id}")
    os.makedirs(path, exist_ok=True)
    return path

def get_novel_dir(novel_title: str = None, novel_id: str = None) -> str:
    """根据小说标题获取独立的工作目录"""
    if novel_title:
        # 将书名转换为安全的文件夹名（移除特殊字符）
        safe_name = "".join(c for c in novel_title if c.isalnum() or c in "._-_中文" or ord(c) > 127)
        if novel_id and novel_id != DEFAULT_NOVEL_ID:
            safe_name = f"{safe_name}_{novel_id[:8]}"
        novel_root = get_user_workspace() or NOVELS_DIR
        novel_base = os.path.join(novel_root, safe_name)
        os.makedirs(novel_base, exist_ok=True)
        return novel_base
    return OUTPUT_DIR

def get_checkpoint_path(novel_title: str = None, novel_id: str = None) -> str:
    """获取checkpoint路径"""
    novel_dir = get_novel_dir(novel_title, novel_id)
    return os.path.join(novel_dir, "checkpoint.json")

def ensure_novel_id(state: dict | None) -> dict | None:
    if state is not None and not state.get("novel_id"):
        state["novel_id"] = DEFAULT_NOVEL_ID
    return state

def get_state_novel_id(state: dict | None) -> str:
    if not state:
        return DEFAULT_NOVEL_ID
    return state.get("novel_id") or DEFAULT_NOVEL_ID

def get_current_story_bible() -> StoryBible:
    """获取当前激活作品的 StoryBible。

    注意：会先确保 current_novel_id 与 checkpoint 一致，避免在没有
    /api/status 调用过的情况下，bible 实例错误地指向 DEFAULT_NOVEL_ID。
    """
    state = load_checkpoint()
    expected_id = get_state_novel_id(state)
    # 同步 current_novel_id（避免 stale 状态）
    if get_current_novel_id() != expected_id:
        set_current_novel_id(expected_id)
    return StoryBible(expected_id)

def get_story_bible_for_state(state: dict | None) -> StoryBible:
    return StoryBible(get_state_novel_id(state))

def make_text_diff(old_text: str, new_text: str) -> list[dict[str, str]]:
    diff = difflib.ndiff((old_text or "").splitlines(), (new_text or "").splitlines())
    rows = []
    for line in diff:
        tag = line[:2]
        text = line[2:]
        if tag == "  ":
            rows.append({"type": "same", "text": text})
        elif tag == "- ":
            rows.append({"type": "removed", "text": text})
        elif tag == "+ ":
            rows.append({"type": "added", "text": text})
    return rows

def read_json_file(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def write_json_file(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_current_novel_path() -> str:
    user_workspace = get_user_workspace()
    if user_workspace:
        return os.path.join(user_workspace, "current_novel.json")
    return CURRENT_NOVEL_PATH

def get_current_novel_id() -> str | None:
    data = read_json_file(get_current_novel_path())
    if not data:
        return None
    return data.get("novel_id")

def set_current_novel_id(novel_id: str):
    write_json_file(get_current_novel_path(), {"novel_id": novel_id, "updated_at": time.time()})

def clear_current_novel_id(novel_id: str | None = None):
    current_path = get_current_novel_path()
    if not os.path.exists(current_path):
        return
    current_id = get_current_novel_id()
    if novel_id is None or current_id == novel_id:
        os.remove(current_path)

def iter_checkpoint_paths() -> list[str]:
    paths = []
    user_workspace = get_user_workspace()
    if user_workspace:
        for subdir in os.listdir(user_workspace):
            checkpoint_path = os.path.join(user_workspace, subdir, "checkpoint.json")
            if os.path.exists(checkpoint_path):
                paths.append(checkpoint_path)
    # 兜底：搜 NOVELS_DIR 顶层（兼容 legacy 等无 user_X 层级的旧作品）
    if os.path.isdir(NOVELS_DIR):
        for entry in os.listdir(NOVELS_DIR):
            full = os.path.join(NOVELS_DIR, entry)
            if os.path.isfile(full) or os.path.basename(full).startswith("user_"):
                continue  # 跳过 user_X 目录（已搜过）和文件
            cp = os.path.join(full, "checkpoint.json")
            if os.path.exists(cp) and cp not in paths:
                paths.append(cp)
    return paths

    legacy_checkpoint = os.path.join(OUTPUT_DIR, "checkpoint.json")
    if os.path.exists(legacy_checkpoint):
        paths.append(legacy_checkpoint)

    if os.path.exists(NOVELS_DIR):
        for subdir in os.listdir(NOVELS_DIR):
            checkpoint_path = os.path.join(NOVELS_DIR, subdir, "checkpoint.json")
            if os.path.exists(checkpoint_path):
                paths.append(checkpoint_path)
    return paths

def load_checkpoint_path(path: str) -> dict | None:
    state = read_json_file(path)
    if not state:
        return None
    ensure_novel_id(state)
    state["_checkpoint_path"] = path
    state["_novel_dir"] = os.path.dirname(path)
    return state

def find_checkpoint_by_novel_id(novel_id: str) -> dict | None:
    for path in iter_checkpoint_paths():
        state = load_checkpoint_path(path)
        if state and get_state_novel_id(state) == novel_id:
            return state
    return None

def list_novel_summaries(include_archived: bool = False) -> list[dict]:
    current_id = get_current_novel_id()
    novels = []
    seen = set()
    for path in iter_checkpoint_paths():
        state = load_checkpoint_path(path)
        if not state:
            continue
        novel_id = get_state_novel_id(state)
        if novel_id in seen:
            continue
        seen.add(novel_id)
        archived = bool(state.get("archived", False))
        if archived and not include_archived:
            continue

        current_chapter = int(state.get("current_chapter", 1) or 1)
        total_chapters = int(state.get("num_chapters", 100) or 100)
        novels.append({
            "novel_id": novel_id,
            "title": state.get("novel_title", "未命名作品"),
            "genre": state.get("novel_genre", ""),
            "style": state.get("novel_style", ""),
            "current_chapter": current_chapter,
            "completed_count": max(0, current_chapter - 1),
            "num_chapters": total_chapters,
            "progress": round(min(100, current_chapter / max(total_chapters, 1) * 100)),
            "archived": archived,
            "is_current": novel_id == current_id,
            "updated_at": state.get("updated_at") or os.path.getmtime(path),
            "checkpoint_path": path,
        })
    return sorted(novels, key=lambda item: item["updated_at"], reverse=True)

def _task_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "name": task["name"],
        "status": task["status"],
        "message": task.get("message", ""),
        "phase": task.get("phase", ""),
        "logs": task.get("logs", []),
        "result": task.get("result"),
        "error": task.get("error"),
        "retryable": bool(task.get("work")) and task.get("status") == "error",
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }

def get_task(task_id: str, user_id: int | None = None) -> dict[str, Any] | None:
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if task and user_id is not None and task.get("user_id") != user_id:
            return None
        return _task_snapshot(task) if task else None


# ═══════════════════════════════════════════════════════════════
# Task 持久化（避免 app 重启导致任务丢失）
# ═══════════════════════════════════════════════════════════════
TASKS_PERSIST_PATH = os.path.join(BASE_DIR, "data", "tasks_persist.json")
os.makedirs(os.path.dirname(TASKS_PERSIST_PATH), exist_ok=True)


def _persist_tasks():
    """把内存 TASKS（除 work lambda 外）写入磁盘 JSON。"""
    try:
        snap = {}
        for tid, t in TASKS.items():
            snap[tid] = {
                "task_id": t["task_id"],
                "name": t["name"],
                "user_id": t.get("user_id"),
                "status": t.get("status"),
                "phase": t.get("phase"),
                "message": t.get("message", ""),
                "logs": t.get("logs", [])[-50:],
                "result": t.get("result"),
                "error": t.get("error"),
                "created_at": t.get("created_at"),
                "updated_at": t.get("updated_at"),
            }
        tmp = TASKS_PERSIST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, TASKS_PERSIST_PATH)
    except Exception as e:
        print(f"[tasks persist] 写盘失败: {e}")


def _restore_tasks():
    """启动时从磁盘恢复 task（注意：work lambda 无法序列化，所以 running/queued 标为 interrupted）。"""
    if not os.path.exists(TASKS_PERSIST_PATH):
        return
    try:
        with open(TASKS_PERSIST_PATH, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except Exception as e:
        print(f"[tasks persist] 读盘失败: {e}")
        return
    restored = 0
    for tid, t in snap.items():
        # work 字段已丢失，无法重新跑
        # 如果是 running/queued，标为 interrupted
        if t.get("status") in ("running", "queued"):
            t["status"] = "interrupted"
            t["phase"] = "interrupted"
            t["message"] = "服务重启，任务已中断。请重新提交。"
            t.setdefault("logs", []).append({
                "phase": "interrupted",
                "message": "⚠️ 服务重启导致任务中断，work 无法序列化，请重新提交",
                "ts": time.time(),
            })
            t["updated_at"] = time.time()
        TASKS[tid] = t
        restored += 1
    if restored:
        print(f"[tasks persist] 恢复了 {restored} 个任务（含 interrupted 状态）")
        # 写回盘（更新 status）
        _persist_tasks()


# 启动时恢复
_restore_tasks()

def update_task(task_id: str, **updates):
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if not task or task.get("user_id") != get_active_user_id():
            return
        log_message = updates.pop("log_message", None)
        task.update(updates)
        if log_message:
            task.setdefault("logs", []).append({
                "phase": task.get("phase", ""),
                "message": log_message,
                "time": time.time(),
            })
            task["logs"] = task["logs"][-50:]
        task["updated_at"] = time.time()
    # 同步写盘（避免 app 重启后任务丢失）
    _persist_tasks()

def start_background_task(name: str, work: Callable[[Callable[[str], None]], dict], user_id: int | None = None) -> str:
    task_id = uuid.uuid4().hex
    now = time.time()
    user_id = user_id or get_active_user_id()
    with TASK_LOCK:
        expired = [
            tid for tid, task in TASKS.items()
            if task.get("status") in {"success", "error"} and now - task.get("updated_at", now) > TASK_TTL_SECONDS
        ]
        for tid in expired:
            TASKS.pop(tid, None)

        TASKS[task_id] = {
            "task_id": task_id,
            "name": name,
            "work": work,
            "user_id": user_id,
            "status": "queued",
            "phase": "queued",
            "message": "任务已排队，等待执行...",
            "logs": [],
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }

    # 任务入队后立刻持久化（work 字段不入盘）
    _persist_tasks()

    def runner():
        user_token = CURRENT_USER_ID.set(user_id)
        def progress(message: str, phase: str = "running"):
            update_task(task_id, phase=phase, message=message, log_message=message)

        token = set_progress_callback(lambda phase, message: progress(message, phase))
        update_task(task_id, status="running", phase="running", message="任务开始执行...", log_message="任务开始执行...")
        try:
            result = work(progress)
            update_task(task_id, status="success", phase="done", message=result.get("message", "任务完成"), result=result, log_message=result.get("message", "任务完成"))
        except Exception as exc:
            traceback.print_exc()
            update_task(task_id, status="error", phase="error", message="任务执行失败", error=str(exc), log_message=f"任务执行失败：{exc}")
        finally:
            reset_progress_callback(token)
            CURRENT_USER_ID.reset(user_token)

    TASK_EXECUTOR.submit(runner)
    return task_id

# 请求模型
class RegisterReq(BaseModel):
    username: str
    password: str
    invite_code: str

class LoginReq(BaseModel):
    username: str
    password: str

class InitRequest(BaseModel):
    title: str
    genre: str
    style: str
    setting: str
    num_chapters: int
    chapter_target_words: int = 2500
    concept_seed: str = ""
    selected_concept: dict[str, Any] | None = None
    concept_refinement: dict[str, Any] | None = None
    full_outline: dict[str, Any] | None = None
    opening_outline: dict[str, Any] | None = None
    style_preset_key: Optional[str] = None  # 风格 preset key（可选）
    theme_intent: str = ""  # 作品核心主题意图（用户显式声明，每章 AI 必读）

class ChapterControlReq(BaseModel):
    free_instruction: str = ""
    chapter_goal: str = ""
    mood: str = ""
    pace: str = ""
    pov: str = ""
    target_words: int | None = None
    must_characters: str = ""
    must_settings: str = ""
    must_hooks: str = ""
    ending_hook: str = ""
    notes: str = ""

class GenerateChapterReq(BaseModel):
    controls: ChapterControlReq | None = None

class BatchGenerateReq(BaseModel):
    """批量续写请求

    Attributes:
        start_chapter: 从哪一章开始（不填则从当前进度续写）
        count: 要生成的章节数
        controls_per_chapter: 是否每章都套用同一组控制项（None = 不传控制项）
        stop_on_error: 遇到单章失败时是否停止（True = 失败即停，False = 跳过继续）
        auto_backup_every: 每 N 章自动备份（默认 10，0 = 不自动备份）
        max_retries_per_chapter: 单章失败时的最大重试次数（0 = 不重试）
        retry_delay_seconds: 重试间隔（秒）
        concurrency: 并发数（默认 1 = 串行；>1 当前会强制降到 1，
                       预留扩展位，需要 StoryBible 加锁 + state 隔离重构）
    """
    start_chapter: int | None = None
    count: int = 5
    controls_per_chapter: ChapterControlReq | None = None
    stop_on_error: bool = True
    auto_backup_every: int = 10
    max_retries_per_chapter: int = 1
    retry_delay_seconds: float = 5.0
    concurrency: int = 1


class BackupReq(BaseModel):
    """手动触发备份请求"""
    label: str = ""


class AIRewriteRequest(BaseModel):
    """章节内 AI 改写"""
    target_text: str
    instruction: str = ""
    context_before: str = ""
    context_after: str = ""


class AIContinueRequest(BaseModel):
    """章节内 AI 续写"""
    prefix_text: str = ""
    target_words: int = 500


class ChapterReviseReq(BaseModel):
    """章节整章反馈修订（不直接保存，返回前后对比）"""
    feedback: str = ""


class ChapterApplyRevisionReq(BaseModel):
    """采用 AI 修订后的新版本（覆盖保存）"""
    new_content: str
    include_novels: bool = True

class SaveChapterReq(BaseModel):
    chapter_num: int
    content: str

class PublishStatusReq(BaseModel):
    status: str
    note: str = ""

class PublishUploadStartReq(BaseModel):
    writer_url: str = ""

# --- 作家工作台请求模型 ---
class InspirationReq(BaseModel):
    content: str
    tags: str = ""

class PlotHookReq(BaseModel):
    content: str
    target_chapter: int

class WorldRuleReq(BaseModel):
    category: str
    rule_text: str

class EntityCardReq(BaseModel):
    card_type: str
    name: str
    fields: dict = {}
    note: str = ""

class StyleProfileAnalyzeReq(BaseModel):
    name: str
    sample_text: str
    is_default: bool = True

class ApproveExtractionToCardReq(BaseModel):
    card_type: str
    name: str
    fields: dict = {}
    note: str = ""

class BrainstormReq(BaseModel):
    seed: str = "（不限定，请随便发散）"
    style_preset_key: Optional[str] = None  # 风格 preset key（可选）

class ConceptRefineReq(BaseModel):
    seed: str = ""
    direction: dict
    style_preset_key: Optional[str] = None  # 风格 preset key（可选）
    num_chapters: int = 20  # 章节约束数量（用户填的预估连载长度，默认 20 上限 100）

class OpeningOutlineReq(BaseModel):
    seed: str = ""
    direction: dict = {}
    refinement: dict
    full_outline: dict[str, Any] | None = None

class FullOutlineReq(BaseModel):
    seed: str = ""
    direction: dict[str, Any] = {}
    refinement: dict[str, Any]
    num_chapters: int = 100
    opening_outline: dict[str, Any] | None = None


def normalize_llm_json_text(raw_text: str) -> str:
    response_text = (raw_text or "").strip()
    if response_text.startswith("```json"):
        response_text = response_text[7:].strip()
    elif response_text.startswith("```"):
        response_text = response_text[3:].strip()
    if response_text.endswith("```"):
        response_text = response_text[:-3].strip()
    start = response_text.find("{")
    end = response_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        response_text = response_text[start:end + 1]
    return response_text.strip()


def repair_common_json_text(text: str) -> str:
    repaired = (text or "").strip()
    # 不要把中文双引号替换成英文双引号；它们常出现在中文字段值内部，
    # 替换后会变成未转义的 JSON 引号，反而破坏原本可修复的结构。
    repaired = repaired.replace("‘", "'").replace("’", "'")
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(r"}\s*\n\s*{", "},\n{", repaired)
    repaired = re.sub(r"]\s*\n\s*\"", "],\n\"", repaired)
    repaired = re.sub(r"}\s*\n\s*\"", "},\n\"", repaired)
    repaired = re.sub(r"(?<=[\"}\]\d])\s*\n\s*(?=\"[A-Za-z0-9_\u4e00-\u9fff]+\"\s*:)", ",\n", repaired)
    repaired = quote_unquoted_json_string_values(repaired)
    repaired = repair_dangling_backslash_before_quote(repaired)
    return repaired


def quote_unquoted_json_string_values(text: str) -> str:
    """Repair common model mistake: "key": 中文内容, where the value is not quoted."""
    def repl(match: re.Match) -> str:
        prefix = match.group(1)
        value = (match.group(2) or "").strip()
        suffix = match.group(3)
        if not value:
            return match.group(0)
        if value[0] in "\"[{":
            return match.group(0)
        if re.match(r"^-?\d+(\.\d+)?$", value) or value in {"true", "false", "null"}:
            return match.group(0)
        if value.endswith('"') and not value.startswith('"'):
            value = value[:-1].rstrip()
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{prefix}"{escaped}"{suffix}'

    return re.sub(
        r'(:\s*)([^"\{\[\]\n][^\n,}\]]*?)(\s*[,}])',
        repl,
        text,
    )


def repair_dangling_backslash_before_quote(text: str) -> str:
    # 模型偶尔会输出 `"良心余额\"`，这会把字段结束引号吞掉。
    # 当反斜杠位于中文/字母数字之后且后面马上是 JSON 分隔符时，将其移除。
    text = re.sub(r'(?<=[\u4e00-\u9fffA-Za-z0-9])\\("\s*[,}\]])', r'\1', text)
    text = re.sub(r'(?<=[\u4e00-\u9fffA-Za-z0-9])\\"\s*(?=,)', r'"\n', text)
    return text


def save_invalid_json_debug(raw_text: str, repaired_text: str = "") -> str:
    os.makedirs(os.path.join(BASE_DIR, ".runlogs"), exist_ok=True)
    path = os.path.join(BASE_DIR, ".runlogs", f"invalid_llm_json_{int(time.time())}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("=== RAW ===\n")
        f.write(raw_text or "")
        if repaired_text:
            f.write("\n\n=== REPAIRED ===\n")
            f.write(repaired_text)
    return path


def repair_json_with_llm(raw_text: str, error_message: str) -> str:
    from models import call_llm

    prompt = (
        "请把下面内容修复为严格合法的 JSON 对象。\n"
        "要求：只输出 JSON，不要 Markdown，不要解释；保留原有字段和中文内容；"
        "补齐缺失逗号，修复字符串引号和尾随逗号。\n\n"
        f"解析错误：{error_message}\n\n"
        f"待修复内容：\n{raw_text}"
    )
    return call_llm(
        role="planner",
        system_prompt="你是严格 JSON 修复器，只输出可被 json.loads 解析的 JSON 对象。",
        prompt=prompt,
        temperature=0.1,
        max_tokens=8192,
    )


def parse_llm_json_object(raw_text: str, allow_llm_repair: bool = True) -> dict:
    response_text = normalize_llm_json_text(raw_text)

    parse_errors = []
    try:
        return json.loads(response_text)
    except json.JSONDecodeError as e:
        parse_errors.append(e)

    repaired_text = repair_common_json_text(response_text)
    if repaired_text != response_text:
        try:
            return json.loads(repaired_text)
        except json.JSONDecodeError as e:
            parse_errors.append(e)

    # P2 增强：last-resort 修复 - 截断的 JSON 尝试找到最后一个完整的数组/对象边界
    truncated_text = try_recover_truncated_json(response_text)
    if truncated_text and truncated_text != response_text:
        try:
            return json.loads(truncated_text)
        except json.JSONDecodeError as e:
            parse_errors.append(e)

    if allow_llm_repair:
        try:
            fixed = repair_json_with_llm(response_text, str(parse_errors[-1] if parse_errors else "unknown"))
            return parse_llm_json_object(fixed, allow_llm_repair=False)
        except Exception as e:
            parse_errors.append(e)

    debug_path = save_invalid_json_debug(raw_text, repaired_text)
    last_error = parse_errors[-1] if parse_errors else "未知错误"
    raise ValueError(f"模型返回的 JSON 格式不完整，自动修复失败。已保存原始输出：{debug_path}。错误：{last_error}")


def try_recover_truncated_json(text: str) -> str | None:
    """截断恢复：尝试在最后一个完整的 } 或 ] 边界截断，补全缺失的括号。

    适用：模型输出被 max_tokens 截断（如 100 章 chapter_constraints 输出超长）。
    原理：找到最后一个 "    }" 这种"完整对象结束"标记（即使末尾没有换行也算），截到那里，再补全 `]}`。
    返回：修复后的文本；无法修复返回 None。
    """
    if not text:
        return None

    # 1. 尝试找到最后一个 "完整对象结束" 标记。
    # 候选：'    },\n' / '    }\n' / '  },\n' / '  }\n' / '    }'（末尾无换行）/ '\n}' / '}'
    # 优先级：嵌套更深的优先（4 空格 > 2 空格 > 0 空格），避免截到外层
    candidates = [
        ('    },\n', True),   # 数组里对象结束 + 逗号
        ('    }\n', False),   # 数组里对象结束
        ('    }', False),     # 数组里对象结束（末尾无换行，最常见截断）
        ('  },\n', True),
        ('  }\n', False),
        ('\n}', False),
        ('}', False),
    ]
    for marker, _has_comma in candidates:
        idx = text.rfind(marker)
        if idx >= 0:
            # 找到匹配的 marker，从这里结束（包含 marker 全部）
            end_pos = idx + len(marker)
            # 检查 marker 后是否还有内容（如果有，可能不是真正的截断点）
            after = text[end_pos:].strip()
            if not after:
                # 末尾干净的，直接用
                truncated = text[:end_pos].rstrip()
                if truncated.endswith(','):
                    truncated = truncated[:-1]
                opens_bracket = truncated.count('[') - truncated.count(']')
                opens_brace = truncated.count('{') - truncated.count('}')
                if opens_bracket < 0 or opens_brace < 0:
                    return None
                truncated += ']' * opens_bracket
                truncated += '}' * opens_brace
                return truncated
            # 末尾还有内容（可能是说明文字），跳过这个 marker
            continue
    return None


CONCEPT_DIRECTION_KEYS = [
    "title",
    "genre",
    "style",
    "logline",
    "target_readers",
    "core_hook",
    "protagonist",
    "golden_finger",
    "main_conflict",
    "differentiation",
    "opening_promise",
    "setting",
]


def normalize_concept_directions(data: dict, seed: str) -> list[dict[str, str]]:
    raw_items = []
    directions = data.get("directions", []) if isinstance(data, dict) else []
    if isinstance(directions, list):
        raw_items.extend(item for item in directions if isinstance(item, dict))
        for item in directions:
            if not isinstance(item, dict):
                continue
            for key in ("direction_1", "direction_2", "direction_3"):
                nested = item.get(key)
                if isinstance(nested, dict):
                    raw_items.append(nested)
    if isinstance(data, dict):
        for key in ("direction_1", "direction_2", "direction_3"):
            nested = data.get(key)
            if isinstance(nested, dict):
                raw_items.append(nested)

    seen_titles = set()
    normalized = []
    for item in raw_items:
        title = str(item.get("title", "")).strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        cleaned = {}
        for key in CONCEPT_DIRECTION_KEYS:
            value = str(item.get(key, "")).strip()
            value = re.sub(r"[\r\n]+", " ", value)
            value = value.replace("**", "").replace("```", "")
            if value.count('"') % 2 == 1:
                value = value.replace('"', "")
            if value.count("“") > value.count("”"):
                value += "”"
            cleaned[key] = value
        cleaned["title"] = cleaned["title"] or f"{seed[:12]}方向"
        cleaned["genre"] = cleaned["genre"] or "题材待定"
        cleaned["style"] = cleaned["style"] or "节奏清晰，人物动机明确"
        cleaned["logline"] = cleaned["logline"] or f"围绕{seed}展开的长篇连载方向。"
        cleaned["target_readers"] = cleaned["target_readers"] or "喜欢强钩子和长线成长的网文读者"
        cleaned["core_hook"] = cleaned["core_hook"] or "主角从低谷反击，逐步建立优势。"
        cleaned["protagonist"] = cleaned["protagonist"] or "主角开局受压，但具备隐藏潜力和明确目标。"
        cleaned["golden_finger"] = cleaned["golden_finger"] or "核心能力随剧情逐步解锁，存在限制和代价。"
        cleaned["main_conflict"] = cleaned["main_conflict"] or "主角成长与外部压迫持续升级。"
        cleaned["differentiation"] = cleaned["differentiation"] or "在常见爽点上加入更明确的职业、世界观或能力限制。"
        cleaned["opening_promise"] = cleaned["opening_promise"] or "前三章完成羞辱、觉醒、第一次反击。"
        cleaned["setting"] = cleaned["setting"] or f"围绕“{seed}”建立主角处境、能力限制、长期敌人与升级路径。"
        normalized.append(cleaned)
        if len(normalized) >= 3:
            break
    return normalized


def lenient_extract_concept_directions(raw_text: str, seed: str) -> list[dict[str, str]]:
    """Extract concept directions from almost-JSON output line by line."""
    text = normalize_llm_json_text(raw_text)
    blocks = re.findall(r"\{\s*(?:\"title\"\s*:.*?)(?=\n\s*\})\n\s*\}", text, re.DOTALL)
    if not blocks:
        blocks = re.findall(r"\{[^{}]*\"title\"\s*:[^{}]*\}", text, re.DOTALL)

    items = []
    for block in blocks:
        item = {}
        for key in CONCEPT_DIRECTION_KEYS:
            match = re.search(rf'"{re.escape(key)}"\s*:\s*(.*?)(?:,\s*$|$)', block, re.MULTILINE)
            if not match:
                item[key] = ""
                continue
            value = match.group(1).strip()
            value = value.rstrip(",").strip()
            if value.startswith('"'):
                value = value[1:]
            if value.endswith('"'):
                value = value[:-1]
            value = value.replace('\\"', '"').replace("\\\\", "\\")
            value = value.strip()
            item[key] = re.sub(r"[\r\n]+", " ", value)
        if item.get("title"):
            items.append(item)

    if not items:
        return []
    return normalize_concept_directions({"directions": items}, seed)


def fallback_concept_directions(seed: str) -> list[dict[str, str]]:
    base = seed.strip() or "被退婚后我成了超绝大佬"
    return [
        {
            "title": base[:18],
            "genre": "都市逆袭 / 身份反转 / 爽文",
            "style": "快节奏爽文，冲突密集，情绪反馈强",
            "logline": "被轻视的主角以隐藏身份完成反击。",
            "target_readers": "喜欢退婚、打脸、身份反转和升级期待的读者",
            "core_hook": "羞辱开局，主角隐忍布局，关键时刻反转打脸。",
            "protagonist": "开局被否定但有隐藏实力，目标是夺回尊严和主动权。",
            "golden_finger": "隐藏身份或特殊能力逐步曝光，每次使用都有代价。",
            "main_conflict": "主角的低调生存与外界持续压迫之间的矛盾。",
            "differentiation": "把反击节奏和长期势力博弈结合，避免只靠单章打脸。",
            "opening_promise": "前三章完成羞辱、第一次反击和更大敌人的出现。",
            "setting": f"故事从“{base}”展开，主角被迫站到低谷，却掌握外人不知道的身份或能力。前期以反击和立威建立爽点，中期扩大势力与秘密，后期揭开真正压迫源头。",
        },
        {
            "title": f"{base[:10]}：幕后执棋",
            "genre": "悬疑权谋 / 都市暗线 / 群像",
            "style": "悬疑压迫，线索克制，氛围紧张",
            "logline": "主角在被放逐后反向操控全局。",
            "target_readers": "喜欢悬疑、布局、反转和幕后博弈的读者",
            "core_hook": "所有人以为主角出局，实际他已经开始布局。",
            "protagonist": "理性克制，擅长收集信息和利用规则反制敌人。",
            "golden_finger": "能识破谎言或看见关键线索，但必须通过行动验证。",
            "main_conflict": "表面失败与暗中掌控之间的认知差。",
            "differentiation": "爽点不只来自武力，而来自信息差、证据链和局势反转。",
            "opening_promise": "前三章给出被弃、发现线索、反手设局的连续钩子。",
            "setting": f"围绕“{base}”构建一场身份、利益和真相交织的长线博弈。主角从边缘位置切入，逐步掌握证据和人心，让压迫者在自以为胜利时落入局中。",
        },
        {
            "title": f"{base[:10]}后我开局封神",
            "genre": "幻想升级 / 强者归来 / 热血",
            "style": "热血升级，目标明确，打脸与成长并重",
            "logline": "主角被否定后觉醒真正力量。",
            "target_readers": "喜欢升级、战斗、强者归来和高燃反击的读者",
            "core_hook": "主角每次被压制都会解锁更高层能力。",
            "protagonist": "外冷内韧，重视承诺，面对压迫选择正面破局。",
            "golden_finger": "核心能力分阶段觉醒，需要完成心境或资源条件。",
            "main_conflict": "旧秩序压制新力量，主角必须建立自己的规则。",
            "differentiation": "升级不是单纯变强，而是伴随责任、势力和规则改变。",
            "opening_promise": "前三章完成觉醒、救场、公开立敌和新目标。",
            "setting": f"以“{base}”为开端，主角在众人轻视中觉醒力量。能力越强，牵出的敌人越高层，故事围绕个人逆袭、势力建立和世界规则重写展开。",
        },
    ]


def regenerate_concept_directions(seed: str, bad_output: str, error_message: str) -> dict:
    from models import call_llm

    prompt = f"""根据一句话灵感重新生成 3 个小说方向，并修正上次输出的结构错误。

一句话灵感：{seed}

上次错误：{error_message}

硬性要求：
1. 只输出严格 JSON，不要 Markdown，不要解释。
2. JSON 顶层只能有 directions。
3. directions 必须是数组，正好 3 个对象。
4. 不允许出现 direction_1、direction_2、direction_3 字段。
5. 所有字段值必须是单行短字符串，不要换行，不要使用 **，不要使用中文或英文引号包裹专有名词。
6. 每个对象必须包含这些字段：{", ".join(CONCEPT_DIRECTION_KEYS)}。

输出格式：
{{
  "directions": [
    {{
      "title": "...",
      "genre": "...",
      "style": "...",
      "logline": "...",
      "target_readers": "...",
      "core_hook": "...",
      "protagonist": "...",
      "golden_finger": "...",
      "main_conflict": "...",
      "differentiation": "...",
      "opening_promise": "...",
      "setting": "..."
    }}
  ]
}}"""
    raw = call_llm(
        role="planner",
        system_prompt="你是严格 JSON 输出器。只输出可被 json.loads 解析的 JSON 对象。",
        prompt=prompt,
        temperature=0.25,
        max_tokens=4096,
    )
    return parse_llm_json_object(raw)

# --- 存档管理逻辑 ---
def load_checkpoint(novel_title: str = None, novel_id: str = None) -> dict:
    """加载checkpoint，如果novel_title为None，则自动查找最新的checkpoint"""
    if novel_id:
        return find_checkpoint_by_novel_id(novel_id)

    if novel_title:
        checkpoint_path = get_checkpoint_path(novel_title)
        if os.path.exists(checkpoint_path):
            return load_checkpoint_path(checkpoint_path)
    else:
        current_id = get_current_novel_id()
        if current_id:
            current_state = find_checkpoint_by_novel_id(current_id)
            if current_state:
                return current_state

        active_novels = list_novel_summaries(include_archived=False)
        if active_novels:
            return find_checkpoint_by_novel_id(active_novels[0]["novel_id"])
    return None

def save_checkpoint(state: dict, novel_title: str = None, novel_id: str = None):
    ensure_novel_id(state)
    active_user_id = get_active_user_id()
    if active_user_id:
        state["user_id"] = active_user_id
    now = time.time()
    state.setdefault("created_at", now)
    state["updated_at"] = now
    state.pop("_checkpoint_path", None)
    state.pop("_novel_dir", None)
    checkpoint_path = get_checkpoint_path(novel_title, novel_id or get_state_novel_id(state))
    state["novel_dir_name"] = os.path.basename(os.path.dirname(checkpoint_path))
    state["updated_at"] = time.time()
    os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
    with open(checkpoint_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    # P0 2 缓存：任何 checkpoint 写入都让 status / novels / stats 失效
    invalidate_for_novel(get_state_novel_id(state))




def split_long_paragraph(paragraph: str, soft_limit: int = 120, hard_limit: int = 220) -> list[str]:
    text = (paragraph or "").strip()
    if len(text) <= hard_limit:
        return [text] if text else []
    parts = re.split(r"(?<=[。！？；…])", text)
    chunks: list[str] = []
    buf = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if buf and len(buf) + len(part) > hard_limit:
            chunks.append(buf.strip())
            buf = part
        else:
            buf += part
        if len(buf) >= soft_limit and re.search(r"[。！？；…]$", buf):
            chunks.append(buf.strip())
            buf = ""
    if buf.strip():
        chunks.append(buf.strip())
    return chunks or [text]

def normalize_chapter_paragraphs(content: str) -> str:
    """Normalize chapter text for mobile novel publishing: blank lines between readable paragraphs."""
    text = (content or "").strip()
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    raw_lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not raw_lines:
        return ""

    title = ""
    if re.match(r"^第\s*[一二三四五六七八九十百千万零\d]+\s*章", raw_lines[0]):
        title = raw_lines[0]
        raw_lines = raw_lines[1:]

    paragraphs: list[str] = []
    for line in raw_lines:
        if len(line) > 220:
            paragraphs.extend(split_long_paragraph(line))
        else:
            paragraphs.append(line)

    cleaned = ([title] if title else []) + [p for p in paragraphs if p]
    return "\n\n".join(cleaned).strip()

def save_chapter(chapter_num: int, content: str, novel_title: str = None, novel_id: str = None):
    content = normalize_chapter_paragraphs(content)
    novel_dir = get_novel_dir(novel_title, novel_id)
    output_dir = os.path.join(novel_dir, "chapters")
    os.makedirs(output_dir, exist_ok=True)
    filename = os.path.join(output_dir, chapter_file_name(chapter_num))
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)

def chapter_file_name(chapter_num: int) -> str:
    # 保持兼容旧版本已经写出的章节文件名。
    return f"第{chapter_num}章.txt"

def chapter_file_candidates(chapter_num: int) -> list[str]:
    return [
        chapter_file_name(chapter_num),
        f"第{chapter_num}章.txt",
        f"chapter_{chapter_num}.txt",
        f"{chapter_num}.txt",
    ]

def get_state_chapters_dir(state: dict) -> str:
    novel_dir = state.get("_novel_dir")
    if not novel_dir:
        novel_dir = get_novel_dir(state.get("novel_title", ""), get_state_novel_id(state))
    return os.path.join(novel_dir, "chapters")

def find_chapter_file(state: dict, chapter_num: int) -> str | None:
    chapters_dir = get_state_chapters_dir(state)
    for name in chapter_file_candidates(chapter_num):
        path = os.path.join(chapters_dir, name)
        if os.path.exists(path):
            return path
    if os.path.exists(chapters_dir):
        for name in os.listdir(chapters_dir):
            if name.endswith(".txt") and re.search(rf"(?<!\d){chapter_num}(?!\d)", name):
                return os.path.join(chapters_dir, name)
    return None

def read_chapter_from_state(state: dict, chapter_num: int) -> str:
    chapter_path = find_chapter_file(state, chapter_num)
    if chapter_path:
        with open(chapter_path, "r", encoding="utf-8") as f:
            return f.read()
    content = StoryBible(get_state_novel_id(state)).get_chapter_content(chapter_num)
    if content:
        return content
    raise HTTPException(status_code=404, detail="章节内容不存在")

def get_publish_status_path(state: dict) -> str:
    novel_dir = state.get("_novel_dir")
    if not novel_dir:
        novel_dir = get_novel_dir(state.get("novel_title", ""), get_state_novel_id(state))
    return os.path.join(novel_dir, "publish_status.json")

def read_publish_statuses(state: dict) -> dict[str, Any]:
    data = read_json_file(get_publish_status_path(state)) or {}
    return data if isinstance(data, dict) else {}

def write_publish_statuses(state: dict, statuses: dict[str, Any]):
    write_json_file(get_publish_status_path(state), statuses)

def get_chapter_publish_status(state: dict, chapter_num: int) -> dict[str, Any]:
    statuses = read_publish_statuses(state)
    item = statuses.get(str(chapter_num), {})
    if not isinstance(item, dict):
        item = {}
    return {
        "status": item.get("status", "not_prepared"),
        "note": item.get("note", ""),
        "updated_at": item.get("updated_at"),
    }

def set_chapter_publish_status(state: dict, chapter_num: int, status: str, note: str = "") -> dict[str, Any]:
    allowed = {
        "not_prepared",
        "checked",
        "copied",
        "browser_filled",
        "save_clicked",
        "draft_saved",
        "failed",
        "needs_review",
    }
    normalized = status if status in allowed else "not_prepared"
    statuses = read_publish_statuses(state)
    item = {
        "status": normalized,
        "note": note.strip(),
        "updated_at": time.time(),
    }
    statuses[str(chapter_num)] = item
    write_publish_statuses(state, statuses)
    return item

def split_publish_chapter(chapter_num: int, content: str, fallback_title: str = "") -> dict[str, str]:
    raw = content or ""
    lines = raw.splitlines()
    first_idx = next((idx for idx, line in enumerate(lines) if line.strip()), None)
    fallback = (fallback_title or f"第{chapter_num}章").strip()
    if first_idx is None:
        return {"title": fallback, "body": ""}

    first_line = lines[first_idx].strip()
    title_like = (
        len(first_line) <= 60
        and (
            re.match(r"^第\s*[0-9一二三四五六七八九十百千万]+\s*章", first_line)
            or first_line.startswith(f"第{chapter_num}章")
            or first_line.startswith(f"第 {chapter_num} 章")
        )
    )
    if not title_like:
        return {"title": fallback, "body": raw.strip()}

    body_lines = lines[:first_idx] + lines[first_idx + 1:]
    return {
        "title": first_line,
        "body": "\n".join(body_lines).strip(),
    }

def build_publish_package(state: dict, chapter_num: int) -> dict[str, Any]:
    content = read_chapter_from_state(state, chapter_num)
    chapter_item = next(
        (item for item in list_saved_chapters_for_state(state) if int(item.get("chapter_num", 0)) == chapter_num),
        {},
    )
    split = split_publish_chapter(chapter_num, content, chapter_item.get("title", ""))
    title = split["title"]
    body = split["body"]
    target_words = int(state.get("chapter_target_words", 2500) or 2500)
    min_words, max_words = get_chapter_word_range(target_words)

    from agents.editor import (
        detect_fanqie_content_safety_risks,
        detect_platform_format_risks,
        detect_emoji_and_symbol_risks,
        detect_ai_cliche_risks,
    )

    format_risks = detect_platform_format_risks(content, min_words, max_words, len(content))
    safety_risks = detect_fanqie_content_safety_risks(f"{title}\n{body}")
    emoji_risks = detect_emoji_and_symbol_risks(content)
    cliche_risks = detect_ai_cliche_risks(content)
    risks = format_risks + safety_risks + emoji_risks + cliche_risks
    must_fix_count = len([risk for risk in risks if risk.get("severity") == "must_fix"])
    warning_count = len([risk for risk in risks if risk.get("severity") == "warning"])
    current_status = get_chapter_publish_status(state, chapter_num)
    suggested_status = "needs_review" if must_fix_count else "checked"
    if current_status.get("status") in {"not_prepared", "needs_review", "checked"}:
        current_status = set_chapter_publish_status(state, chapter_num, suggested_status)

    return {
        "novel_id": get_state_novel_id(state),
        "novel_title": state.get("novel_title", "未命名作品"),
        "chapter_num": chapter_num,
        "chapter_title": title,
        "body": body,
        "full_content": content,
        "word_count": len(body),
        "full_word_count": len(content),
        "risk_summary": {
            "must_fix": must_fix_count,
            "warning": warning_count,
            "total": len(risks),
            "passed": must_fix_count == 0,
        },
        "risks": risks,
        "publish_status": current_status,
    }

def list_saved_chapters_for_state(state: dict) -> list[dict[str, Any]]:
    chapters: dict[int, dict[str, Any]] = {}
    publish_statuses = read_publish_statuses(state)
    chapters_dir = get_state_chapters_dir(state)
    if os.path.exists(chapters_dir):
        for name in os.listdir(chapters_dir):
            if not name.endswith(".txt"):
                continue
            match = re.search(r"(\d+)", name)
            if not match:
                continue
            chapter_num = int(match.group(1))
            path = os.path.join(chapters_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                content = ""
            first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
            chapters[chapter_num] = {
                "chapter_num": chapter_num,
                "title": first_line[:40] or f"第 {chapter_num} 章",
                "word_count": len(content),
                "preview": content.replace("\n", " ")[:120],
                "publish_status": publish_statuses.get(str(chapter_num), {}).get("status", "not_prepared"),
            }

    for chapter_num in state.get("completed_chapters", []) or []:
        try:
            chapter_num = int(chapter_num)
        except (TypeError, ValueError):
            continue
        if chapter_num not in chapters:
            chapters[chapter_num] = {
                "chapter_num": chapter_num,
                "title": f"第 {chapter_num} 章",
                "word_count": 0,
                "preview": "",
                "publish_status": publish_statuses.get(str(chapter_num), {}).get("status", "not_prepared"),
            }
    return [chapters[key] for key in sorted(chapters)]

def safe_export_filename(name: str, suffix: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (name or "未命名作品")).strip(" ._")
    return f"{cleaned or '未命名作品'}{suffix}"

def attachment_headers(filename: str) -> dict[str, str]:
    quoted = quote(filename)
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"}

def chapter_display_title(chapter: dict[str, Any]) -> str:
    chapter_num = chapter.get("chapter_num")
    title = str(chapter.get("title") or "").strip()
    if title and not title.startswith("第"):
        return f"第 {chapter_num} 章 {title}"
    return title or f"第 {chapter_num} 章"

def collect_export_chapters(state: dict, chapter_num: int | None = None) -> list[dict[str, Any]]:
    chapters = list_saved_chapters_for_state(state)
    if chapter_num is not None:
        chapters = [item for item in chapters if int(item.get("chapter_num", 0)) == int(chapter_num)]
    result = []
    for item in chapters:
        num = int(item.get("chapter_num", 0))
        if not num:
            continue
        content = read_chapter_from_state(state, num)
        result.append({**item, "content": content})
    if not result:
        raise HTTPException(status_code=404, detail="没有可导出的章节")
    return result

def build_export_text(title: str, chapters: list[dict[str, Any]]) -> str:
    parts = [title.strip() or "未命名作品"]
    for chapter in chapters:
        parts.append(chapter_display_title(chapter))
        parts.append((chapter.get("content") or "").strip())
    return "\n\n".join(part for part in parts if part)

def xml_safe_text(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text or ""))

def docx_paragraph(text: str, bold: bool = False, size: int | None = None) -> str:
    runs = []
    for segment_index, segment in enumerate(xml_safe_text(text).split("\n")):
        if segment_index:
            runs.append("<w:r><w:br/></w:r>")
        props = ""
        if bold or size:
            inner = ""
            if bold:
                inner += "<w:b/>"
            if size:
                inner += f'<w:sz w:val="{size}"/>'
            props = f"<w:rPr>{inner}</w:rPr>"
        runs.append(f'<w:r>{props}<w:t xml:space="preserve">{escape_xml(segment)}</w:t></w:r>')
    return "<w:p>" + "".join(runs) + "</w:p>"

def build_docx(title: str, chapters: list[dict[str, Any]]) -> bytes:
    body = [docx_paragraph(title.strip() or "未命名作品", bold=True, size=32)]
    for chapter in chapters:
        body.append(docx_paragraph(chapter_display_title(chapter), bold=True, size=28))
        for paragraph in (chapter.get("content") or "").splitlines():
            body.append(docx_paragraph(paragraph))
    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {''.join(body)}
    <w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
  </w:body>
</w:document>'''
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>''')
        docx.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>''')
        docx.writestr("word/document.xml", document_xml)
    return buffer.getvalue()

def export_response(title: str, chapters: list[dict[str, Any]], export_format: str, chapter_num: int | None = None) -> Response:
    normalized = (export_format or "txt").lower()
    suffix_base = f"_第{chapter_num}章" if chapter_num else "_全书"
    if normalized == "docx":
        filename = safe_export_filename(title, f"{suffix_base}.docx")
        return Response(
            content=build_docx(title, chapters),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers=attachment_headers(filename),
        )
    if normalized != "txt":
        raise HTTPException(status_code=400, detail="导出格式只支持 txt 或 docx")
    filename = safe_export_filename(title, f"{suffix_base}.txt")
    return Response(
        content=build_export_text(title, chapters).encode("utf-8-sig"),
        media_type="text/plain; charset=utf-8",
        headers=attachment_headers(filename),
    )

def build_revision_impact_report(state: dict, chapter_num: int, old_content: str, new_content: str) -> dict[str, Any]:
    old_text = old_content or ""
    new_text = new_content or ""
    diff_rows = make_text_diff(old_text, new_text)
    added = "\n".join(row["text"] for row in diff_rows if row["type"] == "added")
    removed = "\n".join(row["text"] for row in diff_rows if row["type"] == "removed")
    changed_text = f"{added}\n{removed}"
    changed_chars = len(added) + len(removed)
    change_ratio = changed_chars / max(len(old_text), 1)

    category_patterns = {
        "人物状态": r"死亡|活着|受伤|失踪|背叛|身份|真实身份|关系|记忆|怀孕|婚约",
        "能力物品": r"系统|金手指|能力|功法|境界|修为|神器|钥匙|文件|证据|药|毒",
        "势力关系": r"家族|集团|宗门|公司|联盟|敌人|合作|交易|掌权|继承",
        "伏笔真相": r"伏笔|真相|秘密|线索|暴露|隐藏|幕后|误会|反转",
        "时间线": r"昨天|今天|明天|三年|十年|小时|天后|此前|之后|同时",
    }
    categories = [name for name, pattern in category_patterns.items() if re.search(pattern, changed_text)]
    later_chapters = [
        item for item in list_saved_chapters_for_state(state)
        if int(item.get("chapter_num", 0)) > chapter_num
    ]
    if change_ratio >= 0.18 or (categories and later_chapters):
        severity = "high"
        advice = "这次修订可能影响后续连贯性，建议继续做影响分析和后文修补。"
    elif change_ratio >= 0.05 or categories:
        severity = "medium"
        advice = "这次修订有一定影响，建议检查后续章节是否需要补丁。"
    else:
        severity = "low"
        advice = "这次更像局部润色，通常不需要重写后文。"
    return {
        "severity": severity,
        "changed_chars": changed_chars,
        "change_ratio": round(change_ratio, 3),
        "categories": categories,
        "later_chapter_count": len(later_chapters),
        "advice": advice,
    }

def normalize_chapter_controls(controls: ChapterControlReq | None) -> dict[str, Any]:
    if not controls:
        return {}
    data = controls.model_dump()
    if data.get("target_words") is not None:
        try:
            data["target_words"] = max(500, int(data["target_words"]))
        except (TypeError, ValueError):
            data["target_words"] = None
    return {key: value for key, value in data.items() if value not in ("", None, [], {})}

def format_chapter_controls_text(controls: dict[str, Any], chapter_num: int) -> str:
    if not controls:
        return ""
    labels = {
        "free_instruction": "用户自然语言指导",
        "chapter_goal": "本章目标",
        "mood": "情绪基调",
        "pace": "节奏",
        "pov": "叙事视角",
        "target_words": "本章目标字数",
        "must_characters": "必须出现人物",
        "must_settings": "必须引用设定",
        "must_hooks": "必须处理伏笔",
        "ending_hook": "结尾钩子",
        "notes": "临时备注",
    }
    lines = [f"【第 {chapter_num} 章写作控制】"]
    for key, label in labels.items():
        value = controls.get(key)
        if value not in ("", None, [], {}):
            lines.append(f"- {label}：{value}")
    return "\n".join(lines)

# ================= 接口逻辑 =================

@app.get("/api/auth/me")
async def auth_me(request: Request):
    if not request.state.user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"user": user_public(request.state.user)}

@app.post("/api/auth/register")
async def auth_register(req: RegisterReq, request: Request, response: Response):
    try:
        user = AUTH.create_user(req.username, req.password, req.invite_code)
        session = AUTH.create_session(user["id"], request.headers.get("user-agent", ""))
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return {"status": "success", "user": user_public(user)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/auth/login")
async def auth_login(req: LoginReq, request: Request, response: Response):
    user = AUTH.verify_password(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    session = AUTH.create_session(user["id"], request.headers.get("user-agent", ""))
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return {"status": "success", "user": user_public(user)}

@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    AUTH.delete_session(request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"status": "success"}


# ═══════════════════════════════════════════════════════════════
# 风格 preset API
# ═══════════════════════════════════════════════════════════════
@app.get("/api/style_presets")
async def list_style_presets():
    """返回所有可用的网文风格 preset（用于前端下拉 + 预览）。"""
    try:
        from prompts_style_presets import list_style_presets as _list_presets
        return {"status": "success", "presets": _list_presets()}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/style_presets/{key}")
async def get_style_preset(key: str):
    """返回单个 preset 的完整内容（前端预览用）。"""
    try:
        from prompts_style_presets import get_style_preset as _get_preset
        p = _get_preset(key)
        if not p:
            raise HTTPException(status_code=404, detail=f"风格 preset '{key}' 不存在")
        return {"status": "success", "preset": p}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/workbench/brainstorm")
async def brainstorm_novel(req: BrainstormReq):
    try:
        from models import call_llm
        from prompts import BRAINSTORM_SYSTEM, BRAINSTORM_PROMPT
        from prompts_style_presets import build_style_preset_prompt_block

        # 注入 style_preset_block（无 preset 时为空字符串）
        preset_block = build_style_preset_prompt_block(req.style_preset_key or "")
        if not preset_block:
            preset_block = ""

        prompt = BRAINSTORM_PROMPT.format(
            seed=req.seed if req.seed.strip() else "（请发挥想象，任意构思一个时下热门的网文开局）",
            style_preset_block=preset_block,
        )
        
        raw_res = call_llm(
            role="planner",
            system_prompt=BRAINSTORM_SYSTEM,
            prompt=prompt,
            temperature=0.8,
            max_tokens=8192  # 思考型模型需要更多预算
        )
        if not raw_res:
             raise HTTPException(status_code=503, detail="模型未返回任何结果，可能是内容被过滤或频率过高")
             
        data = parse_llm_json_object(raw_res)
        return {"status": "success", "result": data}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/workbench/concept_directions")
async def generate_concept_directions(req: BrainstormReq):
    try:
        from models import call_llm
        from prompts import CONCEPT_DIRECTIONS_SYSTEM, CONCEPT_DIRECTIONS_PROMPT
        from prompts_style_presets import build_style_preset_prompt_block

        seed = req.seed.strip() or "被退婚后我成了超绝大佬"
        # 注入 style_preset_block
        preset_block = build_style_preset_prompt_block(req.style_preset_key or "")
        if not preset_block:
            preset_block = "（未选择风格预设）"
        prompt = CONCEPT_DIRECTIONS_PROMPT.format(seed=seed, style_preset_block=preset_block)
        raw_res = call_llm(
            role="planner",
            system_prompt=CONCEPT_DIRECTIONS_SYSTEM,
            prompt=prompt,
            temperature=0.85,
            max_tokens=8192,
        )
        if not raw_res:
            raise HTTPException(status_code=503, detail="模型未返回任何结果")

        parse_warning = ""
        try:
            data = parse_llm_json_object(raw_res)
        except Exception as parse_error:
            parse_warning = f"首次 JSON 解析失败，已自动清洗：{parse_error}"
            print(f"  ⚠️ 方向 JSON 解析失败，尝试宽松提取：{parse_error}")
            lenient_directions = lenient_extract_concept_directions(raw_res, seed)
            if lenient_directions:
                data = {"directions": lenient_directions}
            else:
                print("  ⚠️ 宽松提取失败，尝试重新生成方向")
                try:
                    data = regenerate_concept_directions(seed, raw_res, str(parse_error))
                except Exception as retry_error:
                    parse_warning = f"模型 JSON 连续解析失败，已使用稳定兜底方向：{retry_error}"
                    print(f"  ⚠️ 方向 JSON 重试仍失败，使用兜底方向：{retry_error}")
                    data = {"directions": fallback_concept_directions(seed)}

        directions = normalize_concept_directions(data, seed)
        if len(directions) < 3:
            for item in fallback_concept_directions(seed):
                if item["title"] not in {direction["title"] for direction in directions}:
                    directions.append(item)
                if len(directions) >= 3:
                    break
        return {
            "status": "success",
            "result": {
                "seed": seed,
                "directions": directions[:3],
                "warning": parse_warning,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/workbench/refine_concept")
async def refine_concept(req: ConceptRefineReq):
    try:
        from models import call_llm
        from prompts import CONCEPT_REFINEMENT_SYSTEM, CONCEPT_REFINEMENT_PROMPT
        from prompts_style_presets import build_style_preset_prompt_block

        seed = req.seed.strip() or "（用户未填写原始灵感）"
        direction_json = json.dumps(req.direction, ensure_ascii=False, indent=2)
        # 注入 style_preset_block
        preset_block = build_style_preset_prompt_block(req.style_preset_key or "")
        if not preset_block:
            preset_block = ""
        # P2 增强：把章节数限制在 5-100 之间，0 表示不让 AI 生成 chapter_constraints
        num_chapters = max(5, min(100, int(req.num_chapters or 0))) if req.num_chapters else 0

        prompt = CONCEPT_REFINEMENT_PROMPT.format(
            seed=seed,
            direction_json=direction_json,
            style_preset_block=preset_block,
            num_chapters=num_chapters,
            chapter_constraints_instruction=(
                f"13. chapter_constraints：生成 **{num_chapters} 条**章节约束清单（覆盖整部作品 1..{num_chapters} 章，章节号必须严格连续）。\n"
                f"   **每条必须精简**（整部清单加起来不能超过 8000 字符，否则 JSON 会被截断）：\n"
                f"   - chapter_num: 整数（1..{num_chapters}）\n"
                f"   - purpose: 本章作用，**10-20 字**（如'建立金手指 + 第一次小爽点'）\n"
                f"   - core_event: 核心事件，**20-40 字**（如'主角被对手当众羞辱，意外激活系统'）\n"
                f"   - required_characters: 必出场角色，**单行紧凑列表**（如'林辰、赵主管'，不要 3-4 字段）\n"
                f"   - required_settings: 必引用设定/物品/规则，**单行紧凑列表**（如'反讽系统、办公室'）\n"
                f"   - ending_hook: 章末钩子，**10-20 字**（如'新对手登场'）\n"
                f"   - avoid: 要避免的跑偏，**8-15 字关键词**（如'避免主角无敌'）\n"
                f"   **升级节奏**：1-15 章开局（建立金手指 + 第一次爽点 + 第一次小高潮），16-40 章中期爬升（势力扩大、规则代价、关系网展开），41-{num_chapters} 章升级高潮（新地图/新对手/代价抉择）。\n"
                f"   **角色/设定引用**：required_characters 必须是 main_cast / key_factions 里的具体名称；required_settings 必须是 world_rules 或既有设定里的具体规则/物品/势力。\n"
                f"   **整体精简提示**：避免任何章节写 100+ 字的事件描述，避免给配角写传记，避免堆砌对话示例。每章用 5-7 行 JSON 即可。"
            ) if num_chapters else "13. chapter_constraints：空数组 []（用户未指定章节数）。",
        )
        raw_res = call_llm(
            role="planner",
            system_prompt=CONCEPT_REFINEMENT_SYSTEM,
            prompt=prompt,
            temperature=0.7,
            max_tokens=14336,  # P2 增强：chapter_constraints 100 条 + 12 字段需要更多 token
        )
        if not raw_res:
            raise HTTPException(status_code=503, detail="模型未返回任何结果")

        data = parse_llm_json_object(raw_res)
        if not data.get("core_setting"):
            raise HTTPException(status_code=500, detail="模型未返回有效的核心设定")
        return {"status": "success", "result": data}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/workbench/opening_outline")
async def generate_opening_outline(req: OpeningOutlineReq):
    try:
        from models import call_llm
        from prompts import OPENING_OUTLINE_SYSTEM, OPENING_OUTLINE_PROMPT

        prompt = OPENING_OUTLINE_PROMPT.format(
            seed=req.seed.strip() or "（用户未填写原始灵感）",
            direction_json=json.dumps(req.direction or {}, ensure_ascii=False, indent=2),
            refinement_json=json.dumps(req.refinement, ensure_ascii=False, indent=2),
            full_outline_json=json.dumps(req.full_outline or {}, ensure_ascii=False, indent=2),
        )
        raw_res = call_llm(
            role="planner",
            system_prompt=OPENING_OUTLINE_SYSTEM,
            prompt=prompt,
            temperature=0.65,
            max_tokens=8192,
        )
        if not raw_res:
            raise HTTPException(status_code=503, detail="模型未返回任何结果")

        data = parse_llm_json_object(raw_res)
        chapters = data.get("chapters", [])
        if not isinstance(chapters, list) or len(chapters) < 3:
            raise HTTPException(status_code=500, detail="模型未返回有效的开局章节规划")
        return {"status": "success", "result": data}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


def build_default_full_outline_acts(num_chapters: int) -> list[dict[str, str]]:
    ranges = [
        ("开局", 1, max(1, round(num_chapters * 0.10)), "建立主角处境、核心吸引力和第一组冲突。", "主角完成第一次有效反击，长期主线浮出水面。"),
        ("发展", max(1, round(num_chapters * 0.10)) + 1, max(1, round(num_chapters * 0.40)), "扩大冲突链条，展开势力、规则、成长代价。", "主角发现更深层敌人或规则真相。"),
        ("中段转折", max(1, round(num_chapters * 0.40)) + 1, max(1, round(num_chapters * 0.70)), "让主线矛盾升级，回收早期伏笔并制造重大转折。", "主角付出代价后获得进入终局的资格。"),
        ("高潮决战", max(1, round(num_chapters * 0.70)) + 1, max(1, round(num_chapters * 0.90)), "集中爆发核心矛盾，推动终极对抗。", "最终敌人与最终选择摆到台前。"),
        ("结局收束", max(1, round(num_chapters * 0.90)) + 1, num_chapters, "解决主线矛盾，交代角色命运，完成情绪收束。", "全书完成闭环。"),
    ]
    acts = []
    for name, start, end, purpose, turning_point in ranges:
        start = min(max(1, start), num_chapters)
        end = min(max(start, end), num_chapters)
        acts.append({
            "name": name,
            "chapter_range": f"{start}-{end}",
            "purpose": purpose,
            "turning_point": turning_point,
        })
    return acts


def normalize_full_outline_data(data: dict[str, Any], num_chapters: int) -> dict[str, Any]:
    try:
        total = int(num_chapters)
    except (TypeError, ValueError):
        total = 100
    total = max(1, min(total, 300))

    raw_chapters = data.get("chapters", [])
    if not isinstance(raw_chapters, list):
        raw_chapters = []

    by_num: dict[int, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for index, item in enumerate(raw_chapters, 1):
        if not isinstance(item, dict):
            continue
        try:
            chapter_num = int(item.get("chapter_num", index))
        except (TypeError, ValueError):
            chapter_num = index
        item = {**item, "chapter_num": chapter_num}
        if 1 <= chapter_num <= total and chapter_num not in by_num:
            by_num[chapter_num] = item
        ordered.append(item)

    normalized_chapters = []
    for chapter_num in range(1, total + 1):
        item = by_num.get(chapter_num)
        if not item and chapter_num - 1 < len(ordered):
            item = ordered[chapter_num - 1]
        item = item if isinstance(item, dict) else {}
        normalized_chapters.append({
            "chapter_num": chapter_num,
            "title": str(item.get("title") or f"第{chapter_num}章").strip(),
            "purpose": str(item.get("purpose") or "承接全书主线，推进本阶段剧情。").strip(),
            "main_event": str(item.get("main_event") or item.get("core_event") or "围绕主线矛盾展开新的行动。").strip(),
            "conflict": str(item.get("conflict") or "主角目标与外部阻力发生碰撞。").strip(),
            "reader_hook": str(item.get("reader_hook") or item.get("ending_hook") or "留下下一章的期待点。").strip(),
            "continuity_note": str(item.get("continuity_note") or "承接前章结果，并为后续剧情埋下推进点。").strip(),
        })

    acts = data.get("acts", [])
    if not isinstance(acts, list) or not acts:
        acts = build_default_full_outline_acts(total)

    return {
        "num_chapters": total,
        "overall_arc": str(data.get("overall_arc") or data.get("summary") or "全书围绕主角成长、核心矛盾升级与最终收束展开。").strip(),
        "acts": acts,
        "chapters": normalized_chapters,
    }


@app.post("/api/workbench/full_outline")
async def generate_full_outline(req: FullOutlineReq):
    try:
        from models import call_llm
        from prompts import FULL_OUTLINE_SYSTEM, FULL_OUTLINE_PROMPT

        total = max(1, min(int(req.num_chapters or 100), 300))
        prompt = FULL_OUTLINE_PROMPT.format(
            seed=req.seed.strip() or "（用户未填写原始灵感）",
            direction_json=json.dumps(req.direction or {}, ensure_ascii=False, indent=2),
            refinement_json=json.dumps(req.refinement, ensure_ascii=False, indent=2),
            num_chapters=total,
            opening_outline_json=json.dumps(req.opening_outline or {}, ensure_ascii=False, indent=2),
        )
        raw_res = call_llm(
            role="planner",
            system_prompt=FULL_OUTLINE_SYSTEM,
            prompt=prompt,
            temperature=0.65,
            max_tokens=16384,
        )
        if not raw_res:
            raise HTTPException(status_code=503, detail="模型未返回任何结果")

        data = parse_llm_json_object(raw_res)
        outline = normalize_full_outline_data(data, total)
        return {"status": "success", "result": outline}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """系统面板：Token 用量 + 备份管理"""
    dashboard_path = os.path.join(STATIC_DIR, "dashboard.html")
    if not os.path.exists(dashboard_path):
        return HTMLResponse("Dashboard 文件未找到", status_code=404)
    with open(dashboard_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/status")
@cached("status", ttl=3.0)
async def get_status():
    """获取当前进度，如果有存档则返回存档预览"""
    state = load_checkpoint()

    if state:
        if get_current_novel_id() != get_state_novel_id(state):
            set_current_novel_id(get_state_novel_id(state))
        saved_chapters = list_saved_chapters_for_state(state)
        last_saved_chapter = saved_chapters[-1]["chapter_num"] if saved_chapters else 0
        return {
            "has_checkpoint": True,
            "novel_id": state.get("novel_id", DEFAULT_NOVEL_ID),
            "title": state.get("novel_title"),
            "current_chapter": state.get("current_chapter", 1),
            "completed_count": len(state.get("completed_chapters", [])),
            "last_saved_chapter": last_saved_chapter,
            "saved_chapters": saved_chapters,
            "setting": state.get("novel_setting", ""),
            "style": state.get("novel_style", "风格不限"),
            "genre": state.get("novel_genre", "题材不限"),
            "num_chapters": state.get("num_chapters", 100),
            "chapter_target_words": state.get("chapter_target_words", 2000),
            "last_chapter_controls": state.get("current_chapter_controls", {}),
            "chapter_control_history": state.get("chapter_control_history", []),
            "story_so_far": state.get("story_so_far", "")
        }
    return {"has_checkpoint": False}

@app.get("/api/novels")
@cached("novels", ttl=10.0)
async def get_novels(include_archived: bool = False):
    return {
        "current_novel_id": get_current_novel_id(),
        "novels": list_novel_summaries(include_archived=include_archived)
    }

@app.get("/api/novels/{novel_id}/chapters")
async def get_novel_chapters(novel_id: str):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    return {
        "status": "success",
        "novel_id": novel_id,
        "title": state.get("novel_title", "未命名作品"),
        "chapters": list_saved_chapters_for_state(state),
    }


@app.get("/api/novels/{novel_id}/chapters/stats")
@cached("chapters_stats", ttl=15.0)
async def get_novel_chapters_stats(novel_id: str):
    """批量统计：一次返回所有已写章节的字数 + 标题（轻量版，避免前端并发 50 次 fetch）。

    适用：写作总览 / 仪表盘 / 章节字数排行等只关心"字数"和"标题"的场景。
    性能：复用 list_saved_chapters_for_state，单次扫盘 O(章数)，比前端并发 50 次 fetch 少 1-2 个数量级延迟。
    缓存：15 秒 TTL，章节保存/删除时通过 invalidate_for_novel 主动失效。
    """
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    full = list_saved_chapters_for_state(state)
    # 只返回轻量字段（word_count / title / chapter_num / publish_status）
    slim = [
        {
            "chapter_num": ch.get("chapter_num"),
            "title": ch.get("title", ""),
            "word_count": int(ch.get("word_count", 0) or 0),
            "publish_status": ch.get("publish_status", "not_prepared"),
        }
        for ch in full
    ]
    total_words = sum(item["word_count"] for item in slim)
    return {
        "status": "success",
        "novel_id": novel_id,
        "title": state.get("novel_title", "未命名作品"),
        "chapter_count": len(slim),
        "total_words": total_words,
        "chapters": slim,
    }


# P0 2：缓存诊断端点（仅 admin 可见，监控命中率 / 缓存大小）
@app.get("/api/admin/cache_stats")
async def get_cache_stats(request: Request):
    if not getattr(request.state, "user", None) or not request.state.user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要 admin 权限")
    return {
        "status": "success",
        "cache": ttl_cache.stats(),
        "namespaces": {
            "status_ttl": 3.0,
            "novels_ttl": 10.0,
            "chapters_stats_ttl": 15.0,
        }
    }


@app.post("/api/admin/cache_invalidate")
async def post_cache_invalidate(request: Request):
    """手动失效所有缓存（debug 用）"""
    if not getattr(request.state, "user", None) or not request.state.user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要 admin 权限")
    ttl_cache.invalidate_all()
    return {"status": "success", "message": "缓存已清空", "cache": ttl_cache.stats()}


@app.get("/api/novels/{novel_id}/chapters/{chapter_num}")
async def get_novel_chapter(novel_id: str, chapter_num: int):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    content = read_chapter_from_state(state, chapter_num)
    return {
        "status": "success",
        "novel_id": novel_id,
        "chapter_num": chapter_num,
        "content": content,
        "word_count": len(content),
    }

@app.get("/api/novels/{novel_id}/chapters/{chapter_num}/publish_package")
async def get_publish_package(novel_id: str, chapter_num: int):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    package = build_publish_package(state, chapter_num)
    return {"status": "success", "result": package}

@app.post("/api/novels/{novel_id}/chapters/{chapter_num}/publish_status")
async def update_publish_status(novel_id: str, chapter_num: int, req: PublishStatusReq):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    item = set_chapter_publish_status(state, chapter_num, req.status, req.note)
    return {
        "status": "success",
        "message": "发布状态已更新",
        "publish_status": item,
    }

@app.get("/api/publish_uploader/capability")
def get_publish_uploader_capability():
    return {"status": "success", "result": PUBLISH_UPLOADER.capability()}

@app.post("/api/publish_uploader/start")
def start_publish_uploader(req: PublishUploadStartReq):
    try:
        session = PUBLISH_UPLOADER.start(req.writer_url)
        return {"status": "success", "result": session.to_public()}
    except PublishUploaderError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"启动上传浏览器失败：{e}")

@app.get("/api/publish_uploader/{session_id}")
def get_publish_uploader_session(session_id: str):
    try:
        return {"status": "success", "result": PUBLISH_UPLOADER.get(session_id).to_public()}
    except PublishUploaderError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.post("/api/publish_uploader/{session_id}/close")
def close_publish_uploader_session(session_id: str):
    try:
        return {"status": "success", "result": PUBLISH_UPLOADER.close(session_id)}
    except PublishUploaderError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.post("/api/novels/{novel_id}/chapters/{chapter_num}/publish_upload/{session_id}/fill")
def fill_publish_upload_session(novel_id: str, chapter_num: int, session_id: str):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    package = build_publish_package(state, chapter_num)
    if package.get("risk_summary", {}).get("must_fix", 0):
        raise HTTPException(status_code=400, detail="本章仍有必须处理的发布风险，请先修订后再自动填入")
    try:
        result = PUBLISH_UPLOADER.fill_chapter(session_id, package["chapter_title"], package["body"])
        if result.get("filled_title") and result.get("filled_body"):
            set_chapter_publish_status(state, chapter_num, "browser_filled", "已通过浏览器助手填入番茄后台，等待作者确认保存草稿")
        return {"status": "success", "result": result}
    except PublishUploaderError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"自动填入失败：{e}")

@app.post("/api/novels/{novel_id}/chapters/{chapter_num}/publish_upload/{session_id}/try_save")
def try_save_publish_upload_session(novel_id: str, chapter_num: int, session_id: str):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    try:
        result = PUBLISH_UPLOADER.try_save_draft(session_id)
        if result.get("clicked"):
            set_chapter_publish_status(state, chapter_num, "save_clicked", "已尝试点击保存草稿，请作者在番茄后台确认保存结果")
        return {"status": "success", "result": result}
    except PublishUploaderError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"尝试保存草稿失败：{e}")

@app.post("/api/novels/{novel_id}/chapters/{chapter_num}/save")
async def save_novel_chapter_revision(novel_id: str, chapter_num: int, req: SaveChapterReq):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="章节内容不能为空")

    novel_title = state.get("novel_title", "")
    try:
        old_content = read_chapter_from_state(state, chapter_num)
    except HTTPException:
        old_content = ""
    normalized_content = normalize_chapter_paragraphs(req.content)
    normalized_content = ensure_chapter_title(normalized_content, chapter_num, old_content)
    impact_report = build_revision_impact_report(state, chapter_num, old_content, normalized_content)
    save_chapter(chapter_num, normalized_content, novel_title, novel_id)
    StoryBible(novel_id).add_chapter_version(chapter_num, "manual_revision", normalized_content, "用户前文修订保存")

    completed = set(int(num) for num in (state.get("completed_chapters", []) or []) if str(num).isdigit())
    completed.add(chapter_num)
    state["completed_chapters"] = sorted(completed)
    if chapter_num in {state.get("current_chapter", 1), state.get("current_chapter", 1) - 1}:
        state["chapter_content"] = normalized_content
    save_checkpoint(state, novel_title, novel_id)
    set_chapter_publish_status(state, chapter_num, "needs_review", "章节内容已修订，需要重新生成发布包并检查")

    return {
        "status": "success",
        "message": "章节修订已保存",
        "chapter_num": chapter_num,
        "word_count": len(normalized_content),
        "content": normalized_content,
        "impact_report": impact_report,
    }

@app.get("/api/novels/{novel_id}/export")
async def export_novel(novel_id: str, format: str = "txt"):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    title = state.get("novel_title", "未命名作品")
    chapters = collect_export_chapters(state)
    return export_response(title, chapters, format)

@app.get("/api/novels/{novel_id}/chapters/{chapter_num}/export")
async def export_novel_chapter(novel_id: str, chapter_num: int, format: str = "txt"):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    title = state.get("novel_title", "未命名作品")
    chapters = collect_export_chapters(state, chapter_num)
    return export_response(title, chapters, format, chapter_num)


@app.post("/api/novels/{novel_id}/compress_state")
async def compress_novel_state(novel_id: str, recent_limit: int = 8, synopsis_limit: int = 2500):
    """手动压缩作品的 state：
    1. recent_summaries 保留最近 N 项（直接丢弃溢出项 —— 早期内容早就在 global_synopsis 里程碑节点融过）
    2. global_synopsis 截断到 N 字符（如果超长；保留前 200 + 后 2300 字符）
    3. 重新拼接 story_so_far（= global_synopsis + 最近 N 项详细）
    4. 写回 checkpoint

    用途：state 太大导致 LLM prompt 超 32K token 时，主动瘦身。
    """
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")

    novel_title = state.get("novel_title", "")
    global_synopsis = state.get("global_synopsis", "") or ""
    recent_summaries = list(state.get("recent_summaries", []) or [])

    before_recent = len(recent_summaries)
    before_gs_len = len(global_synopsis)
    before_ssf_len = len(state.get("story_so_far", "") or "")
    before_bc_len = len(state.get("bible_context", "") or "")

    # 1. recent_summaries 截断（直接丢弃溢出项 —— 早期内容早就在 global_synopsis 里程碑节点融过）
    if len(recent_summaries) > recent_limit:
        new_recent = recent_summaries[len(recent_summaries) - recent_limit:]
        state["recent_summaries"] = new_recent
    else:
        new_recent = recent_summaries

    # 2. global_synopsis 截断（保留前 200 + 后 N-200 字符，标注"中段省略"）
    if len(global_synopsis) > synopsis_limit:
        head_chars = 200
        tail_chars = max(100, synopsis_limit - head_chars - 60)
        truncated_gs = (
            global_synopsis[:head_chars]
            + f"\n...（中段省略 {len(global_synopsis) - head_chars - tail_chars} 字符）...\n"
            + global_synopsis[-tail_chars:]
        )
        global_synopsis = truncated_gs
    state["global_synopsis"] = global_synopsis

    # 3. 重新拼接 story_so_far
    new_so_far = ""
    if global_synopsis:
        new_so_far += f"【全书剧情大事件（压缩总结）】\n{global_synopsis}\n\n"
    if new_recent:
        new_so_far += "【近期详细剧情】\n" + "\n\n".join(new_recent)
    if not new_so_far:
        new_so_far = "目前是第一章，故事刚刚开始。"
    state["story_so_far"] = new_so_far

    # 4. 写回 checkpoint
    save_checkpoint(state, novel_title, novel_id)

    after_recent = len(new_recent)
    after_gs_len = len(global_synopsis)
    after_ssf_len = len(new_so_far)

    return {
        "status": "success",
        "novel_id": novel_id,
        "novel_title": novel_title,
        "before": {
            "recent_summaries_count": before_recent,
            "global_synopsis_chars": before_gs_len,
            "story_so_far_chars": before_ssf_len,
            "bible_context_chars": before_bc_len,
        },
        "after": {
            "recent_summaries_count": after_recent,
            "global_synopsis_chars": after_gs_len,
            "story_so_far_chars": after_ssf_len,
        },
        "saved_ssf_chars": before_ssf_len - after_ssf_len,
    }

@app.post("/api/novels/{novel_id}/activate")
async def activate_novel(novel_id: str):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")

    if state.get("archived"):
        state["archived"] = False
        checkpoint_path = state.get("_checkpoint_path")
        state.pop("_checkpoint_path", None)
        state.pop("_novel_dir", None)
        write_json_file(checkpoint_path, state)

    set_current_novel_id(novel_id)
    # P0 2 缓存：切换作品后让 status/novels 失效（避免显示旧作品数据）
    ttl_cache.invalidate("status")
    ttl_cache.invalidate("novels")
    return {"status": "success", "message": "作品已打开", "novel_id": novel_id}

@app.post("/api/novels/{novel_id}/chapters/{chapter_num}/ai_rewrite")
async def ai_rewrite_chapter_segment(
    novel_id: str,
    chapter_num: int,
    req: "AIRewriteRequest" = None,
):
    """章节内 AI 改写工具

    Body: {
      "target_text": "用户选中的文本（必填）",
      "instruction": "改写要求，例如：'改得更简洁' / '把对话改得更紧张'",
      "context_before": "前文（可选）",
      "context_after": "后文（可选）"
    }
    Returns: {
      "rewritten": "改写后的文本",
      "changed": bool
    }
    """
    if req is None or not getattr(req, "target_text", "").strip():
        raise HTTPException(status_code=400, detail="请提供要改写的文本（target_text）")

    target_text = req.target_text
    instruction = (req.instruction or "").strip() or "改进这段文字的表达，使其更生动"
    ctx_before = (req.context_before or "").strip()[-500:]  # 限制长度
    ctx_after = (req.context_after or "").strip()[:500]

    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")

    from models import call_llm
    from prompts import CHAPTER_SEGMENT_REWRITE_SYSTEM, CHAPTER_SEGMENT_REWRITE_PROMPT

    prompt = CHAPTER_SEGMENT_REWRITE_PROMPT.format(
        instruction=instruction,
        context_before=ctx_before,
        target_text=target_text,
        context_after=ctx_after,
    )
    rewritten = call_llm(
        role="writer",
        system_prompt=CHAPTER_SEGMENT_REWRITE_SYSTEM,
        prompt=prompt,
        temperature=0.7,
        max_tokens=2048,
    )
    rewritten = (rewritten or "").strip()
    # 清理 thinking 块（防御性）
    from models import _strip_think_blocks
    rewritten = _strip_think_blocks(rewritten)
    # 清理 markdown 包裹
    if rewritten.startswith("```"):
        rewritten = "\n".join(rewritten.split("\n")[1:])
        if rewritten.endswith("```"):
            rewritten = "\n".join(rewritten.split("\n")[:-1])
        rewritten = rewritten.strip()

    return {
        "status": "success",
        "rewritten": rewritten,
        "original_length": len(target_text),
        "rewritten_length": len(rewritten),
        "changed": rewritten != target_text and len(rewritten) > 0,
    }


@app.post("/api/novels/{novel_id}/chapters/{chapter_num}/ai_continue")
async def ai_continue_chapter(
    novel_id: str,
    chapter_num: int,
    req: "AIContinueRequest" = None,
):
    """章节内 AI 续写工具

    Body: {
      "prefix_text": "已写的最后 N 字（默认 500）",
      "target_words": 目标续写字数（默认 500）
    }
    Returns: { "continued": "续写的文本", "word_count": N }
    """
    if req is None:
        raise HTTPException(status_code=400, detail="需要请求体")

    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")

    target_words = req.target_words or 500
    prefix_text = (req.prefix_text or "")[-1500:]

    from models import call_llm
    from prompts import CHAPTER_CONTINUE_SYSTEM, CHAPTER_CONTINUE_PROMPT

    prompt = CHAPTER_CONTINUE_PROMPT.format(
        target_words=target_words,
        prefix_text=prefix_text,
    )
    continued = call_llm(
        role="writer",
        system_prompt=CHAPTER_CONTINUE_SYSTEM,
        prompt=prompt,
        temperature=0.85,
        max_tokens=target_words * 3,
    )
    continued = (continued or "").strip()
    from models import _strip_think_blocks
    continued = _strip_think_blocks(continued)
    if continued.startswith("```"):
        continued = "\n".join(continued.split("\n")[1:])
        if continued.endswith("```"):
            continued = "\n".join(continued.split("\n")[:-1])
        continued = continued.strip()

    return {
        "status": "success",
        "continued": continued,
        "word_count": len(continued),
    }


# ═══════════════════════════════════════════════════════════════
# 章节反馈修订：整章反馈 → AI 修订 → 前后对比 → 采用/放弃
# ═══════════════════════════════════════════════════════════════

def compute_chapter_diff_html(old_text: str, new_text: str) -> dict:
    """计算两段文本的 diff，返回 HTML 高亮片段 + 统计信息。

    策略：先按段落（\n\n）切分，对每个段落分别做字符级 diff。
    - 相同段落：直接 equal span
    - 替换段落：内部做字符级 diff（增绿删红）
    - 增/删段落：整体高亮

    增：浅绿背景；删：浅红背景 + 删除线；相同：原色。
    返回 { "html_old": str, "html_new": str, "stats": {added, removed, unchanged} }
    """
    import html as _html
    import re

    def char_diff_html(a: str, b: str) -> tuple[str, str, int, int, int]:
        """对单段做字符级 diff，返回 (html_old, html_new, added, removed, unchanged)."""
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        o_parts, n_parts = [], []
        added = removed = unchanged = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            o = _html.escape(a[i1:i2])
            n = _html.escape(b[j1:j2])
            if tag == "equal":
                o_parts.append(f'<span class="diff-eq">{o}</span>')
                n_parts.append(f'<span class="diff-eq">{n}</span>')
                unchanged += i2 - i1
            elif tag == "replace":
                o_parts.append(f'<span class="diff-del">{o}</span>')
                n_parts.append(f'<span class="diff-add">{n}</span>')
                removed += i2 - i1
                added += j2 - j1
            elif tag == "delete":
                o_parts.append(f'<span class="diff-del">{o}</span>')
                removed += i2 - i1
            elif tag == "insert":
                n_parts.append(f'<span class="diff-add">{n}</span>')
                added += j2 - j1
        return "".join(o_parts), "".join(n_parts), added, removed, unchanged

    # 按段落切分（保留分隔符信息）
    def split_paragraphs(text: str) -> list[str]:
        # 用 \n\n 切分，空段也保留
        return [p for p in re.split(r'(\n\n+)', text) if p]

    old_paras = split_paragraphs(old_text)
    new_paras = split_paragraphs(new_text)

    # 段落级 SequenceMatcher
    sm = difflib.SequenceMatcher(None, old_paras, new_paras, autojunk=False)
    old_parts, new_parts = [], []
    total_added = total_removed = total_unchanged = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                p = old_paras[k]
                esc = _html.escape(p)
                old_parts.append(f'<span class="diff-eq">{esc}</span>')
                new_parts.append(f'<span class="diff-eq">{esc}</span>')
                total_unchanged += len(p)
        elif tag == "replace":
            # 逐对做字符级 diff（默认一对一）
            n = max(i2 - i1, j2 - j1)
            for k in range(n):
                old_seg = old_paras[i1 + k] if i1 + k < i2 else ""
                new_seg = new_paras[j1 + k] if j1 + k < j2 else ""
                if old_seg and new_seg:
                    o_html, n_html, ad, rm, un = char_diff_html(old_seg, new_seg)
                    old_parts.append(o_html)
                    new_parts.append(n_html)
                    total_added += ad
                    total_removed += rm
                    total_unchanged += un
                elif old_seg:
                    old_parts.append(f'<span class="diff-del">{_html.escape(old_seg)}</span>')
                    total_removed += len(old_seg)
                elif new_seg:
                    new_parts.append(f'<span class="diff-add">{_html.escape(new_seg)}</span>')
                    total_added += len(new_seg)
        elif tag == "delete":
            for k in range(i1, i2):
                p = old_paras[k]
                old_parts.append(f'<span class="diff-del">{_html.escape(p)}</span>')
                total_removed += len(p)
        elif tag == "insert":
            for k in range(j1, j2):
                p = new_paras[k]
                new_parts.append(f'<span class="diff-add">{_html.escape(p)}</span>')
                total_added += len(p)

    return {
        "html_old": "".join(old_parts),
        "html_new": "".join(new_parts),
        "stats": {
            "added": total_added,
            "removed": total_removed,
            "unchanged": total_unchanged,
            "old_length": len(old_text),
            "new_length": len(new_text),
        },
    }


@app.post("/api/novels/{novel_id}/chapters/{chapter_num}/revise_with_feedback")
async def revise_chapter_with_feedback(
    novel_id: str,
    chapter_num: int,
    req: ChapterReviseReq,
):
    """章节整章反馈修订（不直接保存，返回前后对比）。

    Body: { "feedback": "用户的整章反馈意见" }
    Returns: {
      "old_content": 原文,
      "new_content": 修订后,
      "diff": { html_old, html_new, stats }
    }
    """
    if req is None or not (req.feedback or "").strip():
        raise HTTPException(status_code=400, detail="请提供反馈意见（feedback 不能为空）")

    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")

    # P2 修复：优先从文件系统读（与 jumpToChapter / /api/chapter/{N} 一致），
    # state["chapter_content"] 兜底（兼容 state-only 的作品，如未存档章节）
    # 原因：state["chapter_content"] 可能缓存的是"上次生成的章节"（不是用户当前要看的那章），
    # 导致反馈修订改错了章节。文件系统才是真值。
    old_content = ""
    try:
        old_content = (read_chapter_from_state(state, chapter_num) or "").strip()
    except Exception:
        pass
    if not old_content:
        old_content = (state.get("chapter_content") or "").strip()
    if not old_content:
        old_content = (state.get(f"chapter_{chapter_num}_content") or "").strip()
    if not old_content:
        raise HTTPException(status_code=404, detail=f"第 {chapter_num} 章暂无内容，请先生成章节")

    feedback = req.feedback.strip()
    if len(feedback) > 1500:
        feedback = feedback[:1500] + "…"

    from models import call_llm, _strip_think_blocks
    from prompts import CHAPTER_REVISE_WITH_FEEDBACK_SYSTEM, CHAPTER_REVISE_WITH_FEEDBACK_PROMPT

    prompt = CHAPTER_REVISE_WITH_FEEDBACK_PROMPT.format(
        old_length=len(old_content),
        old_content=old_content[:6000],  # 限制 6000 字防止超出 token
        feedback=feedback,
    )
    new_content = call_llm(
        role="editor",
        system_prompt=CHAPTER_REVISE_WITH_FEEDBACK_SYSTEM,
        prompt=prompt,
        temperature=0.55,
        max_tokens=6000,
    )
    new_content = (new_content or "").strip()
    new_content = _strip_think_blocks(new_content)
    # 清理 markdown 包裹
    if new_content.startswith("```"):
        new_content = "\n".join(new_content.split("\n")[1:])
        if new_content.endswith("```"):
            new_content = "\n".join(new_content.split("\n")[:-1])
        new_content = new_content.strip()

    # P2 增强：长度校验（防 AI 把整章缩成局部）。偏差 >30% 自动拒绝
    # 规则：prompt 要求"字数偏差 ±20%"，超过 30% 视为违规（容忍 10% 余量）
    old_len = len(old_content)
    new_len = len(new_content)
    if old_len > 500 and new_len > 0:
        shrink_ratio = (old_len - new_len) / old_len
        if shrink_ratio > 0.30:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"修订后字数 {new_len} 相比原文 {old_len} 缩水 {shrink_ratio*100:.0f}%，"
                    f"超过 30% 阈值。AI 删除了与反馈无关的内容。"
                    f"请换更具体的反馈（如'只改第 3 段那一处心理描写'），"
                    f"或重新生成。"
                ),
            )
        grow_ratio = (new_len - old_len) / old_len
        if grow_ratio > 0.50:
            # 扩写超过 50% 也算违规（虽然概率小）
            raise HTTPException(
                status_code=422,
                detail=(
                    f"修订后字数 {new_len} 相比原文 {old_len} 扩写 {grow_ratio*100:.0f}%，"
                    f"超过 50% 阈值。请换更具体的反馈。"
                ),
            )
    # 清理常见元话语前缀
    for prefix in ["以下是修改后的版本：", "以下是修订后的章节：", "修订后：", "修改后："]:
        if new_content.startswith(prefix):
            new_content = new_content[len(prefix):].lstrip()
            break

    if not new_content:
        raise HTTPException(status_code=503, detail="AI 未返回任何结果")

    # 计算 diff
    diff = compute_chapter_diff_html(old_content, new_content)

    return {
        "status": "success",
        "old_content": old_content,
        "new_content": new_content,
        "diff": diff,
        "feedback_echo": feedback,
    }


@app.post("/api/novels/{novel_id}/chapters/{chapter_num}/apply_revision")
async def apply_chapter_revision(
    novel_id: str,
    chapter_num: int,
    req: ChapterApplyRevisionReq,
):
    """采用 AI 修订后的新版本（覆盖保存到 state + checkpoint + 章节文件）。"""
    if req is None or not (req.new_content or "").strip():
        raise HTTPException(status_code=400, detail="new_content 不能为空")

    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")

    new_content = req.new_content.strip()
    state["chapter_content"] = new_content

    # 同步保存到章节文件
    try:
        checkpoint_path = state.get("_checkpoint_path")
        if checkpoint_path and os.path.exists(os.path.dirname(checkpoint_path)):
            state.pop("_novel_dir", None)
            write_json_file(checkpoint_path, state)
    except Exception as e:
        print(f"[apply_revision] 写 checkpoint 失败: {e}")

    # 同步到章节文件：复用 find_chapter_file 找到的现有路径（避免写错文件名）
    try:
        # P2 修复：必须复用现有文件路径（"第N章.txt"），不能写 f"{N:03d}.txt" 这种新文件
        # 否则会创建新文件，而 find_chapter_file 仍读旧文件
        existing_path = find_chapter_file(state, chapter_num)
        if existing_path:
            with open(existing_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        else:
            # 兜底：现有文件不存在，用标准格式创建一个
            novel_title = state.get("novel_title", "")
            if novel_title:
                novel_dir = get_novel_dir(novel_title, novel_id)
                ch_dir = os.path.join(novel_dir, "chapters")
                os.makedirs(ch_dir, exist_ok=True)
                ch_path = os.path.join(ch_dir, chapter_file_name(chapter_num))
                with open(ch_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
    except Exception as e:
        print(f"[apply_revision] 写章节文件失败: {e}")

    return {
        "status": "success",
        "chapter_num": chapter_num,
        "new_length": len(new_content),
        "message": "已采用修订版本",
    }


# ═══════════════════════════════════════════════════════════════
# 作品简介（合规检查 + AI 生成 + 编辑）
# ═══════════════════════════════════════════════════════════════

class SynopsisReq(BaseModel):
    """更新或生成作品简介"""
    synopsis: str = ""


@app.get("/api/novels/{novel_id}/synopsis")
async def get_novel_synopsis(novel_id: str):
    """拿作品简介（无则返回空字符串）。"""
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    return {
        "status": "success",
        "novel_id": novel_id,
        "synopsis": state.get("novel_synopsis", "") or "",
        "updated_at": state.get("novel_synopsis_updated_at", 0.0),
    }


@app.put("/api/novels/{novel_id}/synopsis")
async def update_novel_synopsis(novel_id: str, req: SynopsisReq):
    """更新作品简介（保存 + 合规检查）。"""
    from agents.editor import detect_synopsis_compliance_risks
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    new_text = (req.synopsis or "").strip()
    if len(new_text) > 1000:
        raise HTTPException(status_code=400, detail="简介不能超过 1000 字")
    state["novel_synopsis"] = new_text
    state["novel_synopsis_updated_at"] = time.time()
    # 保存
    novel_title = state.get("novel_title", "")
    save_checkpoint(state, novel_title, novel_id)
    # 合规检查
    risks = detect_synopsis_compliance_risks(new_text)
    return {
        "status": "success",
        "synopsis": new_text,
        "updated_at": state["novel_synopsis_updated_at"],
        "risks": risks,
        "passed": not any(r.get("severity") == "must_fix" for r in risks),
    }


@app.post("/api/novels/{novel_id}/synopsis/check")
async def check_novel_synopsis(novel_id: str, req: SynopsisReq):
    """合规检查简介（不保存）。"""
    from agents.editor import detect_synopsis_compliance_risks
    text = (req.synopsis or "").strip()
    risks = detect_synopsis_compliance_risks(text)
    return {
        "status": "success",
        "risks": risks,
        "passed": not any(r.get("severity") == "must_fix" for r in risks),
        "risk_count": {
            "must_fix": sum(1 for r in risks if r.get("severity") == "must_fix"),
            "warning": sum(1 for r in risks if r.get("severity") == "warning"),
        },
    }


@app.post("/api/novels/{novel_id}/synopsis/generate")
async def generate_novel_synopsis(novel_id: str):
    """AI 自动生成作品简介（基于 full_outline / global_synopsis / concept_seed）。"""
    from models import call_llm, _strip_think_blocks
    from prompts import NOVEL_SYNOPSIS_GENERATE_PROMPT

    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")
    novel_title = state.get("novel_title", "")
    full_outline = state.get("global_outline", "") or state.get("full_outline", "")
    global_synopsis = state.get("global_synopsis", "") or ""
    concept_refinement = state.get("concept_refinement", {}) or {}
    setting = state.get("novel_setting", "")
    if not full_outline and not global_synopsis and not concept_refinement:
        raise HTTPException(
            status_code=400,
            detail="作品暂无大纲/概要，无法生成简介。请先在创作前生成大纲。"
        )

    # 拼输入
    logline = concept_refinement.get("logline", "") if isinstance(concept_refinement, dict) else ""
    prompt = NOVEL_SYNOPSIS_GENERATE_PROMPT.format(
        novel_title=novel_title,
        genre=state.get("novel_genre", ""),
        logline=logline,
        full_outline=full_outline[:3000],
        global_synopsis=global_synopsis[:1500],
        setting=setting[:600],
    )
    try:
        raw = call_llm(
            role="editor",
            system_prompt="你是一位专业的网文运营编辑，擅长用一句话/一段话精准概括作品卖点。",
            prompt=prompt,
            temperature=0.65,
            max_tokens=600,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"AI 生成失败：{e}")
    raw = _strip_think_blocks(raw or "").strip()
    # 清理 markdown
    for prefix in ["【简介】", "简介：", "简介:", "**", "##", "###"]:
        if raw.startswith(prefix):
            raw = raw[len(prefix):].lstrip()
    if raw.endswith("**"):
        raw = raw[:-2].rstrip()
    if len(raw) > 500:
        raw = raw[:500].rstrip() + "…"

    # 立即保存 + 合规检查
    from agents.editor import detect_synopsis_compliance_risks
    state["novel_synopsis"] = raw
    state["novel_synopsis_updated_at"] = time.time()
    save_checkpoint(state, novel_title, novel_id)
    risks = detect_synopsis_compliance_risks(raw)

    return {
        "status": "success",
        "synopsis": raw,
        "risks": risks,
        "passed": not any(r.get("severity") == "must_fix" for r in risks),
        "updated_at": state["novel_synopsis_updated_at"],
    }


@app.post("/api/novels/{novel_id}/archive")
async def archive_novel(novel_id: str):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")

    checkpoint_path = state.get("_checkpoint_path")
    state["archived"] = True
    state["updated_at"] = time.time()
    state.pop("_checkpoint_path", None)
    state.pop("_novel_dir", None)
    write_json_file(checkpoint_path, state)
    clear_current_novel_id(novel_id)
    # P0 2 缓存：归档后让 novels/status/stats 失效
    ttl_cache.invalidate("novels")
    ttl_cache.invalidate("status")
    ttl_cache.invalidate_prefix("chapters_stats")
    return {"status": "success", "message": "作品已归档", "novel_id": novel_id}

@app.post("/api/novels/{novel_id}/delete")
@app.delete("/api/novels/{novel_id}")
async def delete_novel(novel_id: str):
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")

    novel_dir = state.get("_novel_dir")
    checkpoint_path = state.get("_checkpoint_path")
    if not novel_dir and checkpoint_path:
        novel_dir = os.path.dirname(checkpoint_path)
    if not novel_dir:
        raise HTTPException(status_code=400, detail="作品目录不可用")

    # 安全校验：作品目录必须在 NOVELS_DIR 树内（允许顶层 + user_X 子目录两种结构）
    abs_root = os.path.abspath(NOVELS_DIR)
    abs_novel_dir = os.path.abspath(novel_dir)
    if abs_novel_dir == abs_root or not abs_novel_dir.startswith(abs_root + os.sep):
        raise HTTPException(status_code=400, detail="作品目录校验失败，已阻止删除")

    try:
        import requests
        requests.post(f"{MCP_SERVER_URL}/clear_database", params={"novel_id": novel_id}, timeout=2)
    except Exception as e:
        print(f"删除作品时清理记忆库失败，继续删除文件：{e}")
    clear_current_novel_id(novel_id)
    if os.path.exists(abs_novel_dir):
        shutil.rmtree(abs_novel_dir)
    # P0 2 缓存：删除后让 novels/status/stats 失效
    ttl_cache.invalidate("novels")
    ttl_cache.invalidate("status")
    ttl_cache.invalidate_prefix("chapters_stats")
    return {"status": "success", "message": "作品已彻底删除", "novel_id": novel_id}


def seed_refined_concept_assets(
    bible: StoryBible,
    refinement: dict[str, Any] | None,
    selected_concept: dict[str, Any] | None,
    concept_seed: str,
    progress: Callable[[str], None],
):
    """Persist refined concept assets into the new novel's memory space."""
    if not refinement:
        return

    progress("正在写入立项包：角色卡、势力卡与世界规则...", "memory")
    if concept_seed:
        bible.init_from_setting(f"【原始一句话灵感】\n{concept_seed}")
    if selected_concept:
        bible.init_from_setting(
            "【已选立项方向】\n"
            + json.dumps(selected_concept, ensure_ascii=False, indent=2)
        )

    lore_blocks = [
        ("【读者简介】", refinement.get("blurb", "")),
        ("【长期主线】", refinement.get("long_term_arc", "")),
        ("【前三章开局策略】", refinement.get("opening_strategy", "")),
    ]
    for title, content in lore_blocks:
        if content:
            bible.init_from_setting(f"{title}\n{content}")

    for role in refinement.get("main_cast", []) or []:
        name = str(role.get("name", "")).strip()
        if not name:
            continue
        bible.save_entity_card(
            "character",
            name,
            {
                "定位": role.get("role", ""),
                "性格": role.get("traits", ""),
                "当前状态": role.get("current_status", ""),
                "目标": role.get("goal", ""),
                "与主角关系": role.get("relationship_to_protagonist", ""),
            },
            "由智能立项包自动创建",
        )

    for faction in refinement.get("key_factions", []) or []:
        name = str(faction.get("name", "")).strip()
        if not name:
            continue
        bible.save_entity_card(
            "faction",
            name,
            {
                "立场": faction.get("position", ""),
                "资源": faction.get("resources", ""),
                "冲突": faction.get("conflict", ""),
            },
            "由智能立项包自动创建",
        )

    for rule in refinement.get("world_rules", []) or []:
        category = str(rule.get("category", "世界规则")).strip() or "世界规则"
        rule_text = str(rule.get("rule", "")).strip()
        if rule_text:
            bible.add_world_rule(category, rule_text)

    opening_strategy = refinement.get("opening_strategy", "")
    if opening_strategy:
        bible.add_plot_hook(f"前三章开局策略：{opening_strategy}", 1)

    for idx, risk in enumerate(refinement.get("risks_to_avoid", []) or [], 1):
        if risk:
            bible.add_world_rule("写作避坑", f"{idx}. {risk}")

    # P2 增强：把 chapter_constraints 写入 bible（让 AI 写每章时 RAG 检索能拿到本章目标）
    chapter_constraints = refinement.get("chapter_constraints", []) or []
    if chapter_constraints:
        # 用 lore 存"章节蓝图总览"（按章节分块，方便按 chapter_num 检索）
        progress(f"正在写入 {len(chapter_constraints)} 章章节约束清单...", "memory")
        for cc in chapter_constraints:
            num = cc.get("chapter_num", "")
            purpose = cc.get("purpose", "")
            core_event = cc.get("core_event", "")
            required_chars = "、".join(cc.get("required_characters", []) or [])
            required_sets = "、".join(cc.get("required_settings", []) or [])
            ending_hook = cc.get("ending_hook", "")
            avoid = cc.get("avoid", "")
            block = (
                f"【第{num}章约束】\n"
                f"作用：{purpose}\n"
                f"核心事件：{core_event}\n"
                f"必须出现角色：{required_chars}\n"
                f"必须引用设定：{required_sets}\n"
                f"章末钩子：{ending_hook}\n"
                f"避免：{avoid}"
            )
            try:
                num_int = int(num) if str(num).isdigit() else 0
            except (TypeError, ValueError):
                num_int = 0
            bible.init_from_setting(block, chapter_num=num_int)


def format_opening_outline_text(opening_outline: dict[str, Any] | None) -> str:
    if not opening_outline:
        return ""
    lines = []
    if opening_outline.get("opening_arc"):
        lines.append(f"【前10章整体弧线】\n{opening_outline.get('opening_arc')}")
    chapters = opening_outline.get("chapters", []) or []
    for item in chapters:
        chapter_num = item.get("chapter_num", "")
        title = item.get("title", "")
        required_characters = "、".join(item.get("required_characters", []) or [])
        required_settings = "、".join(item.get("required_settings", []) or [])
        lines.append(
            f"【第{chapter_num}章：{title}】\n"
            f"作用：{item.get('purpose', '')}\n"
            f"核心事件：{item.get('core_event', '')}\n"
            f"冲突：{item.get('conflict', '')}\n"
            f"爽点/期待：{item.get('reader_hook', '')}\n"
            f"必须出现角色：{required_characters}\n"
            f"必须引用设定：{required_settings}\n"
            f"章末钩子：{item.get('ending_hook', '')}\n"
            f"避免：{item.get('avoid', '')}"
        )
    return "\n\n".join(lines).strip()


def format_full_outline_text(full_outline: dict[str, Any] | None) -> str:
    if not full_outline:
        return ""
    lines = []
    if full_outline.get("overall_arc"):
        lines.append(f"【全书整体弧线】\n{full_outline.get('overall_arc')}")

    acts = full_outline.get("acts", []) or []
    if acts:
        act_lines = ["【分幕结构】"]
        for act in acts:
            if not isinstance(act, dict):
                continue
            act_lines.append(
                f"- {act.get('name', '阶段')}（{act.get('chapter_range', '')}）："
                f"{act.get('purpose', '')}；阶段转折：{act.get('turning_point', '')}"
            )
        lines.append("\n".join(act_lines))

    chapters = full_outline.get("chapters", []) or []
    for item in chapters:
        if not isinstance(item, dict):
            continue
        chapter_num = item.get("chapter_num", "")
        title = item.get("title", "")
        lines.append(
            f"【第{chapter_num}章：{title}】\n"
            f"剧情功能：{item.get('purpose', '')}\n"
            f"核心事件：{item.get('main_event', '')}\n"
            f"主要冲突：{item.get('conflict', '')}\n"
            f"读者期待：{item.get('reader_hook', '')}\n"
            f"衔接要求：{item.get('continuity_note', '')}"
        )
    return "\n\n".join(lines).strip()


def seed_full_outline_assets(
    bible: StoryBible,
    full_outline: dict[str, Any] | None,
    progress: Callable[[str], None],
) -> str:
    outline_text = format_full_outline_text(full_outline)
    if not outline_text:
        return ""
    progress("正在写入全书章节蓝图...", "memory")
    bible.init_from_setting(outline_text)
    for item in full_outline.get("chapters", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            chapter_num = int(item.get("chapter_num", 0))
        except (TypeError, ValueError):
            chapter_num = 0
        if chapter_num <= 0:
            continue
        hook_text = (
            f"全书章节蓝图约束｜第{chapter_num}章《{item.get('title', '')}》："
            f"剧情功能={item.get('purpose', '')}；"
            f"核心事件={item.get('main_event', '')}；"
            f"主要冲突={item.get('conflict', '')}；"
            f"读者期待={item.get('reader_hook', '')}；"
            f"衔接要求={item.get('continuity_note', '')}"
        )
        bible.add_plot_hook(hook_text, chapter_num)
    return outline_text


def seed_opening_outline_assets(
    bible: StoryBible,
    opening_outline: dict[str, Any] | None,
    progress: Callable[[str], None],
) -> str:
    outline_text = format_opening_outline_text(opening_outline)
    if not outline_text:
        return ""
    progress("正在写入前 10 章开局规划...", "memory")
    bible.init_from_setting(outline_text)
    for item in opening_outline.get("chapters", []) or []:
        try:
            chapter_num = int(item.get("chapter_num", 0))
        except (TypeError, ValueError):
            chapter_num = 0
        if chapter_num <= 0:
            continue
        hook_text = (
            f"前10章规划约束｜第{chapter_num}章《{item.get('title', '')}》："
            f"核心事件={item.get('core_event', '')}；"
            f"冲突={item.get('conflict', '')}；"
            f"爽点={item.get('reader_hook', '')}；"
            f"章末钩子={item.get('ending_hook', '')}；"
            f"必须角色={', '.join(item.get('required_characters', []) or [])}；"
            f"必须设定={', '.join(item.get('required_settings', []) or [])}；"
            f"避免={item.get('avoid', '')}"
        )
        bible.add_plot_hook(hook_text, chapter_num)
    return outline_text


def run_init_novel(req: InitRequest, progress: Callable[[str], None]) -> dict:
    """初始化全新小说，生成大纲并保存初始状态"""
    novel_id = uuid.uuid4().hex
    # 让 UsageTracker 把这次 init 的 LLM 调用都归到这本新书
    try:
        model_layer.CURRENT_NOVEL_ID.set(novel_id)
    except Exception:
        pass
    fallback_seed = (req.concept_seed or "").strip()
    novel_title = (req.title or "").strip() or (fallback_seed[:18] if fallback_seed else "未命名作品")
    novel_setting = (req.setting or "").strip() or (
        f"一句话灵感：{fallback_seed}\n请自动扩展题材方向、主角设定、核心冲突、世界规则和开局剧情。"
        if fallback_seed else "请自动生成一部适合连载的长篇网文设定。"
    )
    current_story_bible = StoryBible(novel_id)

    progress("正在创建作品空间...", "running")
    novel_dir = get_novel_dir(novel_title, novel_id)
    chapters_dir = os.path.join(novel_dir, "chapters")
    
    if os.path.exists(chapters_dir):
        import shutil
        shutil.rmtree(chapters_dir)
    os.makedirs(chapters_dir, exist_ok=True)
    
    progress("正在初始化故事宝典...", "memory")
    current_story_bible.clear_database()
    current_story_bible.init_from_setting(novel_setting)
    seed_refined_concept_assets(current_story_bible, req.concept_refinement, req.selected_concept, req.concept_seed, progress)
    full_outline_text = seed_full_outline_assets(current_story_bible, req.full_outline, progress)
    opening_outline_text = seed_opening_outline_assets(current_story_bible, req.opening_outline, progress)

    # P2 增强：从 concept_refinement 提取 chapter_constraints（每章目标清单）
    chapter_constraints = []
    if isinstance(req.concept_refinement, dict):
        chapter_constraints = req.concept_refinement.get("chapter_constraints", []) or []

    initial_state = {
        "user_id": get_active_user_id(),
        "novel_id": novel_id,
        "novel_title": novel_title,
        "novel_genre": req.genre,
        "novel_style": req.style,
        "style_preset_key": req.style_preset_key or "",  # 风格 preset key（章节生成时用）
        "novel_theme_intent": (req.theme_intent or "").strip(),  # 作品核心主题意图（每章 AI 必读）
        "novel_setting": novel_setting,
        "concept_seed": req.concept_seed,
        "selected_concept": req.selected_concept or {},
        "concept_refinement": req.concept_refinement or {},
        "chapter_constraints": chapter_constraints,  # P2 增强：每章目标清单（已写入 bible RAG）
        "full_outline": req.full_outline or {},
        "full_outline_text": full_outline_text,
        "opening_outline": req.opening_outline or {},
        "opening_outline_text": opening_outline_text,
        "num_chapters": req.num_chapters,
        "chapter_target_words": req.chapter_target_words,
        "current_chapter": 1,
        "global_outline": full_outline_text,
        "chapter_outline": "",
        "chapter_pattern_card": "",
        "character_voice_guide": "",
        "reader_promise_guide": "",
        "chapter_drama_card": "",
        "chapter_content": "",
        "bible_context": "",
        "edit_required": "",
        "edit_suggestions": "",
        "quality_report": {},
        "quality_enhanced_once": False,
        "story_so_far": "目前是故事开场。",
        "global_synopsis": "",
        "recent_summaries": [],
        "chapter_control_history": [],
        "current_chapter_controls": {},
        "chapter_controls_text": "",
        "is_approved": False,
        "edit_count": 0,
        "completed_chapters": [],
    }

    progress("正在生成全书大纲与第一章...", "outline")
    full_pipeline = build_full_pipeline()
    result = full_pipeline.invoke(initial_state)
    result["chapter_content"] = finalize_generated_content_before_save(result, 1, result.get("chapter_content", ""))
    
    progress("正在保存章节与存档...", "memory")
    save_chapter(1, result["chapter_content"], novel_title, novel_id)
    StoryBible(novel_id).add_chapter_version(1, "ai_final", result["chapter_content"], "初始化生成的第一章")
    save_checkpoint(result, novel_title, novel_id)
    set_current_novel_id(novel_id)
    
    return {
        "status": "success", 
        "message": "大纲及第一章生成完毕",
        "content": result["chapter_content"],
        "word_count": len(result["chapter_content"]),
        "current_chapter": 1,
        "novel_id": novel_id,
        "quality_report": result.get("quality_report", {}),
    }

@app.post("/api/init")
async def init_novel(req: InitRequest):
    task_id = start_background_task("init_novel", lambda progress: run_init_novel(req, progress))
    return {"status": "queued", "task_id": task_id, "message": "初始化任务已提交"}

def run_generate_next_chapter(progress: Callable[[str], None], controls: ChapterControlReq | None = None) -> dict:
    """生成下一章"""
    progress("正在读取当前作品存档...", "running")
    state = load_checkpoint()
    if not state:
        raise RuntimeError("未找到存档状态，请先初始化小说")

    novel_title = state.get("novel_title", "")
    novel_id = get_state_novel_id(state)
    # 让 UsageTracker 把这次 run 的 LLM 调用都归到这本书
    try:
        model_layer.CURRENT_NOVEL_ID.set(novel_id)
    except Exception:
        pass
    current_chapter = state.get("current_chapter", 1)
    # Graph 里的 summarize_node 会将 next chapter_num 保存进状态里。
    # 我们验证看是否已经到了 num_chapters
    if current_chapter > state.get("num_chapters", 100):
         return {"status": "finished", "message": "已达到目标章节数"}

    control_data = normalize_chapter_controls(controls)
    controls_text = format_chapter_controls_text(control_data, current_chapter)
    control_history = list(state.get("chapter_control_history", []))
    if control_data:
        control_history.append({
            "chapter_num": current_chapter,
            "controls": control_data,
            "created_at": time.time(),
        })
        control_history = control_history[-80:]
        progress("已读取本章写作控制项", "planning")

    chapter_graph = build_chapter_graph()
    
    chapter_state = {
        **state,
        "chapter_outline": "",
        "chapter_pattern_card": "",
        "character_voice_guide": "",
        "reader_promise_guide": "",
        "chapter_drama_card": "",
        "chapter_content": "",
        "edit_required": "",
        "edit_suggestions": "",
        "quality_report": {},
        "quality_enhanced_once": False,
        "current_chapter_controls": control_data,
        "chapter_controls_text": controls_text,
        "chapter_control_history": control_history,
        "is_approved": False,
        "edit_count": 0,
    }
    if control_data.get("target_words"):
        chapter_state["chapter_target_words"] = int(control_data["target_words"])
    
    progress(f"正在规划、写作并审核第 {current_chapter} 章...", "planning")
    result = chapter_graph.invoke(chapter_state)
    
    saved_ch = chapter_state["current_chapter"]
    result["chapter_content"] = finalize_generated_content_before_save(result, saved_ch, result.get("chapter_content", ""))
    progress(f"正在保存第 {saved_ch} 章...", "memory")
    save_chapter(saved_ch, result["chapter_content"], novel_title, novel_id)
    StoryBible(novel_id).add_chapter_version(saved_ch, "ai_final", result["chapter_content"], "AI 生成并通过审核后的版本")
    save_checkpoint(result, novel_title, novel_id)

    # ── 自动备份触发（每 N 章一次，默认 10）──
    try:
        if backup.should_auto_backup(saved_ch):
            progress(f"已生成 {saved_ch} 章，触发自动备份...", "backup")
            backup_path = backup.backup_database(label=f"auto_ch{saved_ch}")
            progress(f"已自动备份数据库: {os.path.basename(backup_path)}", "backup")
            backup.cleanup_old_backups()
    except Exception as e:
        # 备份失败不应阻塞主流程
        print(f"  [警告] 自动备份失败: {e}")

    return {
        "status": "success",
        "message": f"第 {saved_ch} 章生成完毕",
        "chapter_num": saved_ch,
        "content": result["chapter_content"],
        "word_count": len(result["chapter_content"]),
        "quality_report": result.get("quality_report", {}),
    }


def run_batch_generate_chapters(
    progress: Callable[[str], None],
    req: BatchGenerateReq,
) -> dict:
    """批量续写：连续生成 N 章。

    流程：
    1. 读取当前存档
    2. 循环 N 次调用 run_generate_next_chapter
    3. 每章成功后写 checkpoint（run_generate_next_chapter 内部已做）
    4. 记录每章结果到 chapters 列表
    5. 出错时按 stop_on_error 决定是否继续
    """
    # 整个 batch 期间都让 UsageTracker 归到这本书
    state = load_checkpoint()
    if state:
        try:
            model_layer.CURRENT_NOVEL_ID.set(get_state_novel_id(state))
        except Exception:
            pass

    progress(f"开始批量续写：{req.count} 章...", "running")
    state = load_checkpoint()
    if not state:
        raise RuntimeError("未找到存档状态，请先初始化小说")

    # ── 并发控制：当前架构下强制串行 ──
    if req.concurrency > 1:
        progress(
            f"[警告] concurrency={req.concurrency} > 1 当前架构下不安全（state + StoryBible 共享）"
            f"，强制降到 1（串行）。后续需重构 StoryBible 加锁 + state 隔离。",
            "warning"
        )
        actual_concurrency = 1
    else:
        actual_concurrency = 1

    total_chapters = state.get("num_chapters", 100)
    start_ch = req.start_chapter or state.get("current_chapter", 1)
    # 边界保护
    if start_ch < 1:
        start_ch = 1
    if start_ch > total_chapters:
        return {
            "status": "finished",
            "message": f"已达到目标章节数 ({total_chapters})",
            "total_requested": 0,
            "total_generated": 0,
            "chapters": [],
        }

    # 不允许超过 num_chapters
    count = min(req.count, total_chapters - start_ch + 1)
    if count <= 0:
        return {
            "status": "finished",
            "message": f"从第 {start_ch} 章开始已无剩余可生成章节",
            "total_requested": req.count,
            "total_generated": 0,
            "chapters": [],
        }

    progress(f"将生成第 {start_ch} - {start_ch + count - 1} 章（共 {count} 章）", "running")

    chapters_result: list[dict] = []
    failed_at: int | None = None

    for i in range(count):
        ch = start_ch + i
        # phase 字段 = f"chapter_{i+1}_of_{count}"，前端用来解析"已完成 N/总数"
        chapter_phase = f"chapter_{i+1}_of_{count}"
        progress(
            f"[{i+1}/{count}] 开始生成第 {ch} 章...",
            chapter_phase
        )
        # ── 单章自动重试 ──
        last_error: Exception | None = None
        success_inner: dict | None = None
        for attempt in range(req.max_retries_per_chapter + 1):
            try:
                # 每次都重新读取最新 checkpoint（防止状态漂移）
                inner = run_generate_next_chapter(
                    lambda msg, phase="running": progress(f"  [第 {ch} 章] {msg}", phase),
                    req.controls_per_chapter,
                )
                success_inner = inner
                if attempt > 0:
                    progress(
                        f"  [第 {ch} 章] 第 {attempt} 次重试成功",
                        "running"
                    )
                break
            except Exception as e:
                last_error = e
                if attempt < req.max_retries_per_chapter:
                    progress(
                        f"  [第 {ch} 章] 第 {attempt+1} 次失败: {e}，"
                        f"{req.retry_delay_seconds} 秒后重试...",
                        "error"
                    )
                    time.sleep(req.retry_delay_seconds)
                else:
                    progress(
                        f"  [第 {ch} 章] 已重试 {attempt} 次仍失败: {e}",
                        "error"
                    )

        if success_inner is not None:
            chapters_result.append({
                "chapter_num": success_inner.get("chapter_num", ch),
                "word_count": success_inner.get("word_count", 0),
                "status": "success",
                "message": success_inner.get("message", ""),
            })
            # 标记"已完成 i+1 章"（用 done_<i+1>_<count> 区分"开始"和"完成"）
            progress(
                f"[{i+1}/{count}] 第 {ch} 章完成（{success_inner.get('word_count', 0)} 字）",
                f"done_{i+1}_of_{count}"
            )
        else:
            error_msg = f"第 {ch} 章生成失败（已重试 {req.max_retries_per_chapter} 次）: {last_error}"
            chapters_result.append({
                "chapter_num": ch,
                "status": "error",
                "message": error_msg,
                "error": str(last_error),
            })
            failed_at = ch
            progress(error_msg, "error")
            if req.stop_on_error:
                progress(f"遇错即停，已生成 {i} 章", "error")
                break
            else:
                progress(f"跳过第 {ch} 章，继续下一章", "running")
                continue

    success_count = sum(1 for c in chapters_result if c["status"] == "success")
    if success_count == 0:
        status = "error"
        message = f"批量续写全部失败（{len(chapters_result)} 章）"
    elif failed_at is not None:
        status = "partial"
        message = f"批量续写部分成功：{success_count}/{len(chapters_result)} 章"
    else:
        status = "success"
        message = f"批量续写完成：{success_count}/{len(chapters_result)} 章"

    # 任务结束后做一次最终备份（如果至少生成了 1 章）
    if success_count > 0 and req.auto_backup_every > 0:
        try:
            last_ch = max(
                (c["chapter_num"] for c in chapters_result if c["status"] == "success"),
                default=start_ch,
            )
            backup_path = backup.backup_database(label=f"batch_end_ch{last_ch}")
            progress(f"批量任务结束，已备份数据库: {os.path.basename(backup_path)}", "backup")
            backup.cleanup_old_backups()
        except Exception as e:
            print(f"  [警告] 批量结束备份失败: {e}")

    return {
        "status": status,
        "message": message,
        "total_requested": count,
        "total_generated": success_count,
        "chapters": chapters_result,
        "failed_at": failed_at,
        "concurrency_requested": req.concurrency,
        "concurrency_actual": actual_concurrency,
        "concurrency_note": (
            "当前架构下强制串行（concurrency=1）。"
            "如需真正并发，需要重构 StoryBible 加锁 + state 隔离。"
            if req.concurrency > 1 else None
        ),
    }

@app.post("/api/generate_chapter")
async def generate_next_chapter(req: GenerateChapterReq | None = None):
    controls = req.controls if req else None
    task_id = start_background_task("generate_chapter", lambda progress: run_generate_next_chapter(progress, controls))
    return {"status": "queued", "task_id": task_id, "message": "章节生成任务已提交"}


@app.post("/api/novels/{novel_id}/batch_generate")
async def batch_generate_chapters(novel_id: str, req: BatchGenerateReq):
    """批量续写 N 章。

    Body: BatchGenerateReq
    - start_chapter: 起始章节（不填则从当前进度）
    - count: 章节数
    - controls_per_chapter: 每章控制项
    - stop_on_error: 遇错是否停止
    - auto_backup_every: 每 N 章自动备份

    Returns: {"status": "queued", "task_id": "..."}
    任务进度通过 GET /api/tasks/{task_id} 查询。
    """
    # 边界保护
    if req.count <= 0 or req.count > 100:
        raise HTTPException(status_code=400, detail="count 必须在 1-100 之间")
    task_id = start_background_task(
        "batch_generate",
        lambda progress: run_batch_generate_chapters(progress, req),
    )
    return {
        "status": "queued",
        "task_id": task_id,
        "message": f"批量续写任务已提交：{req.count} 章",
    }


@app.post("/api/admin/backup")
async def manual_backup(req: BackupReq | None = None):
    """手动触发数据库（+ novels）备份。"""
    try:
        label = (req.label if req else "") or "manual"
        include_novels = (req.include_novels if req else True)
        if include_novels:
            result = backup.backup_all(label=label)
        else:
            db_path = backup.backup_database(label=label)
            result = {"db": db_path, "novels": None}
        cleanup_deleted = backup.cleanup_old_backups()
        return {
            "status": "success",
            "message": f"备份完成（清理了 {len(cleanup_deleted)} 份旧备份）",
            "backup": result,
            "cleanup_deleted": cleanup_deleted,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/backup")
async def list_backups_endpoint():
    """列出所有备份 + 统计信息。"""
    try:
        backups = backup.list_backups()
        stats = backup.get_backup_stats()
        return {
            "status": "success",
            "backups": backups[:50],  # 最多返回 50 条
            "total": len(backups),
            "stats": stats,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RestoreReq(BaseModel):
    backup_path: str


@app.post("/api/admin/backup/restore")
async def restore_backup_endpoint(req: RestoreReq):
    """从指定备份恢复数据库。"""
    try:
        # 恢复前自动做一份 pre-restore 备份
        restored_to = backup.restore_database(req.backup_path)
        return {
            "status": "success",
            "message": f"已恢复到: {restored_to}",
            "restored_path": restored_to,
            "source_backup": req.backup_path,
            "warning": "MCP Server 需要重启才能加载新数据库",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/usage")
async def get_usage_endpoint(novel_id: str = "", all_books: bool = False):
    """获取 token 消耗统计。

    - 默认（无 novel_id）: 返回**当前激活作品**的累计（跨重启不丢）
    - novel_id="<id>": 返回指定作品的累计
    - all_books=true: 返回所有书的总览（含每本的小计）
    """
    state = load_checkpoint()
    current_novel_id = get_state_novel_id(state)

    if all_books:
        # 全书总览
        return {
            "status": "success",
            "usage": model_layer.get_usage_summary(novel_id="__all__"),
            "current_novel_id": current_novel_id,
        }

    target_id = novel_id or current_novel_id or "__unknown__"
    bucket = model_layer.get_usage_summary(novel_id=target_id)
    return {
        "status": "success",
        "usage": bucket,
        "current_novel_id": current_novel_id,
        "queried_novel_id": target_id,
    }


@app.post("/api/usage/reset")
async def reset_usage_endpoint(novel_id: str = ""):
    """重置 token 统计。novel_id 空 → 重置全部；否则只重置该书。"""
    if novel_id:
        model_layer.reset_usage(novel_id=novel_id)
        msg = f"已重置作品 {novel_id} 的 token 统计"
    else:
        model_layer.reset_usage()
        msg = "已重置全部 token 统计"
    return {"status": "success", "message": msg}


@app.post("/api/usage/backfill_from_chapters")
async def backfill_usage_from_chapters(req: dict):
    """从已保存的章节文件大小，**估算**历史 token 用量并回填到 per_novel。

    适用场景：之前生成的章节没记录 usage（中间件未生效前），
    或者重启后丢失了进程内 records。用户点这个端点 → 从 chapters/*.txt 文件大小
    按经验公式估算（中文 1 字符 ≈ 1.5 token），写入 per_novel。

    Body: {
        "novel_id": "可选，默认当前激活",
        "ratio": 1.5,           # 字符/token 转换比，可调
        "extra_calls_per_chapter": 3,  # 每章大约调用几次 LLM（planner/editor/writer）
        "mode": "add" | "replace"  # add=累加, replace=覆盖
    }
    """
    novel_id = (req or {}).get("novel_id", "") or ""
    if not novel_id:
        state = load_checkpoint()
        novel_id = get_state_novel_id(state) if state else ""

    ratio = float((req or {}).get("ratio", 1.5))
    extra_calls = int((req or {}).get("extra_calls_per_chapter", 3))
    mode = (req or {}).get("mode", "add")

    if not novel_id:
        raise HTTPException(status_code=400, detail="未指定 novel_id，且当前也没有激活作品")

    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        # fallback：load_checkpoint 用于拿 novel_dir_name
        state = load_checkpoint() or {}
    novel_title = state.get("novel_title", "") if state else ""

    # 优先用 state._novel_dir → chapters/（最准确）
    chapters_dir = get_state_chapters_dir(state) if state else None
    if not chapters_dir or not os.path.isdir(chapters_dir):
        # fallback：直接搜 NOVELS_DIR 找包含 novel_id 的子目录
        chapters_dir = None
        novels_root = NOVELS_DIR
        if os.path.isdir(novels_root):
            target_short = novel_id[:8] if len(novel_id) > 8 else novel_id
            candidates = [os.path.join(novels_root, novel_id, "chapters")]
            for root, dirs, files in os.walk(novels_root):
                base = os.path.basename(root)
                if base.endswith(novel_id) or base.endswith(target_short) or novel_id in base or target_short in base:
                    cand = os.path.join(root, "chapters")
                    if os.path.isdir(cand):
                        chapters_dir = cand
                        break
            if not chapters_dir:
                for c in candidates:
                    if os.path.isdir(c):
                        chapters_dir = c
                        break
    if not chapters_dir:
        raise HTTPException(status_code=404, detail=f"找不到章节目录：{chapters_dir}")

    # 文件名可能是 "第1章.txt" / "001.txt" / "chapter_1.txt" 等多种格式
    import re as _re
    def _is_chapter_file(name: str) -> bool:
        if not name.endswith(".txt"):
            return False
        stem = name[:-4]
        # 形式 1: "第N章" / "第N章 第N节"
        if _re.match(r"^第\d+章", stem):
            return True
        # 形式 2: "001" / "chapter_001" / "ch_001"
        if _re.match(r"^\d{1,4}$", stem) or _re.match(r"^(chapter|ch)[-_]?\d+", stem, _re.I):
            return True
        return False

    files = sorted([f for f in os.listdir(chapters_dir) if _is_chapter_file(f)])
    if not files:
        return {"status": "success", "backfilled": 0, "estimated_tokens": 0, "message": "无章节文件可估算"}

    total_chars = 0
    chapter_count = len(files)
    char_per_chapter = []
    for fn in files:
        p = os.path.join(chapters_dir, fn)
        try:
            with open(p, "r", encoding="utf-8") as f:
                content = f.read()
            # 估算：去掉标题行和空白
            chars = sum(1 for c in content if c.strip())
            char_per_chapter.append({"file": fn, "chars": chars})
            total_chars += chars
        except Exception as e:
            print(f"[backfill] 读 {p} 失败: {e}")

    estimated_tokens = int(total_chars * ratio)
    # 每章大约 3-4 次 LLM 调用（planner/editor/writer + 多次 patcher）
    estimated_calls = chapter_count * (1 + extra_calls)

    # 写入 per_novel
    with model_layer.USAGE._lock:
        bucket = model_layer.USAGE.per_novel.setdefault(
            novel_id,
            model_layer.USAGE._empty_novel_bucket(),
        )
        if mode == "replace":
            bucket["total_calls"] = estimated_calls
            bucket["total_prompt_tokens"] = 0
            bucket["total_completion_tokens"] = 0
            bucket["total_tokens"] = estimated_tokens
            bucket["total_duration_seconds"] = 0
            bucket["by_role"] = {
                "planner": {"calls": chapter_count, "tokens": int(estimated_tokens * 0.15)},
                "writer": {"calls": chapter_count, "tokens": int(estimated_tokens * 0.55)},
                "editor": {"calls": chapter_count, "tokens": int(estimated_tokens * 0.20)},
                "patcher": {"calls": chapter_count * extra_calls, "tokens": int(estimated_tokens * 0.10)},
            }
            bucket["by_provider"] = {
                "local": {"calls": int(estimated_calls * 0.7), "tokens": int(estimated_tokens * 0.5)},
                "minimax": {"calls": int(estimated_calls * 0.3), "tokens": int(estimated_tokens * 0.5)},
            }
        else:
            # add：在已有基础上累加（避免覆盖实时统计）
            bucket["total_calls"] += estimated_calls
            bucket["total_tokens"] += estimated_tokens
            bucket["by_provider"].setdefault("local", {"calls": 0, "tokens": 0})
            bucket["by_provider"]["local"]["calls"] += int(estimated_calls * 0.7)
            bucket["by_provider"]["local"]["tokens"] += int(estimated_tokens * 0.5)
            bucket["by_provider"].setdefault("minimax", {"calls": 0, "tokens": 0})
            bucket["by_provider"]["minimax"]["calls"] += int(estimated_calls * 0.3)
            bucket["by_provider"]["minimax"]["tokens"] += int(estimated_tokens * 0.5)
        bucket["last_updated"] = time.time()

    # 写盘
    model_layer.USAGE.save_to_disk()

    return {
        "status": "success",
        "novel_id": novel_id,
        "novel_title": novel_title,
        "chapter_count": chapter_count,
        "total_chars": total_chars,
        "estimated_tokens": estimated_tokens,
        "estimated_calls": estimated_calls,
        "mode": mode,
        "chapter_details": char_per_chapter[:20],  # 最多返回 20 条详情
    }

# 注意：/api/tasks/active 必须在 /api/tasks/{task_id} 之前声明，否则 "active" 会被当成 task_id 拦截
@app.get("/api/tasks/active")
async def get_active_tasks():
    """返回当前用户所有未完成的任务（queued / running / interrupted）。
    - interrupted = 服务重启导致任务中断，需要用户重试
    前端刷新后用这个端点发现后台任务，恢复监控 + 提示用户。
    """
    user_id = get_active_user_id()
    with TASK_LOCK:
        active = [
            t for t in TASKS.values()
            if t.get("user_id") == user_id and t.get("status") in ("queued", "running", "interrupted")
        ]
    return {
        "status": "success",
        "tasks": [_task_snapshot(t) for t in active],
        "count": len(active),
    }


@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str):
    task = get_task(task_id, get_active_user_id())
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return task


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在或已过期")
        if task.get("status") != "error":
            raise HTTPException(status_code=400, detail="只有失败任务可以重试")
        work = task.get("work")
        name = task.get("name", "retry_task")
        if not work:
            raise HTTPException(status_code=400, detail="该任务缺少重试上下文")

    retry_task_id = start_background_task(name, work, user_id=get_active_user_id())
    return {"status": "queued", "task_id": retry_task_id, "message": "重试任务已提交"}

@app.get("/api/chapter/{chapter_num}")
async def get_chapter(chapter_num: int):
    """从文件系统读取指定章节内容"""
    try:
        state = load_checkpoint()
        if state:
            content = read_chapter_from_state(state, chapter_num)
            return {"status": "success", "content": content, "word_count": len(content)}

        for name in chapter_file_candidates(chapter_num):
            filename = os.path.join(OUTPUT_DIR, name)
            if os.path.exists(filename):
                with open(filename, "r", encoding="utf-8") as f:
                    content = f.read()
                return {"status": "success", "content": content, "word_count": len(content)}

        content = get_current_story_bible().get_chapter_content(chapter_num)
        if content:
            return {"status": "success", "content": content, "word_count": len(content)}
        raise HTTPException(status_code=404, detail="章节文件不存在")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/chapter/{chapter_num}/versions")
async def get_chapter_versions(chapter_num: int):
    return {"status": "success", "result": get_current_story_bible().get_chapter_versions(chapter_num)}

@app.get("/api/chapter_versions/{version_id}")
async def get_chapter_version(version_id: int):
    detail = get_current_story_bible().get_chapter_version_detail(version_id)
    if not detail:
        raise HTTPException(status_code=404, detail="版本不存在")
    return {"status": "success", "result": detail}

@app.get("/api/chapter/{chapter_num}/versions/{version_id}/diff")
async def get_chapter_version_diff(chapter_num: int, version_id: int):
    state = load_checkpoint()
    if not state:
        raise HTTPException(status_code=400, detail="未找到当前作品")

    version = get_story_bible_for_state(state).get_chapter_version_detail(version_id)
    if not version:
        raise HTTPException(status_code=404, detail="版本不存在")

    try:
        current = await get_chapter(chapter_num)
        current_content = current.get("content", "")
    except HTTPException:
        current_content = ""

    return {
        "status": "success",
        "result": {
            "version": version,
            "diff": make_text_diff(version.get("content", ""), current_content)
        }
    }


# P2 增强：恢复章节到指定版本（带 novel_id）
@app.post("/api/novels/{novel_id}/chapters/{chapter_num}/versions/{version_id}/restore")
async def restore_chapter_version_endpoint(
    novel_id: str,
    chapter_num: int,
    version_id: int,
):
    """恢复章节到指定历史版本（写入文件系统 + state + checkpoint + 新建一个 restore 版本）。
    适用：反馈修订误删、想撤回、想看某历史版本等场景。
    """
    state = find_checkpoint_by_novel_id(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")

    bible = get_story_bible_for_state(state)
    version = bible.get_chapter_version_detail(version_id)
    if not version:
        raise HTTPException(status_code=404, detail="版本不存在")
    if int(version.get("chapter_num", 0)) != int(chapter_num):
        raise HTTPException(status_code=400, detail="版本与章节号不匹配")

    content = version.get("content", "")
    if not content:
        raise HTTPException(status_code=400, detail="版本内容为空")

    # 1. 写回文件（复用 save_chapter 逻辑，自动 ensure chapter title + 用 find_chapter_file 找到的路径）
    novel_title = state.get("novel_title", "")
    save_chapter(chapter_num, content, novel_title, novel_id)

    # 2. 更新 state（如果是当前章节）
    if int(state.get("current_chapter", 1) or 1) - 1 == int(chapter_num) or \
       int(state.get("last_saved_chapter", 0) or 0) == int(chapter_num):
        state["chapter_content"] = content

    # 3. 写 checkpoint
    try:
        checkpoint_path = state.get("_checkpoint_path")
        if checkpoint_path and os.path.exists(os.path.dirname(checkpoint_path)):
            state.pop("_checkpoint_path", None)
            state.pop("_novel_dir", None)
            write_json_file(checkpoint_path, state)
    except Exception as e:
        print(f"[restore_version] 写 checkpoint 失败: {e}")

    # 4. 新建一个"恢复来源"版本（让用户能再次回滚回去）
    note = f"恢复自 v{version_id}（{version.get('source', '?')} / {version.get('created_at', 0):.0f}）"
    bible.add_chapter_version(chapter_num, "restore_from_version", content, note)

    return {
        "status": "success",
        "message": f"已恢复到 v{version_id}",
        "chapter_num": chapter_num,
        "version_id": version_id,
        "new_version_id": None,  # add_chapter_version 不返回 id，先置 None
        "new_length": len(content),
    }

@app.get("/api/chapter/{chapter_num}/patch_records")
async def get_chapter_patch_records(chapter_num: int):
    return {"status": "success", "result": get_current_story_bible().get_patch_records(chapter_num)}

@app.get("/api/chapter/{chapter_num}/consistency_reports")
async def get_chapter_consistency_reports(chapter_num: int):
    return {"status": "success", "result": get_current_story_bible().get_consistency_reports(chapter_num)}


def run_refresh_chapter_risks(chapter_num: int, progress: Callable[[str, str], None] | None = None) -> dict:
    """Re-run chapter risk detectors for the current saved chapter."""
    def emit(message: str, phase: str = "review"):
        if progress:
            progress(message, phase)

    state = load_checkpoint()
    if not state:
        raise RuntimeError("未找到当前作品")

    novel_id = get_state_novel_id(state)
    bible = get_story_bible_for_state(state)
    content = read_chapter_from_state(state, chapter_num)
    if not content.strip():
        raise RuntimeError("章节正文不存在，无法检测风险")

    emit("正在重新检测本章格式与发布风险...", "review")
    target_words = int(state.get("chapter_target_words", 2000) or 2000)
    min_words, max_words = get_chapter_word_range(target_words)
    word_count = len(content)

    from agents.editor import (
        detect_fanqie_content_safety_risks,
        detect_platform_format_risks,
        run_consistency_detector,
        run_style_drift_detector,
    )
    from prompts import get_story_phase

    edit_round = int(state.get("edit_count", 0) or 0) + 1
    local_risks = detect_platform_format_risks(content, min_words, max_words, word_count)
    local_risks += detect_fanqie_content_safety_risks(content)
    for risk in local_risks:
        bible.add_consistency_report(
            chapter_num=chapter_num,
            review_round=edit_round,
            severity=risk["severity"],
            category=risk["category"],
            message=risk["message"] + (f"\n依据：{risk['evidence']}" if risk.get("evidence") else ""),
            suggestion=risk.get("suggestion", ""),
            status="open" if risk["severity"] == "must_fix" else "noted",
        )

    detector_state = dict(state)
    detector_state["novel_id"] = novel_id
    detector_state["current_chapter"] = chapter_num
    detector_state["chapter_content"] = content
    detector_state["chapter_outline"] = state.get("chapter_outline") or f"第{chapter_num}章已保存正文，按当前全书大纲和前情重新检测风险。"
    detector_state["bible_context"] = state.get("bible_context") or "暂无"

    story_phase = get_story_phase(chapter_num, int(state.get("num_chapters", 100) or 100))
    detector_risks = []
    try:
        emit("正在重新执行一致性分类检测...", "review")
        detector_risks = run_consistency_detector(
            state=detector_state,
            bible=bible,
            chapter_num=chapter_num,
            edit_round=edit_round,
            story_phase=story_phase,
            min_words=min_words,
            max_words=max_words,
            word_count=word_count,
        )
    except Exception as exc:
        print(f"  ⚠️ 手动刷新一致性检测失败：{exc}")
        bible.add_consistency_report(
            chapter_num=chapter_num,
            review_round=edit_round,
            severity="warning",
            category="检测失败",
            message=f"一致性分类检测失败：{exc}",
            suggestion="可稍后重试，或先进行人工复读。",
            status="noted",
        )

    style_risks = []
    try:
        emit("正在重新检测文风稳定性...", "review")
        style_risks = run_style_drift_detector(
            state=detector_state,
            bible=bible,
            chapter_num=chapter_num,
            edit_round=edit_round,
        )
    except Exception as exc:
        print(f"  ⚠️ 手动刷新文风检测失败：{exc}")
        bible.add_consistency_report(
            chapter_num=chapter_num,
            review_round=edit_round,
            severity="warning",
            category="检测失败",
            message=f"文风稳定性检测失败：{exc}",
            suggestion="可稍后重试，或先进行人工复读。",
            status="noted",
        )

    total_risks = len(local_risks) + len(detector_risks) + len(style_risks)
    if total_risks == 0:
        bible.add_consistency_report(
            chapter_num=chapter_num,
            review_round=edit_round,
            severity="pass",
            category="手动刷新",
            message="重新检测后，未发现必须处理的本章风险。",
            suggestion="",
            status="closed",
        )

    reports = bible.get_consistency_reports(chapter_num)
    emit("本章风险刷新完成", "done")
    return {
        "status": "success",
        "message": f"本章风险刷新完成，新增检测项 {total_risks} 条。",
        "risk_count": total_risks,
        "result": reports,
    }


@app.post("/api/chapter/{chapter_num}/refresh_risks")
async def refresh_chapter_risks(chapter_num: int):
    task_id = start_background_task(
        "refresh_chapter_risks",
        lambda progress: run_refresh_chapter_risks(chapter_num, progress),
    )
    return {"status": "queued", "task_id": task_id, "message": "本章风险刷新任务已提交"}


SINGLE_RISK_FIX_SYSTEM = """你是一个谨慎的网文章节修订编辑。
你的任务是只修复用户指定的一条风险，不重写全章、不改变主线、不引入新设定。
你必须同时保证修订后的正文没有重复段落、近似复述、空泛灌水和超长未分段内容。
只输出修订后的完整章节正文，不要解释，不要 Markdown，不要作者说明。"""

SINGLE_RISK_FIX_PROMPT = """请针对下面这一条风险，修订章节正文。

【章节号】
第 {chapter_num} 章

【需要修复的单条风险】
分类：{category}
严重程度：{severity}
问题：{message}
建议：{suggestion}

【修订要求】
1. 只处理这条风险，不要顺手大改其他剧情。
2. 保留原有章节标题、主线事件、人物关系、结尾方向和已出现设定。
3. 如果风险涉及格式、未分段、段落过长，必须重排段落，移动端阅读每段 1-3 句。
4. 如果风险涉及字数偏短，可以自然补充动作、心理、对话、场景压力或信息增量，但不要灌水重复。
5. 如果风险涉及一致性冲突，优先用小范围措辞修正解决，不要推翻后文。
6. 修订后必须删除重复段落、重复句、近似复述和模板化情绪空转；如果删掉后偏短，只能补充新的剧情动作、信息增量或对话交锋。
7. 输出必须是完整修订后的章节正文。

【原章节正文】
{chapter_content}
"""


LOW_QUALITY_REVISION_SYSTEM = """你是一个网文章节低质清理编辑。
你的任务是在不改变主线剧情、不改变人物关系、不改变结尾方向的前提下，清理重复、灌水和格式问题。
只输出清理后的完整章节正文，不要解释，不要 Markdown，不要作者说明。"""

LOW_QUALITY_REVISION_PROMPT = """请清理小说第 {chapter_num} 章的低质问题。

【目标字数范围】
{min_words}-{max_words} 字

【程序检测到的问题】
{issues}

【清理要求】
1. 删除完全重复段落、近似重复段落和重复句子。
2. 压缩空泛心理、模板化情绪、同一动作反复描写。
3. 不要为了补字灌水；需要补足时，只能增加新的动作、信息、对话交锋、冲突升级或具体场景压力。
4. 保留章节标题、主线事件、人物关系和章末钩子方向。
5. 段落适合手机阅读，每段 1-3 句；对话、动作、心理和环境描写自然分段。
6. 输出完整章节正文。

【原章节正文】
{chapter_content}
"""


OPENING_INTEGRITY_FIX_SYSTEM = """你是一个网文章节开头修复编辑。
你的任务是修复章节标题缺失、开头断裂、第一段像半句话、段落格式混乱等问题。
只输出修复后的完整章节正文，不要解释，不要 Markdown，不要作者说明。"""

OPENING_INTEGRITY_FIX_PROMPT = """请修复小说第 {chapter_num} 章的章节格式与开头完整性。

【必须保留的章节标题】
{title}

【修复要求】
1. 第一行必须是章节标题：{title}
2. 标题后第一段必须是完整自然的开场，不要从半句话、总结句、残缺句开始。
3. 不改变主线剧情、人物关系和章末钩子方向。
4. 只允许补齐开头必要衔接、重排段落、删除提示语或修复断裂句。
5. 每段 1-3 句，适合移动端阅读。

【原章节正文】
{old_content}

【待修复正文】
{chapter_content}
"""


RISK_FIX_VERIFIER_SYSTEM = """你是一个严格的网文修复验收员。
你的任务是判断修复后的章节是否真的解决了指定风险。
你必须只输出 JSON，不要 Markdown，不要解释。"""

RISK_FIX_VERIFIER_PROMPT = """请判断下面的修复结果是否真正解决了指定风险。

【章节号】
第 {chapter_num} 章

【指定风险】
分类：{category}
严重程度：{severity}
问题：{message}
建议：{suggestion}

【验收标准】
1. 如果只是删除标题、返回提示语、格式变乱、开头断裂，必须判定为未通过。
2. 如果风险是字数偏短，修复后字数必须达到目标下限。
3. 如果风险是重复/低质，修复后必须明显减少重复，并补充有效剧情推进。
4. 如果风险是一致性/时间线/设定冲突，修复后必须在正文中能看到对应问题被处理。
5. 不能因为模型声称“已修复”就通过，必须依据正文内容判断。

【目标字数范围】
{min_words}-{max_words} 字

【修复后字数】
{word_count} 字

【修复后正文】
{chapter_content}

请严格输出 JSON：
{{
  "passed": true,
  "reason": "通过或未通过原因",
  "remaining_issue": "如果未通过，具体还剩什么问题"
}}
"""


def is_paragraph_risk(report: dict[str, Any]) -> bool:
    text = " ".join(str(report.get(key, "")) for key in ("category", "message", "suggestion"))
    keywords = ["段落", "分段", "未分段", "长段", "格式", "排版", "发布格式"]
    return any(keyword in text for keyword in keywords)


def clean_ai_revised_chapter(
    chapter_num: int,
    content: str,
    min_words: int,
    max_words: int,
    old_content: str = "",
    force_llm_cleanup: bool = False,
) -> str:
    """Normalize AI-written/repaired chapter text and clean obvious repetition."""
    cleaned = ensure_chapter_title(content, chapter_num, old_content)
    issues = detect_repetition_issues(cleaned)
    must_cleanup = force_llm_cleanup or any(item.get("severity") == "must_fix" for item in issues)
    must_repair_opening = looks_like_fake_fix(cleaned) or looks_like_broken_opening(cleaned)
    if not must_cleanup and not must_repair_opening:
        return cleaned

    from models import call_llm

    revised = cleaned
    if must_cleanup:
        revised = call_llm(
            role="editor",
            system_prompt=LOW_QUALITY_REVISION_SYSTEM,
            prompt=LOW_QUALITY_REVISION_PROMPT.format(
                chapter_num=chapter_num,
                min_words=min_words,
                max_words=max_words,
                issues=format_repetition_issues(issues),
                chapter_content=cleaned,
            ),
            temperature=0.28,
            max_tokens=8192,
        )
        revised = ensure_chapter_title(revised, chapter_num, old_content)

    if looks_like_fake_fix(revised) or looks_like_broken_opening(revised):
        title = ensure_chapter_title(revised, chapter_num, old_content).split("\n", 1)[0].strip()
        revised = call_llm(
            role="editor",
            system_prompt=OPENING_INTEGRITY_FIX_SYSTEM,
            prompt=OPENING_INTEGRITY_FIX_PROMPT.format(
                chapter_num=chapter_num,
                title=title,
                old_content=old_content,
                chapter_content=revised,
            ),
            temperature=0.22,
            max_tokens=8192,
        )
    revised = ensure_chapter_title(revised, chapter_num, old_content)
    return revised or cleaned


def parse_fix_verification(raw_text: str) -> dict[str, Any]:
    text = normalize_llm_json_text(raw_text)
    try:
        data = json.loads(text)
    except Exception:
        return {"passed": False, "reason": "验收结果 JSON 解析失败", "remaining_issue": raw_text[:300]}
    if not isinstance(data, dict):
        return {"passed": False, "reason": "验收结果格式异常", "remaining_issue": ""}
    return {
        "passed": bool(data.get("passed")),
        "reason": str(data.get("reason") or ""),
        "remaining_issue": str(data.get("remaining_issue") or ""),
    }


def verify_single_risk_fix(
    chapter_num: int,
    report: dict[str, Any],
    chapter_content: str,
    min_words: int,
    max_words: int,
) -> dict[str, Any]:
    if looks_like_fake_fix(chapter_content):
        return {"passed": False, "reason": "修复结果像提示语而不是正文", "remaining_issue": "未返回完整章节正文"}
    if looks_like_broken_opening(chapter_content):
        return {"passed": False, "reason": "章节开头疑似断裂", "remaining_issue": "开头不是完整自然的正文段落"}
    if len(chapter_content) < min_words and "字数" in " ".join(str(report.get(key, "")) for key in ("category", "message", "suggestion")):
        return {"passed": False, "reason": "字数仍低于目标下限", "remaining_issue": f"当前 {len(chapter_content)} 字，低于 {min_words} 字"}

    from models import call_llm

    raw = call_llm(
        role="editor",
        system_prompt=RISK_FIX_VERIFIER_SYSTEM,
        prompt=RISK_FIX_VERIFIER_PROMPT.format(
            chapter_num=chapter_num,
            category=report.get("category", ""),
            severity=report.get("severity", ""),
            message=report.get("message", ""),
            suggestion=report.get("suggestion", ""),
            min_words=min_words,
            max_words=max_words,
            word_count=len(chapter_content),
            chapter_content=chapter_content,
        ),
        temperature=0.1,
        max_tokens=2048,
    )
    return parse_fix_verification(raw)


def finalize_generated_content_before_save(state: dict, chapter_num: int, content: str) -> str:
    target_words = int(state.get("chapter_target_words", 2000) or 2000)
    min_words, max_words = get_chapter_word_range(target_words)
    context = "\n\n".join(
        str(state.get(key, "") or "")
        for key in ("chapter_outline", "chapter_drama_card", "full_outline_text", "global_outline")
    )
    finalized = clean_ai_revised_chapter(
        chapter_num,
        content,
        min_words,
        max_words,
        old_content=context,
        force_llm_cleanup=False,
    )
    if len(finalized) < min_words:
        from agents.writer import ensure_chapter_word_count
        finalized = ensure_chapter_word_count(
            finalized,
            chapter_num=chapter_num,
            target_words=target_words,
            min_words=min_words,
            max_words=max_words,
        )
        finalized = clean_ai_revised_chapter(
            chapter_num,
            finalized,
            min_words,
            max_words,
            old_content=context,
            force_llm_cleanup=False,
        )
    if looks_like_fake_fix(finalized) or looks_like_broken_opening(finalized):
        raise RuntimeError("章节标题或开头完整性未通过：生成结果疑似缺标题、开头断裂或不是完整正文，请重试生成。")
    # 最终格式清理（删除表情符、修复 markdown 残留、折叠连续标点等）
    finalized = clean_ai_format_artifacts(finalized)
    return finalized


def run_fix_single_chapter_risk(
    chapter_num: int,
    report_id: int,
    progress: Callable[[str, str], None] | None = None,
) -> dict:
    def emit(message: str, phase: str = "patch"):
        if progress:
            progress(message, phase)

    state = load_checkpoint()
    if not state:
        raise RuntimeError("未找到当前作品")

    novel_id = get_state_novel_id(state)
    novel_title = state.get("novel_title", "")
    bible = get_story_bible_for_state(state)
    reports = bible.get_consistency_reports(chapter_num)
    report = next((item for item in reports if int(item.get("id", 0) or 0) == int(report_id)), None)
    if not report:
        raise RuntimeError("未找到这条风险报告，可能已被刷新替换")
    if report.get("status") == "closed":
        return {
            "status": "success",
            "fixed": False,
            "message": "这条风险已处理，无需重复修复。",
            "report_id": report_id,
        }

    old_content = read_chapter_from_state(state, chapter_num)
    if not old_content.strip():
        raise RuntimeError("章节正文不存在，无法修复")
    target_words = int(state.get("chapter_target_words", 2000) or 2000)
    min_words, max_words = get_chapter_word_range(target_words)

    emit("正在修复这条风险...", "patch")
    if is_paragraph_risk(report):
        patched = normalize_chapter_text(old_content, dedupe=True)
    else:
        from models import call_llm
        patched = call_llm(
            role="editor",
            system_prompt=SINGLE_RISK_FIX_SYSTEM,
            prompt=SINGLE_RISK_FIX_PROMPT.format(
                chapter_num=chapter_num,
                category=report.get("category", ""),
                severity=report.get("severity", ""),
                message=report.get("message", ""),
                suggestion=report.get("suggestion", ""),
                chapter_content=old_content,
            ),
            temperature=0.25,
            max_tokens=8192,
        )
        patched = clean_ai_revised_chapter(
            chapter_num,
            patched,
            min_words,
            max_words,
            old_content=old_content,
            force_llm_cleanup=any(
                keyword in " ".join(str(report.get(key, "")) for key in ("category", "message", "suggestion"))
                for keyword in ("重复", "低质", "无意义", "灌水")
            ),
        )
        if len(patched) < min_words:
            from agents.writer import ensure_chapter_word_count
            emit("清理重复后正文偏短，正在补足有效剧情内容...", "writing")
            patched = ensure_chapter_word_count(
                patched,
                chapter_num=chapter_num,
                target_words=target_words,
                min_words=min_words,
                max_words=max_words,
            )
            patched = clean_ai_revised_chapter(chapter_num, patched, min_words, max_words, old_content=old_content)

    if not patched.strip():
        raise RuntimeError("模型未返回有效修订正文")
    verification = verify_single_risk_fix(chapter_num, report, patched, min_words, max_words)
    if not verification.get("passed"):
        emit("第一次修复未通过验收，正在带着问题再次修订...", "patch")
        from models import call_llm
        retry_prompt = SINGLE_RISK_FIX_PROMPT.format(
            chapter_num=chapter_num,
            category=report.get("category", ""),
            severity=report.get("severity", ""),
            message=(
                f"{report.get('message', '')}\n\n"
                f"上一次修复未通过验收：{verification.get('reason', '')}；"
                f"仍存在：{verification.get('remaining_issue', '')}。"
            ),
            suggestion=(
                f"{report.get('suggestion', '')}\n"
                "这一次必须修复验收指出的问题，尤其要保留标题、补齐开头、真正改善正文质量。"
            ),
            chapter_content=patched,
        )
        patched_retry = call_llm(
            role="editor",
            system_prompt=SINGLE_RISK_FIX_SYSTEM,
            prompt=retry_prompt,
            temperature=0.22,
            max_tokens=8192,
        )
        patched_retry = clean_ai_revised_chapter(
            chapter_num,
            patched_retry,
            min_words,
            max_words,
            old_content=old_content,
            force_llm_cleanup=True,
        )
        if len(patched_retry) < min_words:
            from agents.writer import ensure_chapter_word_count
            patched_retry = ensure_chapter_word_count(
                patched_retry,
                chapter_num=chapter_num,
                target_words=target_words,
                min_words=min_words,
                max_words=max_words,
            )
            patched_retry = clean_ai_revised_chapter(chapter_num, patched_retry, min_words, max_words, old_content=old_content)
        second_verification = verify_single_risk_fix(chapter_num, report, patched_retry, min_words, max_words)
        if second_verification.get("passed"):
            patched = patched_retry
            verification = second_verification
        else:
            bible.add_patch_record(
                chapter_num=chapter_num,
                edit_round=int(state.get("edit_count", 0) or 0) + 1,
                patch_index=int(report_id),
                target_text=(report.get("message", "") or "")[:1000],
                instruction=report.get("suggestion", "") or "修复指定风险项",
                replacement_text=patched_retry[:2000],
                success=False,
                reason=f"自动修复未通过验收：{second_verification.get('reason', '')} {second_verification.get('remaining_issue', '')}",
            )
            return {
                "status": "verification_failed",
                "fixed": False,
                "message": f"自动修复未通过质量验收：{second_verification.get('reason', '') or '仍存在问题'}。风险未关闭，建议手动编辑或重试。",
                "report_id": report_id,
                "content": old_content,
                "word_count": len(old_content),
                "verification": second_verification,
            }

    if patched.strip() == normalize_chapter_paragraphs(old_content).strip():
        if is_paragraph_risk(report):
            bible.close_consistency_report(report_id)
            return {
                "status": "success",
                "fixed": False,
                "message": "正文已是分段格式，已将该格式风险标记为已处理。",
                "report_id": report_id,
                "content": old_content,
                "word_count": len(old_content),
            }
        return {
            "status": "no_patch",
            "fixed": False,
            "message": "这条风险未产生有效修订，建议使用手动编辑。",
            "report_id": report_id,
        }

    save_chapter(chapter_num, patched, novel_title, novel_id)
    bible.add_chapter_version(chapter_num, "risk_item_fix", patched, f"单条风险修复：{report.get('category', '本章风险')}")
    bible.add_patch_record(
        chapter_num=chapter_num,
        edit_round=int(state.get("edit_count", 0) or 0) + 1,
        patch_index=int(report_id),
        target_text=(report.get("message", "") or "")[:1000],
        instruction=report.get("suggestion", "") or "修复指定风险项",
        replacement_text="已生成完整修订章节，详见 risk_item_fix 版本。",
        success=True,
        reason="单条风险修复完成",
    )
    bible.close_consistency_report(report_id)
    bible.add_consistency_report(
        chapter_num=chapter_num,
        review_round=int(state.get("edit_count", 0) or 0) + 1,
        severity="pass",
        category="单项修复",
        message=f"已修复风险：{report.get('category', '本章风险')}",
        suggestion="建议点击刷新重新检测本章风险。",
        status="closed",
    )

    if chapter_num in {state.get("current_chapter", 1), state.get("current_chapter", 1) - 1}:
        state["chapter_content"] = patched
    set_chapter_publish_status(state, chapter_num, "needs_review", "章节已修复单条风险，需要重新检查发布包")
    save_checkpoint(state, novel_title, novel_id)

    emit("单条风险修复完成", "done")
    return {
        "status": "success",
        "fixed": True,
        "message": "这条风险已修复，并保存为新版本。",
        "report_id": report_id,
        "content": patched,
        "word_count": len(patched),
    }


@app.post("/api/chapter/{chapter_num}/risks/{report_id}/fix")
async def fix_single_chapter_risk(chapter_num: int, report_id: int):
    task_id = start_background_task(
        "fix_single_chapter_risk",
        lambda progress: run_fix_single_chapter_risk(chapter_num, report_id, progress),
    )
    return {"status": "queued", "task_id": task_id, "message": "单条风险修复任务已提交"}


def run_auto_fix_chapter_risks(chapter_num: int, progress: Callable[[str, str], None] | None = None) -> dict:
    """Use open must-fix risk reports to generate and apply safe local patches."""
    def emit(message: str, phase: str = "patch"):
        if progress:
            progress(message, phase)

    state = load_checkpoint()
    if not state:
        raise RuntimeError("未找到当前作品")

    novel_id = get_state_novel_id(state)
    bible = get_story_bible_for_state(state)
    emit("正在读取本章风险报告...", "review")
    reports = bible.get_consistency_reports(chapter_num)
    must_fix_reports = [
        item for item in reports
        if item.get("severity") == "must_fix" and item.get("status", "open") != "closed"
    ]
    if not must_fix_reports:
        return {
            "status": "no_fix",
            "message": "当前章节没有必须修复的风险。建议类问题可以使用手动编辑处理。",
            "fixed": False,
        }

    old_content = read_chapter_from_state(state, chapter_num)
    if not old_content.strip():
        raise RuntimeError("章节正文不存在，无法修复")

    from agents.editor import format_detector_risks_for_patcher
    from agents.patcher import patch_chapter
    from agents.writer import ensure_chapter_word_count
    from config import get_chapter_word_range
    from models import call_llm

    emit(f"正在根据 {len(must_fix_reports)} 条必须修复风险生成补丁...", "patch")
    risk_patch_required = format_detector_risks_for_patcher(must_fix_reports[:8])
    patch_state = {
        **state,
        "novel_id": novel_id,
        "current_chapter": chapter_num,
        "chapter_content": old_content,
        "edit_required": "",
        "risk_patch_required": risk_patch_required,
        "edit_count": max(1, int(state.get("edit_count", 0) or 0)),
    }
    patched = patch_chapter(patch_state).get("chapter_content", old_content)
    target_words = int(state.get("chapter_target_words", 2000) or 2000)
    min_words, max_words = get_chapter_word_range(target_words)

    if patched.strip() == old_content.strip():
        emit("局部补丁未命中，正在进行整章风险修订兜底...", "patch")
        patched = call_llm(
            role="editor",
            system_prompt=(
                "你是一位负责风险修订的网文主编。你的任务是在不改变主线走向、人物关系和章末钩子的前提下，"
                "根据风险报告修订完整章节。必须删除重复段落、近似复述、空泛灌水和超长未分段内容。"
                "只输出修订后的完整章节正文，不要解释，不要 Markdown。"
            ),
            prompt=(
                f"请修订小说第 {chapter_num} 章。\n\n"
                f"【必须修复风险】\n{risk_patch_required}\n\n"
                f"【修订要求】\n"
                f"1. 保留原章节标题和核心剧情。\n"
                f"2. 优先修复 must_fix 风险，不要新增无关剧情。\n"
                f"3. 保持上下文连贯，章末钩子方向不变。\n"
                f"4. 字数不得低于 {min_words} 字，建议控制在 {min_words}-{max_words} 字。\n"
                f"5. 删除重复段落、重复句和近似复述；不要用同一心理、同一动作、同一解释反复凑字。\n"
                f"6. 每段 1-3 句，对话、动作、心理、环境描写自然分段，适合移动端阅读。\n"
                f"7. 如果需要补字，只能补新的剧情动作、信息增量、对话交锋、冲突升级或具体场景压力。\n"
                f"8. 输出完整章节正文。\n\n"
                f"【原章节正文】\n{old_content}"
            ),
            temperature=0.45,
            max_tokens=8192,
        )
        patched = (patched or "").strip()

    if len(patched) < min_words:
        emit("修复后正文偏短，正在补足字数并保持连贯...", "writing")
        patched = ensure_chapter_word_count(
            patched,
            chapter_num=chapter_num,
            target_words=target_words,
            min_words=min_words,
            max_words=max_words,
        )

    patched = clean_ai_revised_chapter(
        chapter_num,
        patched,
        min_words,
        max_words,
        old_content=old_content,
        force_llm_cleanup=any(
            any(keyword in " ".join(str(report.get(key, "")) for key in ("category", "message", "suggestion"))
                for keyword in ("重复", "低质", "无意义", "灌水"))
            for report in must_fix_reports
        ),
    )
    if len(patched) < min_words:
        emit("清理重复后正文偏短，正在补足有效剧情内容...", "writing")
        patched = ensure_chapter_word_count(
            patched,
            chapter_num=chapter_num,
            target_words=target_words,
            min_words=min_words,
            max_words=max_words,
        )
        patched = clean_ai_revised_chapter(chapter_num, patched, min_words, max_words, old_content=old_content)

    if looks_like_fake_fix(patched) or looks_like_broken_opening(patched):
        return {
            "status": "verification_failed",
            "message": "自动修复后的正文格式或开头仍未通过质量验收，已停止覆盖原文。建议使用单项修复或手动编辑。",
            "fixed": False,
            "risk_count": len(must_fix_reports),
            "content": old_content,
            "word_count": len(old_content),
        }

    aggregate_verification = verify_single_risk_fix(
        chapter_num,
        {
            "category": "一键修复风险集合",
            "severity": "must_fix",
            "message": risk_patch_required,
            "suggestion": "必须逐项修复这些风险，且不能破坏标题、开头、段落格式和正文质量。",
        },
        patched,
        min_words,
        max_words,
    )
    if not aggregate_verification.get("passed"):
        return {
            "status": "verification_failed",
            "message": f"一键修复未通过质量验收：{aggregate_verification.get('reason', '') or '仍存在风险'}。已停止覆盖原文，右侧风险不会被关闭。",
            "fixed": False,
            "risk_count": len(must_fix_reports),
            "content": old_content,
            "word_count": len(old_content),
            "verification": aggregate_verification,
        }

    if patched.strip() == old_content.strip():
        return {
            "status": "no_patch",
            "message": "模型没有生成有效修订，建议进入手动编辑。",
            "fixed": False,
            "risk_count": len(must_fix_reports),
        }

    novel_title = state.get("novel_title", "")
    emit("正在保存修复后的章节版本...", "memory")
    save_chapter(chapter_num, patched, novel_title, novel_id)
    bible.add_chapter_version(chapter_num, "risk_auto_fix", patched, f"一键修复本章风险，处理 {len(must_fix_reports)} 条 must_fix 风险")
    if patched.strip() != old_content.strip():
        bible.add_patch_record(
            chapter_num=chapter_num,
            edit_round=max(1, int(state.get("edit_count", 0) or 0)),
            patch_index=999,
            target_text="整章风险修订兜底",
            instruction=risk_patch_required,
            replacement_text="已生成完整修订章节，详见 risk_auto_fix 版本。",
            success=True,
            reason="局部补丁未命中，已采用整章风险修订兜底",
        )

    if chapter_num in {state.get("current_chapter", 1), state.get("current_chapter", 1) - 1}:
        state["chapter_content"] = patched
    save_checkpoint(state, novel_title, novel_id)
    set_chapter_publish_status(state, chapter_num, "needs_review", "章节已一键修复风险，需要重新检查发布包")
    for report in must_fix_reports:
        try:
            bible.close_consistency_report(int(report.get("id")))
        except Exception:
            pass

    bible.add_consistency_report(
        chapter_num=chapter_num,
        review_round=int(state.get("edit_count", 0) or 0) + 1,
        severity="pass",
        category="一键修复",
        message=f"已自动应用风险修复，原约 {len(old_content)} 字，修复后约 {len(patched)} 字。",
        suggestion="建议人工快速复读本章，并刷新风险报告确认是否还需处理。",
        status="closed",
    )

    return {
        "status": "success",
        "message": "本章风险已尝试自动修复，并保存为新版本。",
        "fixed": True,
        "risk_count": len(must_fix_reports),
        "chapter_num": chapter_num,
        "content": patched,
        "word_count": len(patched),
    }

@app.post("/api/chapter/{chapter_num}/auto_fix_risks")
async def auto_fix_chapter_risks(chapter_num: int):
    task_id = start_background_task(
        "auto_fix_risks",
        lambda progress: run_auto_fix_chapter_risks(chapter_num, progress),
    )
    return {"status": "queued", "task_id": task_id, "message": "风险修复任务已提交"}

# ================= 作家工作台 API (桥接到 MCP) =================

@app.get("/api/workbench/inspirations")
async def get_inspirations():
    return {"result": get_current_story_bible().get_pending_inspirations()}

@app.post("/api/workbench/inspirations")
async def add_inspiration(req: InspirationReq):
    import requests
    res = requests.post(f"{MCP_SERVER_URL}/inspirations", json={"novel_id": get_state_novel_id(load_checkpoint()), "content": req.content, "tags": req.tags})
    return res.json()

@app.post("/api/workbench/plot_hooks")
async def add_plot_hook(req: PlotHookReq):
    import requests
    res = requests.post(f"{MCP_SERVER_URL}/plot_hooks", json={"novel_id": get_state_novel_id(load_checkpoint()), "content": req.content, "target_chapter": req.target_chapter})
    return res.json()

@app.get("/api/workbench/world_rules")
async def get_world_rules():
    return {"result": get_current_story_bible().get_world_rules()}

@app.post("/api/workbench/world_rules")
async def add_world_rule(req: WorldRuleReq):
    import requests
    res = requests.post(f"{MCP_SERVER_URL}/world_rules", json={"novel_id": get_state_novel_id(load_checkpoint()), "category": req.category, "rule_text": req.rule_text})
    return res.json()

@app.get("/api/workbench/entity_cards")
async def get_entity_cards(card_type: str = ""):
    return {"result": get_current_story_bible().get_entity_cards(card_type)}


@app.post("/api/workbench/refresh")
async def refresh_workbench():
    """工作台一键刷新：拉取所有 entity_cards / pending_extractions / 风格档案。

    前端 `refreshNovelOverview` 和 `refreshCharacterRelations` / `refreshEntityCards`
    都依赖这个端点一次性拿到所有数据。
    """
    import requests as _req

    try:
        bible = get_current_story_bible()
        novel_id = bible.novel_id

        # 1) entity_cards（从 bible / mcp 取）
        try:
            cards = bible.get_entity_cards() or []
        except Exception:
            cards = []

        # 2) pending_extractions（mcp 端）
        pending = []
        try:
            r = _req.get(f"{MCP_SERVER_URL}/pending_extractions", params={"novel_id": novel_id}, timeout=3)
            if r.ok:
                pending = r.json().get("result", []) or []
        except Exception:
            pass

        # 3) 风格档案
        try:
            styles = bible.get_style_profiles() or []
        except Exception:
            styles = []

        return {
            "status": "success",
            "result": {
                "cards": cards,
                "pending_extractions": pending,
                "style_profiles": styles,
                "novel_id": novel_id,
            },
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "detail": str(e), "result": {"cards": [], "pending_extractions": [], "style_profiles": []}}

@app.post("/api/workbench/entity_cards")
async def save_entity_card(req: EntityCardReq):
    return get_current_story_bible().save_entity_card(req.card_type, req.name, req.fields, req.note)


@app.post("/api/workbench/extract_entities_from_chapter/{chapter_num}")
async def extract_entities_from_chapter(chapter_num: int):
    """从指定章节正文里调 LLM 提取实体（角色/物品/势力/地点），批量入库为 entity_card。

    跳过已存在同名同类型的卡，避免重复。
    """
    from models import call_llm, _strip_think_blocks
    from prompts import CHAPTER_EXTRACT_ENTITIES_SYSTEM, CHAPTER_EXTRACT_ENTITIES_PROMPT
    import json as _json

    state = load_checkpoint()
    if not state:
        raise HTTPException(status_code=404, detail="作品不存在")

    chapter_content = (state.get("chapter_content") or "").strip()
    if not chapter_content:
        # 尝试从 checkpoint 的 current_chapter 拿
        if state.get("current_chapter") != chapter_num:
            raise HTTPException(
                status_code=400,
                detail=f"当前章节正文为空，且 current_chapter={state.get('current_chapter')} 与请求的 {chapter_num} 不一致",
            )

    if len(chapter_content) < 200:
        raise HTTPException(status_code=400, detail="章节正文过短（<200字），跳过提取")

    bible = get_current_story_bible()
    existing_cards = bible.get_entity_cards() or []
    # 简化展示给 LLM 的已存在列表（避免 prompt 过长）
    existing_summary = _json.dumps(
        [{"type": c.get("card_type"), "name": c.get("name")} for c in existing_cards],
        ensure_ascii=False,
    )

    prompt = CHAPTER_EXTRACT_ENTITIES_PROMPT.format(
        chapter_content=chapter_content[:6000],
        existing_cards=existing_summary[:2000],
    )
    raw = call_llm(
        role="extractor",
        system_prompt=CHAPTER_EXTRACT_ENTITIES_SYSTEM,
        prompt=prompt,
        temperature=0.2,
        max_tokens=2500,
    )
    if not raw:
        raise HTTPException(status_code=503, detail="模型未返回任何结果")

    # 解析 JSON
    text = _strip_think_blocks(raw)
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = "\n".join(text.split("\n")[:-1])
        text = text.strip()

    try:
        data = _json.loads(text)
    except Exception as e:
        # 尝试用 app 内置的 parse_llm_json_object
        try:
            data = parse_llm_json_object(text)
        except Exception:
            raise HTTPException(
                status_code=500,
                detail=f"提取结果 JSON 解析失败: {e}。原文片段: {text[:200]}",
            )

    if not isinstance(data, dict):
        data = {}

    # 收集已存在卡名（避免重复入库）
    existing_keys = {(c.get("card_type"), c.get("name")) for c in existing_cards}

    created = []
    skipped = []
    card_type_map = {
        "characters": "character",
        "items": "item",
        "factions": "faction",
        "locations": "location",
    }
    for json_key, card_type in card_type_map.items():
        for entity in data.get(json_key, []) or []:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name", "")).strip()
            if not name:
                continue
            if (card_type, name) in existing_keys:
                skipped.append({"type": card_type, "name": name, "reason": "已存在"})
                continue
            fields = entity.get("fields", {}) or {}
            if not isinstance(fields, dict):
                fields = {"备注": str(fields)}
            # 角色额外存 gender 到 fields
            if card_type == "character" and entity.get("gender"):
                fields = {"gender": entity.get("gender"), **fields}
            try:
                result = bible.save_entity_card(card_type, name, fields, note=f"自动提取自第 {chapter_num} 章")
                if result and (isinstance(result, dict) and result.get("result") != "error"):
                    created.append({"type": card_type, "name": name, "fields": fields})
                    existing_keys.add((card_type, name))
                else:
                    skipped.append({"type": card_type, "name": name, "reason": str(result)})
            except Exception as e:
                skipped.append({"type": card_type, "name": name, "reason": str(e)})

    return {
        "status": "success",
        "chapter_num": chapter_num,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "created": created,
        "skipped": skipped,
    }

@app.delete("/api/workbench/entity_cards/{card_type}/{name}")
async def delete_entity_card(card_type: str, name: str):
    return get_current_story_bible().delete_entity_card(card_type, name)

@app.get("/api/workbench/style_profiles")
async def get_style_profiles():
    return {"result": get_current_story_bible().get_style_profiles()}

@app.post("/api/workbench/style_profiles/analyze")
async def analyze_style_profile(req: StyleProfileAnalyzeReq):
    try:
        from models import call_llm
        from prompts import STYLE_FINGERPRINT_SYSTEM, STYLE_FINGERPRINT_PROMPT

        name = req.name.strip() or "未命名文风"
        sample_text = req.sample_text.strip()
        if len(sample_text) < 80:
            raise HTTPException(status_code=400, detail="文风样本太短，建议至少 80 字")

        raw_res = call_llm(
            role="planner",
            system_prompt=STYLE_FINGERPRINT_SYSTEM,
            prompt=STYLE_FINGERPRINT_PROMPT.format(sample_text=sample_text),
            temperature=0.2,
            max_tokens=4096,
        )
        fingerprint = parse_llm_json_object(raw_res)
        if not fingerprint.get("writer_instruction"):
            raise HTTPException(status_code=500, detail="模型未返回可用的文风约束")

        saved = get_current_story_bible().save_style_profile(
            name=name,
            sample_text=sample_text,
            fingerprint=fingerprint,
            is_default=req.is_default,
        )
        if saved.get("result") == "error":
            raise HTTPException(status_code=500, detail=saved.get("error", "文风保存失败"))
        return {"status": "success", "result": saved.get("result"), "fingerprint": fingerprint}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/workbench/style_profiles/{profile_id}/default")
async def set_default_style_profile(profile_id: int):
    return get_current_story_bible().set_default_style_profile(profile_id)

@app.delete("/api/workbench/style_profiles/{profile_id}")
async def delete_style_profile(profile_id: int):
    return get_current_story_bible().delete_style_profile(profile_id)

@app.get("/api/workbench/pending_extractions")
async def get_pending_extractions():
    import requests
    res = requests.get(f"{MCP_SERVER_URL}/pending_extractions", params={"novel_id": get_state_novel_id(load_checkpoint())})
    return res.json()

@app.post("/api/workbench/approve_extraction/{item_id}")
async def approve_extraction(item_id: int):
    import requests
    res = requests.post(f"{MCP_SERVER_URL}/pending_extractions/approve/{item_id}", params={"novel_id": get_state_novel_id(load_checkpoint())})
    return res.json()

@app.post("/api/workbench/approve_extraction_to_card/{item_id}")
async def approve_extraction_to_card(item_id: int, req: ApproveExtractionToCardReq):
    import requests
    res = requests.post(
        f"{MCP_SERVER_URL}/pending_extractions/approve_to_card/{item_id}",
        json={
            "novel_id": get_state_novel_id(load_checkpoint()),
            "card_type": req.card_type,
            "name": req.name,
            "fields": req.fields,
            "note": req.note,
        }
    )
    return res.json()

@app.get("/api/workbench/ai_review/{chapter_num}")
async def get_ai_review(chapter_num: int):
    import requests
    res = requests.get(f"{MCP_SERVER_URL}/ai_reviews/{chapter_num}", params={"novel_id": get_state_novel_id(load_checkpoint())})
    return res.json()


@app.post("/api/workbench/critical_review/{chapter_num}")
async def critical_review(chapter_num: int):
    """批判式审稿：用挑剔编辑视角对当前章节挑刺，专门压制 AI 写作的典型病灶
    （永动机升级 / 配角工具人 / 主角被动成长 / 战斗密度过高 / 对话同质化 / 环境符号化 / 余韵缺失）。"""
    from models import call_llm
    from prompts import CRITICAL_REVIEW_PROMPT
    state = load_checkpoint()
    if not state:
        raise HTTPException(status_code=400, detail="未找到存档状态")
    chapter_content = read_chapter_from_state(state, chapter_num)
    if not chapter_content:
        raise HTTPException(status_code=404, detail=f"第 {chapter_num} 章内容为空")
    try:
        review = call_llm(
            role="editor",
            system_prompt="你是一位眼光毒辣的网文资深编辑，只挑刺、不夸赞、不给建议。用犀利、直接、不留情面的语气。",
            prompt=CRITICAL_REVIEW_PROMPT.format(chapter_content=chapter_content),
            temperature=0.6,
            max_tokens=2000,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"批判审稿失败：{e}")
    return {"status": "success", "chapter_num": chapter_num, "result": review}


@app.post("/api/workbench/chapter_type_detect")
async def chapter_type_detect(req: dict):
    """章节类型多样性检测：分析最近 N 章的"战斗/对话/独白/日常/设定/情感"占比
    提示"连续 5 章战斗" / "独白过少" / "日常缺位"等风险"""
    from models import call_llm
    from prompts import CHAPTER_TYPE_PROMPT
    import json as _json
    n = int(req.get("n") or 10)
    state = load_checkpoint()
    if not state:
        raise HTTPException(status_code=400, detail="未找到存档状态")
    cur_ch = state.get("current_chapter", 0)
    completed = state.get("completed_count", max(0, cur_ch - 1))
    if completed <= 0:
        raise HTTPException(status_code=400, detail="暂无已完成章节")
    n = min(n, completed)
    chapters_text = []
    for i in range(max(1, completed - n + 1), completed + 1):
        content = read_chapter_from_state(state, i)
        if content:
            # 每章只取前 1500 字（避免 prompt 过长）
            chapters_text.append(f"### 第 {i} 章\n{content[:1500]}")
    if not chapters_text:
        raise HTTPException(status_code=400, detail="章节内容为空")
    try:
        raw = call_llm(
            role="planner",
            system_prompt="你是网文类型分析师，严格按要求输出 JSON。",
            prompt=CHAPTER_TYPE_PROMPT.format(chapters_content="\n\n".join(chapters_text)),
            temperature=0.3,
            max_tokens=2500,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"章节类型检测失败：{e}")
    # 解析 JSON（处理可能的 markdown 包裹）
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = _json.loads(raw)
    except Exception:
        # 提取 JSON 子串
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            raise HTTPException(status_code=500, detail=f"LLM 返回非 JSON：{raw[:200]}")
        data = _json.loads(m.group(0))
    return {"status": "success", "n": n, "result": data}


@app.post("/api/workbench/outline_coverage")
async def outline_coverage_check(req: dict):
    """大纲覆盖度检查：对比"大纲每章的剧情功能" vs "已写章节正文"，返回每章匹配度 + 跳过的章节"""
    from models import call_llm
    from prompts import OUTLINE_COVERAGE_PROMPT
    import json as _json
    n = int(req.get("n") or 20)
    state = load_checkpoint()
    if not state:
        raise HTTPException(status_code=400, detail="未找到存档状态")
    cur_ch = state.get("current_chapter", 0)
    completed = state.get("completed_count", max(0, cur_ch - 1))
    if completed <= 0:
        raise HTTPException(status_code=400, detail="暂无已完成章节")
    n = min(n, completed)
    # 1. 取大纲（每章的 purpose + main_event + title）
    full_outline = state.get("full_outline", {}) or {}
    outline_chapters = full_outline.get("chapters", []) or []
    if not outline_chapters:
        raise HTTPException(status_code=400, detail="大纲未生成 chapters 列表，无法做对照")
    # 2. 取已写章节正文
    chapters_text = []
    for i in range(max(1, completed - n + 1), completed + 1):
        content = read_chapter_from_state(state, i)
        if content:
            chapters_text.append(f"第 {i} 章：{content[:1500]}")
    if not chapters_text:
        raise HTTPException(status_code=400, detail="章节内容为空")
    # 3. 取大纲文本（限制长度）
    outline_text = format_full_outline_text(full_outline)
    if len(outline_text) > 6000:
        outline_text = outline_text[:6000] + "\n...(已截断)..."
    try:
        raw = call_llm(
            role="planner",
            system_prompt="你是网文大纲审核员，严格按要求输出 JSON。",
            prompt=OUTLINE_COVERAGE_PROMPT.format(
                outline_text=outline_text,
                chapters_content="\n\n".join(chapters_text),
            ),
            temperature=0.3,
            max_tokens=3000,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"大纲对照失败：{e}")
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = _json.loads(raw)
    except Exception:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            raise HTTPException(status_code=500, detail=f"LLM 返回非 JSON：{raw[:200]}")
        data = _json.loads(m.group(0))
    return {"status": "success", "n": n, "result": data}


@app.post("/api/workbench/anti_ai_flavor/{chapter_num}")
async def anti_ai_flavor(chapter_num: int):
    """反 AI 味检测：识别本章的 AI 套话（眼神深邃/嘴角勾起等），给出改写建议"""
    from models import call_llm
    from prompts import ANTI_AI_FLAVOR_PROMPT
    import json as _json
    state = load_checkpoint()
    if not state:
        raise HTTPException(status_code=400, detail="未找到存档状态")
    content = read_chapter_from_state(state, chapter_num)
    if not content:
        raise HTTPException(status_code=404, detail=f"第 {chapter_num} 章内容为空")
    if len(content) > 6000:
        content = content[:6000] + "\n...(已截断，仅分析前 6000 字)..."
    try:
        raw = call_llm(
            role="editor",
            system_prompt="你是反 AI 味编辑，专找网文常见套话（眼神深邃/嘴角勾起等），并给出改写方向。",
            prompt=ANTI_AI_FLAVOR_PROMPT.format(chapter_content=content),
            temperature=0.4,
            max_tokens=2000,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"反 AI 味检测失败：{e}")
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = _json.loads(raw)
    except Exception:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            raise HTTPException(status_code=500, detail=f"LLM 返回非 JSON：{raw[:200]}")
        data = _json.loads(m.group(0))
    return {"status": "success", "chapter_num": chapter_num, "result": data}

# ================= 新增功能 API =================

@app.post("/api/save_chapter")
async def save_chapter_edit(req: SaveChapterReq):
    """保存编辑后的章节内容"""
    try:
        state = load_checkpoint()
        if not state:
            raise HTTPException(status_code=400, detail="未找到存档状态")
        
        novel_title = state.get("novel_title", "")
        novel_id = get_state_novel_id(state)
        
        try:
            old_content = read_chapter_from_state(state, req.chapter_num)
        except HTTPException:
            old_content = ""
        normalized_content = normalize_chapter_paragraphs(req.content)
        normalized_content = ensure_chapter_title(normalized_content, req.chapter_num, old_content)

        # 保存编辑内容到文件
        save_chapter(req.chapter_num, normalized_content, novel_title, novel_id)
        StoryBible(novel_id).add_chapter_version(req.chapter_num, "manual_edit", normalized_content, "用户手动编辑保存")
        
        # 更新checkpoint（如果这是当前章节）
        if req.chapter_num in {state.get("current_chapter", 1), state.get("current_chapter", 1) - 1}:
            state["chapter_content"] = normalized_content
        save_checkpoint(state, novel_title, novel_id)
        
        return {
            "status": "success",
            "message": f"第 {req.chapter_num} 章已保存",
            "word_count": len(normalized_content),
            "content": normalized_content
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/finish_novel")
async def finish_novel():
    """完成小说，归档当前作品并准备开始新书"""
    try:
        state = load_checkpoint()
        if not state:
            return {
                "status": "success",
                "message": "无存档数据，已准备好开始新书"
            }
        
        novel_id = get_state_novel_id(state)
        checkpoint_path = state.get("_checkpoint_path") or get_checkpoint_path(state.get("novel_title", ""), novel_id)
        state["completed"] = True
        state["archived"] = True
        state["updated_at"] = time.time()
        state.pop("_checkpoint_path", None)
        state.pop("_novel_dir", None)
        write_json_file(checkpoint_path, state)
        clear_current_novel_id(novel_id)
        
        return {
            "status": "success",
            "message": "已完成本书并归档，准备开始新作品。"
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 网文创作智能体 Web 服务启动...")
    print("✨ 请在浏览器访问：http://127.0.0.1:8050")
    print("=" * 60)
    uvicorn.run("app:app", host="127.0.0.1", port=8050, reload=True)
