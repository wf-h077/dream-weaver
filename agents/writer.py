"""执笔作者智能体节点

职责：根据章节大纲和参考设定，生成高质量的小说正文。
注入故事阶段意识，使文风和节奏契合当前叙事位置。
"""
import re as re
from models import call_llm
from prompts import (
    WRITER_SYSTEM, WRITER_PROMPT, get_story_phase,
    SKILL_SELECTOR_SYSTEM, SKILL_SELECTOR_PROMPT
)
from prompts_style_presets import build_style_preset_prompt_block
from state import NovelState
from config import get_chapter_word_range
from task_progress import report_progress
from agents.text_quality import (
    ensure_chapter_title,
    extract_chapter_title,
    looks_like_broken_opening,
    looks_like_fake_fix,
    normalize_chapter_text,
)
import os
import json


WORD_COUNT_EXPAND_SYSTEM = """你是一位负责网文章节补写的资深作者。
你的任务是在不改变原有剧情走向、不新增突兀设定、不破坏人物状态的前提下，把偏短的章节自然扩写到目标字数范围。
只输出扩写后的完整章节正文，不要解释，不要 Markdown，不要作者说明。"""

WORD_COUNT_EXPAND_PROMPT = """下面这章正文低于目标字数，需要扩写。

【章节号】
第 {chapter_num} 章

【目标字数】
目标约 {target_words} 字，合格范围 {min_words}-{max_words} 字。当前约 {word_count} 字，至少还需要补足 {missing_words} 字。

【扩写要求】
1. 保留第一行章节标题，不要重复标题，不要新增第二个标题。
2. 保留原剧情主线、人物关系和结尾方向，不要推翻原文。
3. 优先补足：场景细节、人物动作与心理、关键对话、冲突升级、信息增量、章末钩子。
4. 不要为了凑字重复同一句话、同一动作、同一情绪、同一解释，也不要空泛抒情或堆砌无意义描写。
5. 段落适合手机阅读，每段 1-3 句。
6. 每新增 300 字至少要有一个新的动作、信息、关系变化、冲突升级或选择后果。
7. 输出必须是完整扩写后的章节正文。

【原正文】
{chapter_content}
"""

WORD_COUNT_INSERT_SYSTEM = """你是一位网文章节连贯补写作者。
你的任务是为一章偏短的正文补写一组可插入段落，让它自然衔接到原来的章末钩子。
只输出新增段落，不要输出标题，不要解释，不要 Markdown，不要复述原文。"""

WORD_COUNT_INSERT_PROMPT = """下面这章仍低于最低字数，需要补写可插入段落。

【章节号】
第 {chapter_num} 章

【字数要求】
目标约 {target_words} 字，最低必须达到 {min_words} 字。当前约 {word_count} 字，还缺至少 {missing_words} 字。

【补写位置】
补写内容会插入到原文最后 1-2 个章末钩子段落之前，所以必须能自然引向原来的结尾。

【补写要求】
1. 只写新增段落，不要写章节标题。
2. 内容必须属于本章当前事件，不要开启下一章剧情。
3. 补足人物反应、动作细节、对话交锋、环境压力、信息增量或冲突升级。
4. 必须服务原来的章末钩子，不能改变结尾方向。
5. 不要重复原文已有句子、相同心理、相同动作或相同解释，不要灌水。
6. 新增段落必须带来新的剧情推进、信息变化或人物选择。
7. 新增段落合计建议 {insert_words} 字以上。

【原正文】
{chapter_content}

【原章末段落】
{ending_excerpt}
"""

CHAPTER_OPENING_REPAIR_SYSTEM = """你是一个网文章节开头修复编辑。
你的任务是修复章节标题缺失、开头断裂、第一段像半句话、正文格式混乱等问题。
只输出修复后的完整章节正文，不要解释，不要 Markdown，不要作者说明。"""

CHAPTER_OPENING_REPAIR_PROMPT = """请修复小说第 {chapter_num} 章的标题和开头完整性。

【必须使用的章节标题】
{chapter_title}

【本章大纲】
{chapter_outline}

【修复要求】
1. 第一行必须是章节标题：{chapter_title}
2. 标题后第一段必须是完整自然的正文开场，不能从半句话、残句、总结句开始。
3. 如果原文第一句像“血。那夜……”这类残缺开头，请补齐或重写开场衔接。
4. 不改变主线剧情、人物关系、核心事件和章末钩子。
5. 每段 1-3 句，保留移动端阅读格式。
6. 输出完整章节正文。

【待修复正文】
{chapter_content}
"""

def load_skills():
    """读取 skills 目录下的所有挂载技能"""
    skills = {}
    skills_dir = "skills"
    if os.path.exists(skills_dir):
        for filename in os.listdir(skills_dir):
            if filename.endswith(".json"):
                try:
                    with open(os.path.join(skills_dir, filename), "r", encoding="utf-8") as f:
                        data = json.load(f)
                        skills[data["name"]] = data
                except Exception as e:
                    print(f"  ⚠️ 加载技能文件 {filename} 失败: {e}")
    return skills

def build_chapter_controls_text(state: NovelState) -> str:
    text = state.get("chapter_controls_text", "")
    if not text:
        return ""
    return f"\n\n【本章写作控制面板】\n{text}\n写作时必须落实这些控制项，尤其是人物、设定、伏笔、情绪、节奏、视角和结尾钩子。"

def build_platform_format_constraints(target_words: int, min_words: int, max_words: int) -> str:
    return f"""

【番茄小说发布友好格式约束】
请把本章写成可直接粘贴到网文发布后台的正文，默认按番茄小说这类移动端阅读平台的发布习惯处理：
1. 字数：本章目标约 {target_words} 字，允许范围 {min_words}-{max_words} 字；不要明显短章，也不要为了凑字重复灌水。
2. 正文纯净：只输出小说正文。不要输出 Markdown、项目符号、写作说明、作者注、AI 自述、审核意见、括号备注或“以下是正文”等提示语。
3. 章节格式：如需写章节标题，只能使用“第X章 标题”这种普通文本标题；不要使用 #、【】、加粗符号或分隔线。
4. 段落适配手机阅读：自然分段，单段尽量 1-3 句；避免连续超长段落；对话、动作、心理和环境描写要分段清晰。
5. 标点与对话：中文标点规范；对话引号统一；对白不要堆成问答记录，要穿插动作、神态、心理或场景变化。
6. 内容合规：避免露骨色情、未成年人不当内容、极端血腥猎奇、违法犯罪教学、现实政治敏感煽动、仇恨歧视和明显违规表达。可以写冲突、暧昧、危险和暴力，但要以剧情表达为主，避免细节越界。
7. 网文节奏：每章必须有清晰事件推进、情绪变化、信息增量或冲突升级；结尾尽量留下下一章期待或钩子。
8. 商业可读性：避免过度文艺化、抽象化和长篇议论；优先让读者看懂人物目标、矛盾压力和下一步期待。
9. 禁止无关信息：正文中不要出现外链、广告、QQ群、微信号、平台吐槽、求关注、求打赏、作者联系方式等与故事无关的信息。
10. 禁止低质无意义内容：不要重复灌水、重复段落、近似复述、乱码、无序符号、表情堆叠、整章空白、整章外文或繁体、章节乱序。
10.1 禁止廉价重复：同一句、同一动作、同一心理、同一解释、同一气氛描写不能反复出现；如果需要强调，必须用新的动作、选择、信息或冲突后果来表达。
11. 避免平台安全风险：不得恶意歪曲历史事实或历史人物；不得损害英雄烈士形象；不得宣扬邪教、封建迷信、宗教狂热、恐怖主义、极端主义、赌博、吸毒、违法犯罪教学、民族仇恨或歧视。
12. 未成年人保护：不要描写或宣扬未成年人涉黄赌毒、早恋诱导、霸凌、抽烟、斗殴、混社会、自杀自残细节、师生恋、怀孕生子、恋童或虐待青少年儿童等负面导向内容。
"""


def ensure_chapter_word_count(
    content: str,
    chapter_num: int,
    target_words: int,
    min_words: int,
    max_words: int,
) -> str:
    """Expand a generated chapter if it is below the hard lower bound."""
    final_content = (content or "").strip()
    hard_min_words = min_words
    desired_min_words = min_words + min(160, max(80, int(min_words * 0.05)))

    for attempt in range(3):
        word_count = len(final_content)
        if word_count >= hard_min_words:
            return final_content

        missing_words = desired_min_words - word_count
        print(f"  ⚠️ 正文偏短（约 {word_count} 字，最低 {hard_min_words} 字），自动扩写第 {attempt + 1} 轮...")
        report_progress(f"正文偏短，正在自动扩写补足约 {missing_words} 字...", "writing")
        expanded = call_llm(
            role="writer",
            system_prompt=WORD_COUNT_EXPAND_SYSTEM,
            prompt=WORD_COUNT_EXPAND_PROMPT.format(
                chapter_num=chapter_num,
                target_words=target_words,
                min_words=desired_min_words,
                max_words=max_words,
                word_count=word_count,
                missing_words=max(1, missing_words),
                chapter_content=final_content,
            ),
            temperature=0.75,
            max_tokens=8192,
        )
        expanded = (expanded or "").strip()
        if len(expanded) > len(final_content):
            final_content = expanded
        else:
            print("  ⚠️ 扩写结果未变长，保留当前正文")
            break

    for _ in range(3):
        if len(final_content) >= hard_min_words:
            break
        word_count = len(final_content)
        missing_words = hard_min_words - word_count
        insert_words = missing_words + min(180, max(80, int(missing_words * 0.5)))
        print(f"  ⚠️ 完整扩写后仍偏短（约 {word_count} 字），改用章末前插入补写...")
        report_progress(f"正在补写可插入段落，确保达到最低 {hard_min_words} 字...", "writing")

        ending_excerpt = get_ending_excerpt(final_content)
        insert_text = call_llm(
            role="writer",
            system_prompt=WORD_COUNT_INSERT_SYSTEM,
            prompt=WORD_COUNT_INSERT_PROMPT.format(
                chapter_num=chapter_num,
                target_words=target_words,
                min_words=hard_min_words,
                word_count=word_count,
                missing_words=missing_words,
                insert_words=insert_words,
                chapter_content=final_content,
                ending_excerpt=ending_excerpt,
            ),
            temperature=0.72,
            max_tokens=4096,
        )
        insert_text = clean_insert_text(insert_text)
        if not insert_text:
            break
        before = len(final_content)
        final_content = insert_before_ending(final_content, insert_text)
        if len(final_content) <= before:
            break

    return final_content


def split_paragraphs(text: str) -> list[str]:
    return [part.strip() for part in (text or "").splitlines() if part.strip()]


def get_ending_excerpt(text: str) -> str:
    paragraphs = split_paragraphs(text)
    return "\n".join(paragraphs[-2:]) if paragraphs else ""


def clean_insert_text(text: str | None) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    paragraphs = split_paragraphs(cleaned)
    if paragraphs and paragraphs[0].startswith("第") and "章" in paragraphs[0][:12]:
        paragraphs = paragraphs[1:]
    return "\n\n".join(paragraphs).strip()


def insert_before_ending(content: str, insert_text: str) -> str:
    paragraphs = split_paragraphs(content)
    if len(paragraphs) <= 3:
        return f"{content.strip()}\n\n{insert_text.strip()}".strip()
    title = paragraphs[0] if paragraphs[0].startswith("第") and "章" in paragraphs[0][:12] else ""
    body = paragraphs[1:] if title else paragraphs
    keep_tail_count = 2 if len(body) >= 6 else 1
    head = body[:-keep_tail_count]
    tail = body[-keep_tail_count:]
    merged = ([title] if title else []) + head + split_paragraphs(insert_text) + tail
    return "\n\n".join(item for item in merged if item).strip()


def finalize_generated_chapter_text(content: str, chapter_num: int, chapter_outline: str, dedupe: bool = False) -> str:
    """规范化生成的章节文本。

    Args:
        dedupe: 是否去重。默认 False（保留 M3 原始内容，避免字数被削到触发扩写）。
                只在最终保存时（save_chapter 之前）传 True。
    """
    from agents.text_quality import clean_ai_format_artifacts
    title = extract_chapter_title(chapter_outline, chapter_num) or f"第{chapter_num}章"
    # 修复 1：剥离 M3 常见的元话语前缀（"以下"、"好的"、"嗯"、"当然"等）
    finalized = _strip_meta_prefix(content)
    finalized = ensure_chapter_title(finalized, chapter_num, chapter_outline)
    if looks_like_fake_fix(finalized) or looks_like_broken_opening(finalized):
        report_progress("检测到章节标题缺失或开头断裂，正在修复开场...", "writing")
        repaired = call_llm(
            role="editor",
            system_prompt=CHAPTER_OPENING_REPAIR_SYSTEM,
            prompt=CHAPTER_OPENING_REPAIR_PROMPT.format(
                chapter_num=chapter_num,
                chapter_title=title,
                chapter_outline=chapter_outline,
                chapter_content=finalized,
            ),
            temperature=0.22,
            max_tokens=8192,
        )
        # 修复：剥离元话语前缀后再 ensure_chapter_title
        repaired = _strip_meta_prefix(repaired)
        finalized = ensure_chapter_title(repaired, chapter_num, chapter_outline)
    # AI 格式清理（删除表情符、修复 markdown 残留、折叠连续标点等）
    finalized = clean_ai_format_artifacts(finalized)
    return normalize_chapter_text(finalized, dedupe=dedupe)


# M3 常见元话语前缀清单（"以下是第N章..."、"好的，我..." 等）
_META_PREFIX_PATTERNS = [
    r"^\s*以下是(本章|第\s*\d+\s*章|第\s*[\u4e00-\u9fff]+\s*章|改写后|修改后|调整后|修订后)的?(正文|内容|章节|全文)?[:：]?\s*",
    r"^\s*好的[，,：:]?\s*(我)?(已经)?(为你)?(开始)?(写|写一下|给你|生成|创作|试试)?[。.,]?\s*\n?",
    r"^\s*好的[，,：:]?\s*(来)?(写|给你写|开始|试试)[。.,]?\s*\n?",
    r"^\s*(当然|嗯|好|行|可以)[，,：:]?\s*",
    r"^\s*(这是|下面是)第\s*\d+\s*章[:：]?\s*",
    r"^\s*第\s*\d+\s*章(正文|内容|全文)?\s*[:：]?\s*",
    r"^\s*(好的)?(我)?(来)?(开始)?写[。.,]?\s*\n?",
    r"^\s*(来|开始|试试)(写|创作|给你|生成)[一下]*[。.,]?\s*\n?",
    r"^\s*以下是.*?\n",
]


def _strip_meta_prefix(text: str) -> str:
    """剥离 M3 生成的"以下是..."等元话语前缀，避免被判为假修复。"""
    if not text:
        return text
    stripped = text
    # 最多剥 3 次（避免无限循环）
    for _ in range(3):
        original = stripped
        for pattern in _META_PREFIX_PATTERNS:
            new_stripped = re.sub(pattern, "", stripped, count=1, flags=re.MULTILINE)
            if new_stripped != stripped:
                stripped = new_stripped.lstrip()
                break
        if stripped == original:
            break
    return stripped

def write_chapter(state: NovelState) -> dict:
    """
    LangGraph 节点：生成/重写章节正文。
    """
    chapter_num = state["current_chapter"]
    num_chapters = state.get("num_chapters", 100)
    edit_count = state.get("edit_count", 0)

    # 上下文截断：避免长篇累积导致 prompt 超 32K token 限制
    try:
        from models import truncate_state_context
        state = truncate_state_context(state)
    except Exception as e:
        print(f"  ⚠️ 截断上下文失败: {e}")

    controls_text = build_chapter_controls_text(state)

    if edit_count == 0:
        print(f"\n✍️  [执笔作者] 正在创作第{chapter_num}章...")
        report_progress(f"正在创作第 {chapter_num} 章正文...", "writing")
        revision_note = controls_text
    else:
        print(f"\n✍️  [执笔作者] 正在根据反馈修改第{chapter_num}章（第{edit_count}次修改）...")
        report_progress(f"正在根据审稿反馈重写第 {chapter_num} 章...", "writing")
        revision_note = f"{controls_text}\n\n【编辑要求强制修改的硬伤】：\n{state.get('edit_required', '')}\n这些问题必须在重写时解决。"

    # 1. 动态挂载技能
    skills = load_skills()
    dynamic_skill_text = ""
    if skills:
        skills_list = "\n".join([f"- {name}: {info['description']}" for name, info in skills.items()])
        selector_prompt = SKILL_SELECTOR_PROMPT.format(
            skills_list=skills_list,
            chapter_outline=state["chapter_outline"]
        )
        selected_skill = call_llm(
            role="planner",  # 用个小模型或推理快的角色
            system_prompt=SKILL_SELECTOR_SYSTEM,
            prompt=selector_prompt,
            temperature=0.1,
            max_tokens=50
        ).strip()
        
        if selected_skill in skills:
            print(f"  🎯 [动态技能] 匹配成功：自动挂载【{selected_skill}】专精")
            report_progress(f"已挂载写作技能：{selected_skill}", "writing")
            dynamic_skill_text = skills[selected_skill]["prompt"]
        else:
            print(f"  🎯 [动态技能] 未匹配到特定的专家技能 ({selected_skill})，使用基础文笔")

    # 计算当前故事阶段
    from memory import StoryBible, DEFAULT_NOVEL_ID
    novel_id = state.get("novel_id", DEFAULT_NOVEL_ID)
    story_bible = StoryBible(novel_id)
    
    world_rules_list = story_bible.get_world_rules()
    world_rules_str = "暂无严格设定的境界或能力限定。"
    if world_rules_list:
        world_rules_str = ""
        for rule in world_rules_list:
            world_rules_str += f"- [{rule['category']}] {rule['rule_text']}\n"

    entity_cards_context = story_bible.get_entity_cards_context()
    structured_status = state.get("structured_status", "{}")
    if entity_cards_context:
        structured_status = f"{structured_status}\n\n{entity_cards_context}"

    style_fingerprint_context = story_bible.get_style_fingerprint_context()
    novel_style = state.get("novel_style", "风格不限")
    if style_fingerprint_context:
        novel_style = f"{novel_style}\n\n{style_fingerprint_context}"
    
    story_phase = get_story_phase(chapter_num, num_chapters)

    # 计算本章的目标字数
    target_words = state.get("chapter_target_words", 2000)
    min_words, max_words = get_chapter_word_range(target_words)

    # 注入 style_preset_block（从 state 读取）
    style_preset_key = state.get("style_preset_key", "")
    preset_block = build_style_preset_prompt_block(style_preset_key)
    if not preset_block:
        preset_block = "（未选择风格预设，按小说类型与现有文风指纹自由发挥）"

    system = WRITER_SYSTEM.format(
        min_words=min_words,
        max_words=max_words,
        extra_words=int(min_words * 0.15),  # 推荐多写 15%
        style_preset_block=preset_block,
    ) + build_platform_format_constraints(target_words, min_words, max_words)

    # 注入作品核心主题意图（用户显式声明，每章必读；压制 AI 写作的"永动机升级 / 配角工具人 / 主角被动成长"等病灶）
    theme_intent = (state.get("novel_theme_intent") or "").strip()
    if theme_intent:
        system += (
            "\n\n【作品核心主题意图】（作者显式声明，每章必读）：\n"
            f"{theme_intent}\n\n"
            "请严格按此意图组织本章的剧情走向、人物行为和情绪节奏。"
            "如果该意图与具体章节的剧情要求冲突，优先尊重作者的主题意图。"
        )

    # 优先使用合并版 chapter_brief（brief_composer 节点输出），fallback 到 4 个旧字段
    chapter_brief = state.get("chapter_brief", "").strip()
    if chapter_brief:
        chapter_brief_block = (
            "【本章创作指令包】（含章节样板 + 角色声音 + 读者期待 + 戏剧卡）：\n"
            f"{chapter_brief}"
        )
    else:
        # 兼容老数据：手动拼接 4 段
        chapter_brief_block = (
            "【本章戏剧卡】：\n"
            f"{state.get('chapter_drama_card', '本章需要明确冲突、爽点兑现、主角主动选择和章末钩子。')}\n\n"
            "【章节样板】：\n"
            f"{state.get('chapter_pattern_card', '')}\n\n"
            "【角色声音与对白约束】：\n"
            f"{state.get('character_voice_guide', '暂无明确角色声音约束。')}\n\n"
            "【读者期待与伏笔兑现台账】：\n"
            f"{state.get('reader_promise_guide', '本章需要兑现一个读者期待，并留下一个具体的下一章追读问题。')}"
        )

    prompt = WRITER_PROMPT.format(
        chapter_num=chapter_num,
        story_so_far=state.get("story_so_far", "目前是第一章，故事刚刚开始。"),
        chapter_outline=state["chapter_outline"],
        chapter_brief_block=chapter_brief_block,
        world_rules=world_rules_str,
        structured_status=structured_status,
        bible_context=state.get("bible_context", "暂无"),
        story_phase=story_phase,
        novel_style=novel_style,
        dynamic_skill=dynamic_skill_text,
        revision_note=revision_note,
        min_words=min_words,
        max_words=max_words,
    )

    # 最终防御：拼好的 prompt 还可能超 32K token 限制（虽然前面截了 state，但 style/voice/promise/brief 等可能很长）
    # 32768 - output_tokens(4096) - 2000(system) - 2000(business prompt) = ~24000 给 state + 扩展内容
    # 字符 / token 比例按 1:1 算
    try:
        from models import estimate_tokens, truncate_to_chars
        prompt_tokens = estimate_tokens(prompt)
        system_tokens = estimate_tokens(system)
        # 留 4096 token 给 output + 2000 buffer
        # 用户 max_tokens 可能是 4096 (输出)，所以 input 上限 = 32768 - 4096 - 1024 = 27648
        max_input = 24000
        if prompt_tokens + system_tokens > max_input:
            overflow = (prompt_tokens + system_tokens) - max_input
            # 优先截断 story_so_far（state 内的长字段）
            ssf = state.get("story_so_far", "")
            if estimate_tokens(ssf) > 3000:
                new_len = max(1000, len(ssf) - overflow - 500)  # 截 overflow + 500 字符 buffer
                truncated_ssf = truncate_to_chars(ssf, new_len, head=200)
                # 替换 prompt 内的 story_so_far 部分
                prompt = prompt.replace(ssf, truncated_ssf, 1)
                print(f"  ✂️ 二次截断 story_so_far: {len(ssf)} → {len(truncated_ssf)} 字符（多溢出 {overflow}）")
            else:
                # 截 structured_status / chapter_brief_block
                print(f"  ⚠️ prompt 仍超 {overflow} tokens，但 story_so_far 不可截（< 3000 字符）")
    except Exception as e:
        print(f"  ⚠️ 最终截断失败: {e}")

    # Agentic Tool Loop
    from tools.search_lore import SEARCH_LORE_TOOL_SCHEMA, search_lore
    from tools.read_chapter import READ_CHAPTER_TOOL_SCHEMA, read_chapter
    import json
    
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt}
    ]
    tools = [SEARCH_LORE_TOOL_SCHEMA, READ_CHAPTER_TOOL_SCHEMA]
    
    final_content = ""
    for step in range(5):  # 避免死循环，最多允许调用 5 次工具
        response = call_llm(
            role="writer",
            messages=messages,
            tools=tools,
            temperature=0.85,
            max_tokens=8192,
        )
        
        if isinstance(response, str):
            final_content = response
            break
        elif hasattr(response, "tool_calls") and response.tool_calls:
            # 追加 assistant message
            assistant_msg = {
                "role": "assistant",
                "content": response.content or "", 
                "tool_calls": [
                    {
                        "id": t.id,
                        "type": "function",
                        "function": {
                            "name": t.function.name,
                            "arguments": t.function.arguments
                        }
                    } for t in response.tool_calls
                ]
            }
            messages.append(assistant_msg)
            
            for tool_call in response.tool_calls:
                tool_result = ""
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception:
                    # fallback to string if json parsing fails
                    args = tool_call.function.arguments

                if tool_call.function.name == "search_lore":
                    query = args.get("query", "") if isinstance(args, dict) else args
                    report_progress(f"写作中检索设定：{query}", "tooling")
                    tool_result = search_lore(query, novel_id=novel_id)
                elif tool_call.function.name == "read_chapter":
                    chapter_num = args.get("chapter_num", -1) if isinstance(args, dict) else -1
                    if isinstance(chapter_num, str) and chapter_num.isdigit():
                        chapter_num = int(chapter_num)
                    report_progress(f"写作中翻阅历史章节：第 {chapter_num} 章", "tooling")
                    tool_result = read_chapter(chapter_num, novel_id=novel_id)
                    
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": tool_result
                })
        elif hasattr(response, "content") and response.content:
            final_content = response.content
            break
        else:
            final_content = str(response)
            break

    final_content = finalize_generated_chapter_text(final_content, chapter_num, state.get("chapter_outline", ""))
    final_content = ensure_chapter_word_count(
        final_content,
        chapter_num=chapter_num,
        target_words=target_words,
        min_words=min_words,
        max_words=max_words,
    )
    final_content = finalize_generated_chapter_text(final_content, chapter_num, state.get("chapter_outline", ""))
    if len(final_content) < min_words:
        final_content = ensure_chapter_word_count(
            final_content,
            chapter_num=chapter_num,
            target_words=target_words,
            min_words=min_words,
            max_words=max_words,
        )
        final_content = finalize_generated_chapter_text(final_content, chapter_num, state.get("chapter_outline", ""))
    word_count = len(final_content)
    print(f"  ✅ 正文生成完成（约 {word_count} 字）")
    report_progress(f"正文生成完成，约 {word_count} 字", "writing")
    return {"chapter_content": final_content}
