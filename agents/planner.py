"""总策划智能体节点

职责：
1. 根据初始设定生成全书大纲
2. 为每一章生成细化大纲（含故事阶段意识）
"""
from models import call_llm
from prompts import PLANNER_SYSTEM, PLANNER_OUTLINE_PROMPT, PLANNER_CHAPTER_PROMPT, get_story_phase
from state import NovelState
from task_progress import report_progress


def build_opening_outline_hint(state: NovelState) -> str:
    text = state.get("opening_outline_text", "")
    if text:
        return f"\n\n【前10章强节奏开局规划（必须优先遵守）】\n{text}"
    return ""


def build_full_outline_hint(state: NovelState) -> str:
    text = state.get("full_outline_text", "")
    if text:
        return f"\n\n【用户确认的全书章节蓝图（必须作为全局大纲）】\n{text}"
    return ""


def build_chapter_controls_hint(state: NovelState) -> str:
    text = state.get("chapter_controls_text", "")
    if text:
        return f"\n\n【本章写作控制面板】\n{text}\n以上控制项必须优先落实到本章大纲中。"
    return ""


def generate_outline(state: NovelState) -> dict:
    """
    LangGraph 节点：生成全书大纲。
    仅在第一章时调用。
    """
    print("\n🎯 [总策划] 正在构思全书大纲...")
    report_progress("正在构思全书大纲...", "outline")

    full_outline_text = state.get("full_outline_text", "").strip()
    if full_outline_text:
        print("  ✅ 使用用户确认的全书章节蓝图")
        report_progress("已采用用户确认的全书章节蓝图", "outline")
        return {"global_outline": full_outline_text}

    setting = state["novel_setting"] + build_full_outline_hint(state) + build_opening_outline_hint(state)
    prompt = PLANNER_OUTLINE_PROMPT.format(
        title=state["novel_title"],
        genre=state["novel_genre"],
        setting=setting,
        num_chapters=state.get("num_chapters", 100),
    )
    outline = call_llm(
        role="planner",
        system_prompt=PLANNER_SYSTEM,
        prompt=prompt,
        temperature=0.8,
    )

    print("  ✅ 全书大纲已生成")
    report_progress("全书大纲已生成", "outline")
    return {"global_outline": outline}


def plan_chapter(state: NovelState) -> dict:
    """
    LangGraph 节点：为当前章节生成细化大纲。
    注入故事阶段信息，使策划感知当前处于故事弧线的哪个位置。
    """
    chapter_num = state["current_chapter"]
    num_chapters = state.get("num_chapters", 100)
    print(f"\n🎯 [总策划] 正在规划第{chapter_num}章大纲...")
    report_progress(f"正在规划第 {chapter_num} 章大纲...", "planning")

    from memory import StoryBible, DEFAULT_NOVEL_ID
    story_bible = StoryBible(state.get("novel_id", DEFAULT_NOVEL_ID))

    # --- 检索工作台预设指令 ---
    pending_inspirations = story_bible.get_pending_inspirations()
    pending_hooks = story_bible.get_pending_plot_hooks(chapter_num)
    
    auth_inst = "暂无"
    plot_hooks_str = "暂无"
    
    if pending_inspirations or pending_hooks:
        auth_inst = ""
        plot_hooks_str = ""
        
        if pending_hooks:
            print(f"  🪝 发现目标为第{chapter_num}章的作者预设伏笔！强制注入大纲规划。")
            for hook in pending_hooks:
                auth_inst += f"- [伏笔要求] {hook['content']}\n"
                
        if pending_inspirations:
            print(f"  💡 发现待处理的作者灵感碎片！尝试融入本章...")
            for insp in pending_inspirations:
                # 标记该灵感试图被使用
                story_bible.mark_inspiration_used(insp["id"])
                plot_hooks_str += f"- [灵感素材] {insp['content']} (标签: {insp['tags']})\n"

    # 计算当前故事阶段
    story_phase = get_story_phase(chapter_num, num_chapters)

    author_instructions = auth_inst if auth_inst else "暂无"
    author_instructions += build_chapter_controls_hint(state)

    prompt = PLANNER_CHAPTER_PROMPT.format(
        chapter_num=chapter_num,
        global_outline=state["global_outline"],
        story_so_far=state.get("story_so_far", "目前是第一章，故事才刚刚开始。"),
        bible_context=state.get("bible_context", "暂无前文记录"),
        story_phase=story_phase,
        author_instructions=author_instructions,
        plot_hooks=plot_hooks_str if plot_hooks_str else "暂无",
    )
    chapter_outline = call_llm(
        role="planner",
        system_prompt=PLANNER_SYSTEM,
        prompt=prompt,
        temperature=0.7,
    )

    print(f"  ✅ 第{chapter_num}章大纲已完成")
    report_progress(f"第 {chapter_num} 章大纲已完成", "planning")
    return {"chapter_outline": chapter_outline, "edit_count": 0}
