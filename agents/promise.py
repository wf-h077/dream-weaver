"""Reader promise guide node.

The guide keeps every chapter honest about serialized reading momentum:
what the chapter should pay off, what it should escalate, and what promise it
should leave for the next chapter.
"""
from __future__ import annotations

import re
from typing import Any

from memory import DEFAULT_NOVEL_ID, StoryBible
from state import NovelState
from task_progress import report_progress


def _text(value: Any, limit: int = 140) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "、".join(str(item) for item in value if item)
    elif isinstance(value, dict):
        value = "；".join(f"{k}:{v}" for k, v in value.items() if v not in ("", None, [], {}))
    value = str(value).strip().replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    if len(value) > limit:
        return value[:limit].rstrip() + "..."
    return value


def _collect_outline_for_chapter(state: NovelState) -> str:
    chapter_num = int(state.get("current_chapter", 1) or 1)
    opening = state.get("opening_outline") or {}
    full = state.get("full_outline") or {}
    for source in (opening, full):
        chapters = source.get("chapters") if isinstance(source, dict) else []
        if not isinstance(chapters, list):
            continue
        for item in chapters:
            if not isinstance(item, dict):
                continue
            try:
                if int(item.get("chapter_num", 0) or 0) == chapter_num:
                    parts = [
                        item.get("purpose"),
                        item.get("core_event") or item.get("main_event"),
                        item.get("conflict"),
                        item.get("reader_hook"),
                        item.get("ending_hook"),
                        item.get("continuity_note"),
                    ]
                    return "；".join(_text(part, 100) for part in parts if part)
            except (TypeError, ValueError):
                continue
    return ""


def _extract_recent_open_promises(story_so_far: str) -> list[str]:
    if not story_so_far:
        return []
    keywords = ["伏笔", "约定", "承诺", "威胁", "秘密", "真相", "钩子", "期限", "危机", "追查"]
    lines = []
    for raw in re.split(r"[\n。；;]", story_so_far):
        line = raw.strip()
        if 8 <= len(line) <= 120 and any(key in line for key in keywords):
            lines.append(line)
    return lines[-4:]


def _extract_control_promises(state: NovelState) -> list[str]:
    controls = state.get("current_chapter_controls") or {}
    if not isinstance(controls, dict):
        return []
    keys = [
        ("free_instruction", "用户临时指导"),
        ("chapter_goal", "本章目标"),
        ("must_hooks", "必须处理伏笔"),
        ("ending_hook", "章末钩子"),
        ("notes", "备注"),
    ]
    items = []
    for key, label in keys:
        value = _text(controls.get(key), 100)
        if value:
            items.append(f"{label}：{value}")
    return items


def _extract_pending_hooks(state: NovelState) -> list[str]:
    chapter_num = int(state.get("current_chapter", 1) or 1)
    bible = StoryBible(state.get("novel_id", DEFAULT_NOVEL_ID))
    try:
        hooks = bible.get_pending_plot_hooks(chapter_num)
    except Exception:
        hooks = []
    result = []
    if isinstance(hooks, list):
        for item in hooks[:5]:
            if isinstance(item, dict):
                content = _text(item.get("content"), 100)
                target = item.get("target_chapter")
                if content:
                    result.append(f"第{target or chapter_num}章伏笔：{content}")
            else:
                content = _text(item, 100)
                if content:
                    result.append(content)
    return result


def _story_phase_label(chapter_num: int, total: int) -> str:
    if total <= 0:
        return "连载推进"
    progress = chapter_num / max(total, 1)
    if progress <= 0.1:
        return "开局留存：必须建立点击承诺、主角吸引力和第一波危机"
    if progress <= 0.4:
        return "发展攀升：必须让旧危机升级，同时兑现一个阶段性小爽点"
    if progress <= 0.75:
        return "高潮爆发：必须回收前文伏笔，制造更高代价和更强反转"
    if progress <= 0.9:
        return "终局压迫：必须减少新坑，集中处理主线终极矛盾"
    return "结局收束：必须回收承诺，给角色命运和主线结果交代"


def build_reader_promise_guide(state: NovelState) -> dict:
    chapter_num = int(state.get("current_chapter", 1) or 1)
    total = int(state.get("num_chapters", 100) or 100)
    report_progress(f"正在生成第 {chapter_num} 章读者期待台账...", "planning")

    chapter_outline = _text(state.get("chapter_outline"), 220)
    outline_plan = _collect_outline_for_chapter(state)
    control_promises = _extract_control_promises(state)
    pending_hooks = _extract_pending_hooks(state)
    recent_promises = _extract_recent_open_promises(state.get("story_so_far", ""))

    lines = [
        "【本章读者期待台账】",
        f"- 连载阶段：{_story_phase_label(chapter_num, total)}",
    ]
    if outline_plan:
        lines.append(f"- 蓝图承诺：{outline_plan}")
    if chapter_outline:
        lines.append(f"- 本章大纲承诺：{chapter_outline}")
    if control_promises:
        lines.append("- 用户指定期待：" + "；".join(control_promises[:4]))
    if pending_hooks:
        lines.append("- 待回收伏笔：" + "；".join(pending_hooks[:4]))
    elif recent_promises:
        lines.append("- 前文潜在期待：" + "；".join(recent_promises[:3]))
    else:
        lines.append("- 前文潜在期待：本章至少制造一个清晰问题，并在章末让读者知道下一章要看什么。")

    lines.extend(
        [
            "【执行规则】",
            "1. 本章开头 300 字内必须让读者知道当前压力、目标或悬念。",
            "2. 本章中段必须兑现一个小承诺：反击、发现、关系变化、能力进展或信息反转。",
            "3. 本章结尾必须留下一个具体可追读的问题，不能只写情绪口号。",
            "4. 如果回收旧伏笔，必须同时制造新的更高层级期待，维持连载牵引。",
        ]
    )
    guide = "\n".join(lines)
    print(f"  [读者期待] 已生成第{chapter_num}章承诺台账")
    report_progress("读者期待台账已生成", "planning")
    return {"reader_promise_guide": guide}
