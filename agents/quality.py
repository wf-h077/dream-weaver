"""章节质量引擎节点

职责：
1. 在正文写作前生成“本章戏剧卡”
2. 在正文写作后评估商业可读性
3. 对低分章节自动做一次增强重写
"""
from __future__ import annotations

import json
import re
from typing import Any

from config import get_chapter_word_range
from models import call_llm
from prompts import (
    DRAMA_CARD_PROMPT,
    DRAMA_CARD_SYSTEM,
    QUALITY_ASSESSOR_PROMPT,
    QUALITY_ASSESSOR_SYSTEM,
    QUALITY_ENHANCER_PROMPT,
    QUALITY_ENHANCER_SYSTEM,
    get_story_phase,
)
from state import NovelState
from task_progress import report_progress
from agents.text_quality import detect_repetition_issues, format_repetition_issues, normalize_chapter_text


QUALITY_PASS_SCORE = 7.2
QUALITY_KEY_MIN_SCORE = 6.5
QUALITY_KEY_FIELDS = [
    "opening_hook",
    "conflict_density",
    "payoff",
    "protagonist_agency",
    "ending_hook",
    "promise_payoff",
]


def strip_json_fence(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if text.startswith("```json"):
        text = text[7:].strip()
    elif text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return text.strip()


def parse_quality_json(raw_text: str) -> dict[str, Any]:
    text = strip_json_fence(raw_text)
    try:
        data = json.loads(text)
    except Exception:
        text = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            data = json.loads(text)
        except Exception:
            return {
                "overall_score": 0,
                "scores": {},
                "verdict": "enhance",
                "main_weaknesses": ["质量评估 JSON 解析失败"],
                "enhancement_instructions": "重点增强开篇钩子、冲突密度、主角主动选择、爽点兑现和章末期待。",
            }
    if not isinstance(data, dict):
        return {
            "overall_score": 0,
            "scores": {},
            "verdict": "enhance",
            "main_weaknesses": ["质量评估结果格式异常"],
            "enhancement_instructions": "重点增强章节可读性和追读动力。",
        }
    data.setdefault("scores", {})
    data.setdefault("main_weaknesses", [])
    data.setdefault("enhancement_instructions", "")
    return data


def should_enhance(report: dict[str, Any]) -> bool:
    try:
        overall = float(report.get("overall_score", 0) or 0)
    except (TypeError, ValueError):
        overall = 0
    scores = report.get("scores") if isinstance(report.get("scores"), dict) else {}
    key_low = False
    for key in QUALITY_KEY_FIELDS:
        try:
            if float(scores.get(key, 10) or 0) < QUALITY_KEY_MIN_SCORE:
                key_low = True
                break
        except (TypeError, ValueError):
            key_low = True
            break
    return report.get("verdict") == "enhance" or overall < QUALITY_PASS_SCORE or key_low


def extract_pattern_name(pattern_card: str) -> str:
    first_line = next((line.strip() for line in (pattern_card or "").splitlines() if line.strip()), "")
    if first_line.startswith("【章节样板："):
        return first_line.replace("【章节样板：", "").replace("】", "").strip()
    return ""


def design_chapter_drama_card(state: NovelState) -> dict:
    chapter_num = state["current_chapter"]
    num_chapters = state.get("num_chapters", 100)
    report_progress(f"正在设计第 {chapter_num} 章戏剧卡...", "planning")
    print(f"\n🎬 [质量引擎] 正在设计第{chapter_num}章戏剧卡...")

    prompt = DRAMA_CARD_PROMPT.format(
        chapter_num=chapter_num,
        global_outline=state.get("global_outline", ""),
        story_so_far=state.get("story_so_far", "目前是第一章，故事才刚刚开始。"),
        chapter_outline=state.get("chapter_outline", ""),
        chapter_pattern_card=state.get("chapter_pattern_card", ""),
        character_voice_guide=state.get("character_voice_guide", ""),
        reader_promise_guide=state.get("reader_promise_guide", ""),
        bible_context=state.get("bible_context", "暂无"),
        story_phase=get_story_phase(chapter_num, num_chapters),
    )
    drama_card = call_llm(
        role="planner",
        system_prompt=DRAMA_CARD_SYSTEM,
        prompt=prompt,
        temperature=0.55,
        max_tokens=4096,
    )
    drama_card = (drama_card or "").strip()
    if not drama_card:
        drama_card = "本章必须具备明确冲突、主角主动选择、信息增量、爽点兑现和章末钩子。"

    print("  ✅ 本章戏剧卡已生成")
    report_progress("本章戏剧卡已生成", "planning")
    return {"chapter_drama_card": drama_card}


def quality_gate_chapter(state: NovelState) -> dict:
    chapter_num = state["current_chapter"]
    chapter_content = state.get("chapter_content", "")
    target_words = state.get("chapter_target_words", 2000)
    min_words, max_words = get_chapter_word_range(target_words)

    report_progress(f"正在评估第 {chapter_num} 章可读性质量...", "review")
    print(f"\n📈 [质量引擎] 正在评估第{chapter_num}章商业可读性...")

    raw_report = call_llm(
        role="editor",
        system_prompt=QUALITY_ASSESSOR_SYSTEM,
        prompt=QUALITY_ASSESSOR_PROMPT.format(
            chapter_num=chapter_num,
            chapter_drama_card=state.get("chapter_drama_card", ""),
            chapter_pattern_card=state.get("chapter_pattern_card", ""),
            character_voice_guide=state.get("character_voice_guide", ""),
            reader_promise_guide=state.get("reader_promise_guide", ""),
            chapter_outline=state.get("chapter_outline", ""),
            chapter_content=chapter_content,
        ),
        temperature=0.1,
        max_tokens=4096,
    )
    report = parse_quality_json(raw_report)
    report["pattern_name"] = extract_pattern_name(state.get("chapter_pattern_card", ""))
    report["voice_guide_used"] = bool(state.get("character_voice_guide"))
    report["promise_guide_used"] = bool(state.get("reader_promise_guide"))
    repetition_issues = detect_repetition_issues(chapter_content)
    if repetition_issues:
        report["repetition_issues"] = repetition_issues
        report.setdefault("main_weaknesses", [])
        for issue in repetition_issues[:4]:
            text = f"{issue.get('category')}：{issue.get('message')}"
            if text not in report["main_weaknesses"]:
                report["main_weaknesses"].append(text)
        if any(item.get("severity") == "must_fix" for item in repetition_issues):
            report["verdict"] = "enhance"
    enhanced = False

    if should_enhance(report) and not state.get("quality_enhanced_once", False):
        report_progress("章节可读性不足，正在自动增强重写...", "writing")
        print("  ⚠️ 质量评分不足，执行一次增强重写")
        improved = call_llm(
            role="writer",
            system_prompt=QUALITY_ENHANCER_SYSTEM,
            prompt=QUALITY_ENHANCER_PROMPT.format(
                chapter_num=chapter_num,
                min_words=min_words,
                max_words=max_words,
                chapter_drama_card=state.get("chapter_drama_card", ""),
                chapter_pattern_card=state.get("chapter_pattern_card", ""),
                character_voice_guide=state.get("character_voice_guide", ""),
                reader_promise_guide=state.get("reader_promise_guide", ""),
                chapter_outline=state.get("chapter_outline", ""),
                quality_report=json.dumps(report, ensure_ascii=False, indent=2),
                repetition_report=format_repetition_issues(repetition_issues),
                chapter_content=chapter_content,
            ),
            temperature=0.72,
            max_tokens=8192,
        )
        improved = (improved or "").strip()
        improved = normalize_chapter_text(improved, dedupe=True)
        if len(improved) >= max(300, int(len(chapter_content) * 0.6)):
            chapter_content = improved
            enhanced = True
        else:
            report.setdefault("main_weaknesses", []).append("增强重写结果过短，已保留原文")

    report["enhanced"] = enhanced
    if enhanced:
        report["verdict_after_gate"] = "enhanced"
        print("  ✅ 已完成质量增强")
        report_progress("章节质量增强完成", "review")
        return {
            "chapter_content": chapter_content,
            "quality_report": report,
            "quality_enhanced_once": True,
        }

    print("  ✅ 质量门禁完成")
    report_progress("章节质量门禁完成", "review")
    return {
        "quality_report": report,
        "quality_enhanced_once": state.get("quality_enhanced_once", False),
    }
