"""LangGraph 状态定义模块"""
from typing import TypedDict, List, Optional
import operator
from langgraph.graph import add_messages


class NovelState(TypedDict):
    """小说创作工作流的共享状态"""

    # ── 全书信息 ──
    novel_id: str                        # 小说项目 ID，用于隔离记忆库与章节数据
    novel_title: str                     # 小说标题
    novel_genre: str                     # 小说类型（如：都市修仙）
    novel_style: str                     # 小说文风要求（如：暗黑、轻松幽默）
    novel_setting: str                   # 核心设定（用户提供的初始创意）
    concept_seed: str                    # 一句话灵感来源
    selected_concept: dict               # 用户采用的立项方向
    concept_refinement: dict             # 深度细化后的立项包
    full_outline: dict                   # 全书章节蓝图结构化数据
    full_outline_text: str               # 全书章节蓝图文本
    opening_outline: dict                # 前 10 章开局规划结构化数据
    opening_outline_text: str            # 前 10 章开局规划文本
    num_chapters: int                    # 全书目标总章数

    # ── 大纲层 ──
    global_outline: str                  # 全书大纲
    current_chapter: int                 # 当前正在创作的章节号
    chapter_outline: str                 # 当前章节的细化大纲
    chapter_pattern_card: str            # 本章爆款结构样板（向后兼容旧字段）
    character_voice_guide: str           # 本章角色声音与对白约束（向后兼容旧字段）
    reader_promise_guide: str            # 本章读者期待与伏笔兑现台账（向后兼容旧字段）
    chapter_drama_card: str              # 本章戏剧卡：爽点、冲突、期待、反转与章末钩子（向后兼容旧字段）
    chapter_brief: str                   # 章节创作指令包（合并 4 段，writer 优先消费）
    story_so_far: str                    # 前情摘要（提供给大模型的格式化输出）
    global_synopsis: str                 # 长期全书大事件记忆（压缩）
    recent_summaries: List[str]          # 最近8章的详细概要
    structured_status: str               # 结构化状态（如人物境界、关系、物品）从 entity_status 提取
    current_chapter_controls: dict       # 当前章节写作控制面板参数
    chapter_controls_text: str           # 当前章节写作控制文本
    chapter_control_history: List[dict]  # 每章使用过的写作控制历史

    # ── 创作层 ──
    chapter_content: str                 # 当前章节的生成正文
    bible_context: str                   # 从 ChromaDB 检索到的相关设定

    # ── 质检层 ──
    edit_required: str                   # 必须修改的硬伤
    edit_suggestions: str                # 建议优化的项（可选）
    detector_risks: List[dict]           # 专项一致性检测器输出的结构化风险
    risk_patch_required: str             # 需要由 patcher 处理的 must_fix 风险说明
    quality_report: dict                 # 小说可读性质量评分与增强记录
    quality_enhanced_once: bool          # 本轮是否已经做过质量增强，避免无限重写
    is_approved: bool                    # 是否通过质检 (向前兼容)
    review_result: str                   # 'approve', 'patch', 或 'rewrite'
    edit_count: int                      # 当前章节已修改次数

    # ── 输出层 ──
    completed_chapters: List[str]        # 已完成的章节正文列表
