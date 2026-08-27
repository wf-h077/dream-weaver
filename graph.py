"""LangGraph 工作流定义

构建创作流程的有向循环图：
  recall → plan_chapter → write → review → (循环或通过) → update_memory
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from concurrent.futures import ThreadPoolExecutor
from langgraph.graph import StateGraph, END
from state import NovelState
from memory import StoryBible, DEFAULT_NOVEL_ID
from agents.planner import generate_outline, plan_chapter
from agents.writer import write_chapter, ensure_chapter_word_count
from agents.brief_composer import compose_chapter_brief
from agents.quality import quality_gate_chapter
from agents.editor import review_chapter
from agents.patcher import patch_chapter
from task_progress import report_progress
from config import get_chapter_word_range


# 全局并发池：summarize 节点内的 3 个 LLM 调用复用
_SUMMARIZE_EXECUTOR = ThreadPoolExecutor(
    max_workers=3,
    thread_name_prefix="summarize_llm",
)

# 全局故事宝典实例
story_bible = StoryBible()

# ── 辅助节点 ──

def get_story_bible(state: NovelState) -> StoryBible:
    return StoryBible(state.get("novel_id", DEFAULT_NOVEL_ID))


def format_quality_summary(quality_report: dict | None) -> str:
    if not isinstance(quality_report, dict) or not quality_report:
        return ""
    score = quality_report.get("overall_score", "未知")
    verdict = quality_report.get("verdict_after_gate") or quality_report.get("verdict", "")
    enhanced = "是" if quality_report.get("enhanced") else "否"
    weaknesses = quality_report.get("main_weaknesses") or []
    if isinstance(weaknesses, list):
        weakness_text = "；".join(str(item) for item in weaknesses[:3] if item)
    else:
        weakness_text = str(weaknesses)
    lines = [
        "【章节质量门禁】",
        f"综合评分：{score}",
        f"质量结论：{verdict or '未标注'}",
        f"是否自动增强：{enhanced}",
    ]
    if weakness_text:
        lines.append(f"主要弱点：{weakness_text}")
    return "\n".join(lines)


def extract_pattern_name(pattern_card: str) -> str:
    first_line = next((line.strip() for line in (pattern_card or "").splitlines() if line.strip()), "")
    if first_line.startswith("【章节样板："):
        return first_line.replace("【章节样板：", "").replace("】", "").strip()
    return ""

def recall_node(state: NovelState) -> dict:
    """从 ChromaDB 检索与当前章节相关的设定信息"""
    chapter_num = state["current_chapter"]
    bible = get_story_bible(state)
    report_progress(f"正在检索第 {chapter_num} 章相关设定...", "recall")
    print(f"\n📖 [记忆管理] 正在检索第{chapter_num}章相关设定...")

    # 从大纲提取纯名词关键词用于检索（避免由于大纲太长导致检索失焦）
    query = bible.extract_keywords(state.get('chapter_outline', ''))
    if not query:
        query = state.get('global_outline', '')[:500]

    context = bible.recall(query)

    print(f"  ✅ 检索完成: (关键词: {query})")
    report_progress("记忆检索完成", "recall")
    return {"bible_context": context}


def update_memory_node(state: NovelState) -> dict:
    """将审核通过的章节内容存入 ChromaDB"""
    chapter_num = state["current_chapter"]
    bible = get_story_bible(state)
    report_progress(f"正在把第 {chapter_num} 章写入故事宝典...", "memory")
    print(f"\n💾 [记忆管理] 正在更新故事宝典...")

    bible.update_from_chapter(chapter_num, state["chapter_content"])

    # 将完成的章节加入列表
    completed = list(state.get("completed_chapters", []))
    completed.append(state["chapter_content"])

    return {
        "completed_chapters": completed,
    }

def summarize_node(state: NovelState) -> dict:
    """生成本章摘要，追加到前情摘要中，维护长短期记忆。

    三个 LLM 调用（复盘 / 里程碑压缩 / 状态提取）并发执行。
    原串行耗时 ≈ 3 * t_LLM，并行后 ≈ max(t_LLM) ≈ t_LLM。
    """
    chapter_num = state["current_chapter"]
    bible = get_story_bible(state)
    report_progress(f"正在生成第 {chapter_num} 章摘要与复盘...", "summary")
    print(f"\n📝 [记忆管理] 正在生成第{chapter_num}章前情摘要...")

    chapter_summary = bible.summarize_chapter(state["chapter_content"])

    recent_summaries = state.get("recent_summaries", [])
    global_synopsis = state.get("global_synopsis", "")

    # 防止 recent_summaries 为 None
    if not recent_summaries:
        recent_summaries = []

    new_recent = list(recent_summaries)
    new_recent.append(f"[第{chapter_num}章]:\n{chapter_summary}")
    # 关键：限制 recent_summaries 最多保留 RECENT_SUMMARIES_LIMIT 项
    # 超过的项会被 pop 出来追加到 global_synopsis 中（让 LLM 在里程碑节点再次压缩）
    RECENT_SUMMARIES_LIMIT = 8
    if len(new_recent) > RECENT_SUMMARIES_LIMIT:
        overflow = len(new_recent) - RECENT_SUMMARIES_LIMIT
        # 移出最早的几项（不立即调 LLM，由 need_synopsis_update 触发里程碑合并）
        _ = new_recent[:overflow]
        new_recent = new_recent[overflow:]
        print(f"  ✂️ recent_summaries 截断: 保留 {len(new_recent)} 项（丢弃 {overflow} 项最早的）")

    from models import call_llm
    from prompts import SYNOPSIS_UPDATE_PROMPT, STATUS_EXTRACTOR_PROMPT, REFLECTION_PROMPT

    # 准备 3 个 LLM 任务
    quality_summary = format_quality_summary(state.get("quality_report", {}))
    pattern_name = extract_pattern_name(state.get("chapter_pattern_card", ""))
    if quality_summary and pattern_name:
        quality_summary = f"{quality_summary}\n使用样板：{pattern_name}"

    # 任务 1: AI 复盘
    def _call_ai_reflection():
        print(f"  🧠 正在生成本章 AI 复盘与走向建议...")
        result = call_llm(
            role="planner",
            system_prompt="你是一位资深的网文编辑，负责辅助作者复盘。",
            prompt=REFLECTION_PROMPT.format(chapter_content=state["chapter_content"]),
            temperature=0.7,
        )
        if quality_summary:
            result = f"{quality_summary}\n\n{result}"
        return result

    # 任务 2: 里程碑压缩（条件触发）
    def _call_synopsis_update():
        if len(new_recent) <= 8:
            return None
        # 注意：new_recent 已经被 pop（取最老），需要在这里再 pop 一次
        # 但因为 _call_ai_reflection 和 _call_status_extract 也用 new_recent，
        # 它们不能动 new_recent。所以这里用一个副本。
        return None  # 实际逻辑在下方

    # 任务 3: 状态提取
    def _call_status_extract():
        print(f"  🔍 正在提取本章角色及物品的状态变更...")
        old_status = bible.get_entity_status() or "{}"
        chapter_content = state.get("chapter_content", "")
        try:
            prompt = STATUS_EXTRACTOR_PROMPT.format(
                old_status_json=old_status,
                chapter_content=chapter_content,
            )
        except KeyError as e:
            print("STATUS_EXTRACTOR_PROMPT format KeyError:", e)
            print("state keys:", list(state.keys()))
            print("old_status:", repr(old_status))
            print("chapter_content:", repr(chapter_content))
            raise
        result = call_llm(
            role="extractor",
            system_prompt="你是一个精准提取角色状态的结构化解析器。",
            prompt=prompt,
            temperature=0.1,
        ).strip()
        # 清理 markdown 包裹
        if result.startswith("```json"):
            result = result[7:].strip()
        elif result.startswith("```"):
            result = result[3:].strip()
        if result.endswith("```"):
            result = result[:-3].strip()
        return result

    # ── 并发执行 3 个 LLM 任务 ──
    need_synopsis_update = len(new_recent) > 8

    def _call_synopsis_update_real():
        if not need_synopsis_update:
            return None
        # 取出最老的 summary（不修改 new_recent）
        local_recent = list(new_recent)
        oldest_summary = local_recent.pop(0)
        print(f"  🔄 正在将较老的章节融入全书大事件总览（里程碑构建）...")
        prompt = SYNOPSIS_UPDATE_PROMPT.format(
            global_synopsis=global_synopsis if global_synopsis else "（暂无全书总览）",
            old_summary=oldest_summary,
        )
        return call_llm(
            role="planner",
            system_prompt="你是一个负责维护小说全案设定的高级助手。",
            prompt=prompt,
            temperature=0.4,
        )

    futures = [
        _SUMMARIZE_EXECUTOR.submit(_call_ai_reflection),
        _SUMMARIZE_EXECUTOR.submit(_call_status_extract),
        _SUMMARIZE_EXECUTOR.submit(_call_synopsis_update_real),
    ]

    # 收集结果（按提交顺序：复盘, 状态, 里程碑）
    ai_reflection = futures[0].result()
    new_status_str = futures[1].result()
    new_global_synopsis = futures[2].result()  # 可能为 None

    if need_synopsis_update and new_global_synopsis is not None:
        global_synopsis = new_global_synopsis
    # 如果触发条件但 LLM 失败，保持旧 global_synopsis

    # 同步持久化到 bible
    bible.add_ai_review(chapter_num, ai_reflection)
    if new_status_str:
        bible.update_entity_status(new_status_str)
    latest_status = bible.get_entity_status()

    # 重新拼接 story_so_far
    new_so_far = ""
    if global_synopsis:
        new_so_far += f"【全书剧情大事件（压缩总结）】\n{global_synopsis}\n\n"

    if new_recent:
        new_so_far += "【近期详细剧情】\n"
        new_so_far += "\n\n".join(new_recent)

    if not new_so_far:
        new_so_far = "目前是第一章，故事刚刚开始。"

    print(f"  ✅ 摘要生成、面板更新与 AI 复盘完成")
    report_progress("摘要、角色状态与 AI 复盘已完成", "summary")
    return {
        "story_so_far": new_so_far,
        "global_synopsis": global_synopsis,
        "recent_summaries": new_recent,
        "structured_status": latest_status,
        "current_chapter": chapter_num + 1,   # 推进到下一章
    }


def should_revise(state: NovelState) -> str:
    """条件边：决定审核后是按哪种方式修改，还是继续"""
    res = state.get("review_result", "")
    if res == "approve":
        return "update_memory"
    elif res == "patch":
        return "patcher"
    elif res == "rewrite":
        return "writer"


def word_count_guard_node(state: NovelState) -> dict:
    """Final hard guard before memory/save: chapter must meet target length."""
    chapter_num = state["current_chapter"]
    target_words = state.get("chapter_target_words", 2000)
    min_words, max_words = get_chapter_word_range(target_words)
    chapter_content = state.get("chapter_content", "")
    if len(chapter_content) >= min_words:
        return {}

    report_progress(f"最终字数守门：正文低于 {min_words} 字，正在补写...", "writing")
    expanded = ensure_chapter_word_count(
        chapter_content,
        chapter_num=chapter_num,
        target_words=target_words,
        min_words=min_words,
        max_words=max_words,
    )
    if len(expanded) < min_words:
        raise RuntimeError(f"章节字数未达最低要求：当前约 {len(expanded)} 字，最低要求 {min_words} 字。请重试或适当降低目标字数。")
    return {"chapter_content": expanded}
        
    # 向前兼容 fallback
    if state.get("is_approved", False):
        return "update_memory"
    else:
        return "writer"


# ── 构建图 ──

def build_chapter_graph() -> StateGraph:
    """
    构建单章创作的 LangGraph 工作流。

    流程：recall → planner → writer → editor →（条件）→ update_memory → summarize
    """
    builder = StateGraph(NovelState)

    # 添加节点
    builder.add_node("recall", recall_node)
    builder.add_node("planner", plan_chapter)
    builder.add_node("brief_composer", compose_chapter_brief)  # 合并：替代原 pattern/voice/promise/drama_card
    builder.add_node("writer", write_chapter)
    builder.add_node("quality_gate", quality_gate_chapter)
    builder.add_node("editor", review_chapter)
    builder.add_node("patcher", patch_chapter)
    builder.add_node("word_count_guard", word_count_guard_node)
    builder.add_node("update_memory", update_memory_node)
    builder.add_node("summarize", summarize_node)

    # 添加边
    builder.set_entry_point("recall")
    builder.add_edge("recall", "planner")
    builder.add_edge("planner", "brief_composer")
    builder.add_edge("brief_composer", "writer")
    builder.add_edge("writer", "quality_gate")
    builder.add_edge("quality_gate", "editor")

    # 条件边：审核是否通过
    builder.add_conditional_edges(
        "editor",
        should_revise,
        {
            "update_memory": "word_count_guard",
            "writer": "writer",
            "patcher": "patcher"
        }
    )

    builder.add_edge("patcher", "editor")

    builder.add_edge("word_count_guard", "update_memory")
    builder.add_edge("update_memory", "summarize")
    builder.add_edge("summarize", END)

    return builder.compile()


def build_full_pipeline() -> StateGraph:
    """
    构建完整的创作流水线（含全书大纲生成）。

    流程：generate_outline → recall → planner → brief_composer → writer → editor → update_memory → summarize
    """
    builder = StateGraph(NovelState)

    # 添加节点
    builder.add_node("generate_outline", generate_outline)
    builder.add_node("recall", recall_node)
    builder.add_node("planner", plan_chapter)
    builder.add_node("brief_composer", compose_chapter_brief)  # 合并：替代原 pattern/voice/promise/drama_card
    builder.add_node("writer", write_chapter)
    builder.add_node("quality_gate", quality_gate_chapter)
    builder.add_node("editor", review_chapter)
    builder.add_node("patcher", patch_chapter)
    builder.add_node("word_count_guard", word_count_guard_node)
    builder.add_node("update_memory", update_memory_node)
    builder.add_node("summarize", summarize_node)

    # 边
    builder.set_entry_point("generate_outline")
    builder.add_edge("generate_outline", "recall")
    builder.add_edge("recall", "planner")
    builder.add_edge("planner", "brief_composer")
    builder.add_edge("brief_composer", "writer")
    builder.add_edge("writer", "quality_gate")
    builder.add_edge("quality_gate", "editor")

    builder.add_conditional_edges(
        "editor",
        should_revise,
        {
            "update_memory": "word_count_guard",
            "writer": "writer",
            "patcher": "patcher"
        }
    )

    builder.add_edge("patcher", "editor")

    builder.add_edge("word_count_guard", "update_memory")
    builder.add_edge("update_memory", "summarize")
    builder.add_edge("summarize", END)

    return builder.compile()
