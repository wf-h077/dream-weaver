"""长篇稳定性测试：跑 N 章，验证跨章 context 保持。

每章基于上一章的 state（含 story_so_far / global_synopsis / recent_summaries /
entity_status 等），验证：
1. 角色名跨章一致
2. 物品/设定跨章一致
3. 状态面板累积正确
4. 长期 context 不丢

使用前提：
- mcp_server.py 已启动（端口 8001）
- .env 配置完成（MINIMAX_API_KEY、本地 GPUStack）
"""
import sys
import os
import argparse
import time
import json
import re
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
    parser = argparse.ArgumentParser(description="批量跑 N 章，stateful 跨章 context")
    parser.add_argument("inspiration", help="一句话灵感，如：被退婚后我成了绝顶高手")
    parser.add_argument("--count", type=int, default=3, help="要跑几章（默认 3）")
    parser.add_argument("--target-words", type=int, default=2500, help="每章目标字数")
    parser.add_argument("--start-chapter", type=int, default=1, help="从第几章开始")
    parser.add_argument("--no-full", action="store_true", help="跳过全书大纲生成")
    parser.add_argument("--output-dir", default="output", help="章节保存目录")
    args = parser.parse_args()

    safe_print("=" * 60)
    safe_print(f"长篇稳定性测试：跑 {args.count} 章")
    safe_print("=" * 60)

    # 环境检查
    import config
    if not config.MINIMAX_API_KEY:
        safe_print("[ERROR] .env 未配置 MINIMAX_API_KEY")
        return 1
    if not config.LOCAL_API_KEY:
        safe_print("[ERROR] .env 未配置 API_KEY")
        return 1
    safe_print(f"  [OK] MiniMax API 已配置")
    safe_print(f"  [OK] 本地 GPUStack 已配置: {config.LOCAL_BASE_URL}")

    try:
        import requests
        r = requests.get("http://127.0.0.1:8001/docs", timeout=2)
        if r.status_code != 200:
            safe_print(f"[ERROR] MCP 服务未正常响应: {r.status_code}")
            return 1
        safe_print("  [OK] MCP 服务在线")
    except Exception as e:
        safe_print(f"[ERROR] MCP 服务不可达: {e}")
        return 1

    # 模型配置
    safe_print(f"  [OK] 模型配置:")
    for role, m in config.MODELS.items():
        safe_print(f"      {role:10s} -> {m['model']:18s} provider={m['provider']:8s}")

    # 创建输出目录
    os.makedirs(args.output_dir, exist_ok=True)
    batch_id = int(time.time())
    batch_dir = os.path.join(args.output_dir, f"batch_{batch_id}")
    os.makedirs(batch_dir, exist_ok=True)
    safe_print(f"  [OK] 输出目录: {batch_dir}")

    # 初始化作品
    safe_print("")
    safe_print("-" * 60)
    safe_print(f"步骤 1: 初始化作品《{args.inspiration}》")
    safe_print("-" * 60)

    import models
    models.reset_usage()

    from graph import build_full_pipeline, build_chapter_graph
    if args.no_full:
        pipeline = build_chapter_graph()
        # 用 --no-full 时先用预设大纲
        initial_state = {
            "novel_id": f"batch_{batch_id}",
            "novel_title": args.inspiration,
            "novel_genre": "都市修仙",
            "novel_style": "爽文",
            "novel_setting": args.inspiration,
            "num_chapters": 100,
            "current_chapter": args.start_chapter,
            "global_outline": (
                f"《{args.inspiration}》大纲：\n"
                "1. 主角陷入低谷，被退婚羞辱\n"
                "2. 获得金手指/奇遇觉醒\n"
                "3. 逐步打脸对手积累爽点\n"
                "4. 最终逆袭走向巅峰"
            ),
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
    else:
        pipeline = build_full_pipeline()
        initial_state = {
            "novel_id": f"batch_{batch_id}",
            "novel_title": args.inspiration,
            "novel_genre": "都市修仙",
            "novel_style": "爽文",
            "novel_setting": args.inspiration,
            "num_chapters": 100,
            "current_chapter": args.start_chapter,
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

    # 跑 N 章
    safe_print("")
    safe_print("-" * 60)
    safe_print(f"步骤 2: 顺序跑 {args.count} 章（每章基于上一章 state）")
    safe_print("-" * 60)

    state = initial_state
    chapter_results = []
    total_start = time.time()

    for i in range(args.count):
        ch = args.start_chapter + i
        safe_print(f"\n[第 {ch} 章] 开始...")
        ch_start = time.time()

        # 重置每章的临时字段
        state["current_chapter"] = ch
        state["chapter_outline"] = ""
        state["chapter_pattern_card"] = ""
        state["character_voice_guide"] = ""
        state["reader_promise_guide"] = ""
        state["chapter_drama_card"] = ""
        state["chapter_brief"] = ""
        state["chapter_content"] = ""
        state["edit_required"] = ""
        state["edit_suggestions"] = ""
        state["detector_risks"] = []
        state["risk_patch_required"] = ""
        state["quality_report"] = {}
        state["quality_enhanced_once"] = False
        state["is_approved"] = False
        state["review_result"] = ""
        state["edit_count"] = 0

        try:
            result = pipeline.invoke(state)
        except Exception as e:
            safe_print(f"  [ERROR] 第 {ch} 章失败: {e}")
            import traceback
            traceback.print_exc()
            chapter_results.append({
                "chapter": ch,
                "status": "error",
                "error": str(e),
            })
            break

        elapsed = time.time() - ch_start
        chapter_content = result.get("chapter_content", "")
        # 保存章节
        out_path = os.path.join(batch_dir, f"chapter_{ch:03d}.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(chapter_content)
        # 保存 state 快照（用于一致性分析）
        state_path = os.path.join(batch_dir, f"state_{ch:03d}.json")
        state_snapshot = {
            "chapter": ch,
            "story_so_far": result.get("story_so_far", ""),
            "global_synopsis": result.get("global_synopsis", ""),
            "recent_summaries": result.get("recent_summaries", []),
            "structured_status": result.get("structured_status", ""),
            "current_chapter": result.get("current_chapter"),
            "word_count": len(chapter_content),
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state_snapshot, f, ensure_ascii=False, indent=2)

        safe_print(f"  [OK] {elapsed:.1f}s, {len(chapter_content)} 字")
        safe_print(f"        保存到: {out_path}")

        # 准备下一章 state（继承关键字段）
        next_state = dict(result)
        next_state["chapter_target_words"] = args.target_words
        state = next_state

        chapter_results.append({
            "chapter": ch,
            "status": "success",
            "elapsed": elapsed,
            "word_count": len(chapter_content),
            "out_path": out_path,
            "state_path": state_path,
        })

    total_elapsed = time.time() - total_start

    # 输出总结
    safe_print("")
    safe_print("=" * 60)
    safe_print("批量跑测试总结")
    safe_print("=" * 60)
    safe_print(f"  总耗时: {total_elapsed:.1f}s ({total_elapsed/60:.1f} 分钟)")
    safe_print(f"  成功章数: {sum(1 for r in chapter_results if r['status']=='success')}/{args.count}")
    if chapter_results:
        success_chs = [r for r in chapter_results if r['status']=='success']
        if success_chs:
            avg = sum(r['elapsed'] for r in success_chs) / len(success_chs)
            safe_print(f"  平均单章: {avg:.1f}s")
            total_words = sum(r['word_count'] for r in success_chs)
            safe_print(f"  总字数: {total_words}")

    # 用量统计
    usage = models.get_usage_summary()
    safe_print("")
    safe_print("Token 消耗：")
    safe_print(f"  总调用: {usage['total_calls']} 次")
    safe_print(f"  总 token: {usage['total_tokens']}")
    safe_print(f"  按角色：")
    for role, v in usage["by_role"].items():
        safe_print(f"    {role:10s} {v['calls']} 次 / {v['tokens']} tokens")
    safe_print(f"  按 provider：")
    for prov, v in usage["by_provider"].items():
        safe_print(f"    {prov:10s} {v['calls']} 次 / {v['tokens']} tokens")

    # 估算成本
    m3_tokens = usage["by_provider"].get("minimax", {}).get("tokens", 0)
    safe_print("")
    safe_print(f"  M3 实际 token: {m3_tokens}")
    safe_print(f"  M3 估算成本: ¥{m3_tokens * 0.0001:.2f}（按 ¥0.1/1k tokens 混合价）")

    # 提示用户跑一致性分析
    safe_print("")
    safe_print("=" * 60)
    safe_print("下一步：跑一致性分析")
    safe_print("=" * 60)
    safe_print(f"  python analyze_consistency.py {batch_dir}")
    safe_print("")
    safe_print("  会自动提取每章关键实体（角色/物品/境界/地点）")
    safe_print("  并对比跨章一致性")

    return 0


if __name__ == "__main__":
    sys.exit(main())
