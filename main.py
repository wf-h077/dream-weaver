"""小说创作智能体 - 主入口

用法：
  conda activate wnovel
  python main.py
"""
import os
import sys

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
from config import OUTPUT_DIR
from graph import build_full_pipeline, build_chapter_graph, story_bible


def save_chapter(chapter_num: int, content: str):
    """将章节正文保存到 output 目录"""
    filename = os.path.join(OUTPUT_DIR, f"第{chapter_num}章.txt")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  📄 已保存到 {filename}")


def save_checkpoint(state: dict):
    """将当前记忆状态保存到 checkpoint.json 以支持断点续写"""
    checkpoint_path = os.path.join(OUTPUT_DIR, "checkpoint.json")
    with open(checkpoint_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"  💾 进度已自动存档至 checkpoint.json")


def run_novel(title: str, genre: str, setting: str, style: str = "风格不限", start_chapter: int = 1, num_chapters: int = 3):
    """
    运行完整的小说创作流程。

    Args:
        title: 小说标题
        genre: 小说类型
        setting: 核心设定描述
        style: 文章风格要求
        num_chapters: 要生成的章节数
    """
    print("=" * 60)
    print(f"📚 《{title}》创作启动")
    print(f"   类型: {genre} | 目标: {num_chapters} 章")
    print("=" * 60)

    # 获取续写信息 (如果是续写)
    checkpoint_path = os.path.join(OUTPUT_DIR, "checkpoint.json")
    global_outline = ""
    global_synopsis = ""
    recent_summaries = []
    story_so_far = "目前是故事开场。"

    if os.path.exists(checkpoint_path):
        print(f"\n🔄 检测到存档文件，正在从 checkpoint.json 恢复状态...")
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            saved_state = json.load(f)
        
        # 自动跳到最新的下一章续写 (当用户未强制修改start_chapter时)
        saved_ch = saved_state.get("current_chapter", 1)
        if saved_ch > start_chapter:
            start_chapter = saved_ch
            print(f"  自动识别续写进度：将从第 {start_chapter} 章开始创作")
            
        global_outline = saved_state.get("global_outline", "（全局大纲加载失败）")
        global_synopsis = saved_state.get("global_synopsis", "")
        recent_summaries = saved_state.get("recent_summaries", [])
        story_so_far = saved_state.get("story_so_far", "（前情提要加载失败）")
    elif start_chapter > 1:
        # 兼容老逻辑：如果没有 checkpoint.json，则尝试扫描 txt 文件
        global_outline = "（这是续写，假定大纲已存在）"
        print(f"\n⚠️ 未找到 checkpoint.json，正在回溯前 {start_chapter-1} 章剧情重建摘要...")
        for i in range(1, start_chapter):
            ch_path = os.path.join(OUTPUT_DIR, f"第{i}章.txt")
            if os.path.exists(ch_path):
                with open(ch_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                print(f"  - 生成第{i}章摘要...")
                summary = story_bible.summarize_chapter(content)
                recent_summaries.append(f"[第{i}章]:\n{summary}")
            else:
                recent_summaries.append(f"[第{i}章]:\n（前情文件缺失）")
        
        story_so_far = "【近期详细剧情】\n" + "\n\n".join(recent_summaries)

    # 初始化故事宝典（仅在确认没有续写进度且是第一章时）
    if start_chapter == 1:
        story_bible.init_from_setting(setting)

    # ── 第一章或启动章：使用完整流水线（含大纲生成）──
    print(f"\n{'─' * 40}")
    if start_chapter == 1:
        print(f"  开始创作第 1 章（含全书大纲生成）")
    else:
        print(f"  从第 {start_chapter} 章开始续写")
    print(f"{'─' * 40}")

    full_pipeline = build_full_pipeline()
    initial_state = {
        "novel_title": title,
        "novel_genre": genre,
        "novel_style": style,
        "novel_setting": setting,
        "num_chapters": num_chapters,
        "current_chapter": start_chapter,
        "global_outline": global_outline,
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
        "story_so_far": story_so_far,
        "global_synopsis": global_synopsis,
        "recent_summaries": recent_summaries,
        "is_approved": False,
        "edit_count": 0,
        "completed_chapters": [],
    }

    if start_chapter == 1:
        full_pipeline = build_full_pipeline()
        result = full_pipeline.invoke(initial_state)
    else:
        # 如果是续写，只用 chapter graph，跳过全书大纲生成
        chapter_graph = build_chapter_graph()
        result = chapter_graph.invoke(initial_state)
        
    save_chapter(start_chapter, result["chapter_content"])
    save_checkpoint(result)

    # ── 后续章节：使用单章流水线 ──
    chapter_graph = build_chapter_graph()

    for ch in range(start_chapter + 1, num_chapters + 1):
        print(f"\n{'─' * 40}")
        print(f"  开始创作第 {ch} 章")
        print(f"{'─' * 40}")

        chapter_state = {
            **result,              # 继承前一章的状态
            "current_chapter": ch,
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
            "is_approved": False,
            "edit_count": 0,
        }

        result = chapter_graph.invoke(chapter_state)
        save_chapter(ch, result["chapter_content"])
        save_checkpoint(result)

    # ── 完成 ──
    print("\n" + "=" * 60)
    print(f"🎉 《{title}》前{num_chapters}章创作完成！")
    print(f"   输出目录: {OUTPUT_DIR}")
    print("=" * 60)

    # 打印全书大纲概览
    print("\n📋 全书大纲：")
    print(result.get("global_outline", "")[:1000])


if __name__ == "__main__":
    import traceback
    try:
        # ── 示例：都市修仙小说 ──
        run_novel(
            title="天道执笔人",
            genre="都市修仙",
            setting="""
    背景设定：
    现代都市与隐秘修仙界并存的世界。修仙界隐藏在都市的暗面，普通人无法感知灵气的存在。

    主角：
    林墨，25岁，原本是一名默默无闻的网文写手。某天在写作时意外发现，
    自己写下的修仙情节会在一周后变成现实。他写的功法真的能修炼，
    他写的法宝真的会出现。但代价是——每次动笔，他的寿命都会缩短。

    核心冲突：
    林墨必须在"创作力量"和"生命消耗"之间做出抉择。
    同时，修仙界的势力开始注意到这个能"书写现实"的凡人......

    力量体系：
    修仙境界从低到高：引气期 → 筑基期 → 金丹期 → 元婴期 → 化神期
    林墨的特殊能力"天道之笔"不属于传统修仙体系，而是一种更高维度的力量。
    """,
            start_chapter=1, # 从第8章开始续写
            num_chapters=100,  # 目标100章
        )
    except Exception as e:
        print("\n=== CRASH CAUGHT ===")
        traceback.print_exc()
        print("====================")
