"""端到端跑一章：用真实的 M3 + Qwen 27B + Qwen 9B 跑一次完整章节生成。

使用前提：
  1) .env 已配置 MINIMAX_API_KEY
  2) 本地 GPUStack 上的 qwen3.5-27b / qwen3.5-9b 模型服务在运行
  3) mcp_server.py 已启动（端口 8001）

使用方式：
  python e2e_run_chapter.py "被退婚后我成了绝顶高手" --chapter 1
  python e2e_run_chapter.py "末日医生" --chapter 5
"""
import sys
import os
import argparse
import time
import json
import threading

sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv(override=True)


def safe_print(s):
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode("ascii"), flush=True)


def main():
    parser = argparse.ArgumentParser(description="用真实 LLM 跑一章")
    parser.add_argument("inspiration", help="一句话灵感，如：被退婚后我成了绝顶高手")
    parser.add_argument("--chapter", type=int, default=1, help="生成第几章（默认 1）")
    parser.add_argument("--target-words", type=int, default=2500, help="每章目标字数")
    parser.add_argument("--no-full", action="store_true", help="跳过全书大纲生成（只生成当前章）")
    parser.add_argument("--style-preset", default="", help="风格 preset key（如 apocalypse_survival / rebirth_revenge）")
    args = parser.parse_args()

    # 检查环境
    safe_print("=" * 60)
    safe_print("端到端跑一章")
    safe_print("=" * 60)

    # 检查 .env
    import config
    if not config.MINIMAX_API_KEY:
        safe_print("[ERROR] .env 未配置 MINIMAX_API_KEY")
        return 1
    if not config.LOCAL_API_KEY:
        safe_print("[ERROR] .env 未配置 API_KEY（本地 GPUStack）")
        return 1
    safe_print(f"  [OK] MiniMax API 已配置")
    safe_print(f"  [OK] 本地 GPUStack 已配置: {config.LOCAL_BASE_URL}")
    safe_print(f"  [OK] 模型配置:")
    for role, m in config.MODELS.items():
        safe_print(f"      {role:10s} -> {m['model']:18s} provider={m['provider']:8s}")

    # 检查 mcp_server
    try:
        import requests
        r = requests.get("http://127.0.0.1:8001/docs", timeout=2)
        if r.status_code != 200:
            safe_print(f"[ERROR] MCP 服务未正常响应: {r.status_code}")
            return 1
        safe_print("  [OK] MCP 服务在线 (http://127.0.0.1:8001)")
    except Exception as e:
        safe_print(f"[ERROR] MCP 服务不可达: {e}")
        safe_print("       请先在新终端运行: python mcp_server.py")
        return 1

    # 1) 初始化作品
    safe_print("")
    safe_print("-" * 60)
    safe_print(f"步骤 1: 初始化作品《{args.inspiration}》")
    safe_print("-" * 60)

    import models
    # 重置 usage 统计
    models.reset_usage()

    from graph import build_full_pipeline
    pipeline = build_full_pipeline()
    state = {
        "novel_id": f"e2e_{int(time.time())}",
        "novel_title": args.inspiration,
        "novel_genre": "都市",
        "novel_style": "爽文",
        "novel_setting": args.inspiration,
        "style_preset_key": args.style_preset,  # 风格 preset（可选）
        "num_chapters": 100,
        "current_chapter": 1,
        "global_outline": "",
        "chapter_outline": "",
        "chapter_pattern_card": "",
        "character_voice_guide": "",
        "reader_promise_guide": "",
        "chapter_drama_card": "",
        "chapter_brief": "",
        "story_so_far": "目前是第一章，故事刚刚开始。",
        "global_synopsis": "",
        "recent_summaries": [],
        "structured_status": "{}",
        "chapter_target_words": args.target_words,
        "bible_context": "",
        "edit_required": "",
        "edit_suggestions": "",
        "detector_risks": [],
        "risk_patch_required": "",
        "quality_report": {},
        "quality_enhanced_once": False,
        "is_approved": False,
        "review_result": "",
        "edit_count": 0,
        "completed_chapters": [],
    }

    if args.no_full:
        # 跳过 generate_outline，直接用预设大纲
        state["global_outline"] = (
            f"《{args.inspiration}》大纲：\n"
            "1. 主角因为{事件}陷入低谷\n"
            "2. 获得{金手指}开始崛起\n"
            "3. 逐步打脸{对手}积累爽点\n"
            "4. 最终{逆袭}走向巅峰"
        )
        from graph import build_chapter_graph
        pipeline = build_chapter_graph()
        safe_print("  [SKIP] 跳过全书大纲生成（--no-full）")
    else:
        safe_print("  正在生成全书大纲...")

    # 跑第 1 章
    safe_print("")
    safe_print("-" * 60)
    safe_print(f"步骤 2: 跑第 {args.chapter} 章创作")
    safe_print("-" * 60)
    start = time.time()
    try:
        result = pipeline.invoke(state)
    except Exception as e:
        safe_print(f"[ERROR] 跑批失败: {e}")
        import traceback
        traceback.print_exc()
        return 1
    elapsed = time.time() - start
    chapter_content = result.get("chapter_content", "")
    safe_print(f"  耗时: {elapsed:.1f}s")
    safe_print(f"  正文字数: {len(chapter_content)}")
    safe_print(f"  目标字数: {args.target_words}")
    safe_print(f"  状态: {'通过' if result.get('is_approved') else '需修改'}")

    # 用量统计
    usage = models.get_usage_summary()
    safe_print("")
    safe_print("-" * 60)
    safe_print("Token 消耗")
    safe_print("-" * 60)
    safe_print(f"  总调用: {usage['total_calls']} 次")
    safe_print(f"  总 token: {usage['total_tokens']}")
    safe_print(f"  总耗时: {usage['total_duration_seconds']:.1f}s")
    safe_print(f"  按角色:")
    for role, v in usage["by_role"].items():
        safe_print(f"    {role:10s} {v['calls']} 次 / {v['tokens']} tokens")
    safe_print(f"  按 provider:")
    for prov, v in usage["by_provider"].items():
        safe_print(f"    {prov:10s} {v['calls']} 次 / {v['tokens']} tokens")

    # 保存结果
    output_file = f"output/e2e_chapter_{int(time.time())}.txt"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(chapter_content)
    safe_print("")
    safe_print(f"  章节正文已保存到: {output_file}")

    # 输出部分正文预览
    safe_print("")
    safe_print("-" * 60)
    safe_print("正文预览（前 800 字）")
    safe_print("-" * 60)
    safe_print(chapter_content[:800])
    if len(chapter_content) > 800:
        safe_print("...")

    return 0


if __name__ == "__main__":
    sys.exit(main())
