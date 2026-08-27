"""章节创作指令包节点

把原本分散的 4 个节点（pattern / voice / promise / drama_card）合并为
单次 LLM 调用，节省 3 次 prompt token + 调度开销。

合并后节点输出：
- pattern_card   章节样板（从 8 个爆款结构里挑最匹配的）
- voice_card     角色声音约束
- promise_card   读者期待台账
- drama_card     本章戏剧卡（爽点、冲突、钩子）
- chapter_brief  上述 4 段拼接成的整段文本，writer 节点直接消费
"""
from __future__ import annotations

import json
import re
from typing import Any

from memory import DEFAULT_NOVEL_ID, StoryBible
from models import call_llm
from prompts import (
    BRIEF_COMPOSER_PROMPT,
    BRIEF_COMPOSER_SYSTEM,
    get_story_phase,
)
from prompts_style_presets import build_style_preset_prompt_block
from state import NovelState
from task_progress import report_progress


# ═══════════════════════════════════════════════════════
# 8 套爆款章节样板（继承自原 patterns.py，硬编码不调 LLM）
# ═══════════════════════════════════════════════════════

CHAPTER_PATTERNS = [
    {
        "name": "压迫反击",
        "keywords": ["退婚", "羞辱", "压迫", "打脸", "反击", "挑衅", "看不起", "逼迫"],
        "card": "【章节样板：压迫反击】\n结构节拍：\n1. 开场立刻给压力：主角被公开轻视、逼迫、质疑或陷入不利局面。\n2. 对手升级压迫：让对手说出更过分的条件或展示更强筹码。\n3. 主角忍住不解释：先观察漏洞，制造读者期待。\n4. 关键证据/能力/身份露出一角：不要一次全揭，先打一记有效反击。\n5. 爽点兑现：对手当场失态，围观者认知反转。\n6. 章末抛出更大压迫源：让读者知道真正敌人还没登场。\n避免：不要一开局就无敌碾压到底；不要让主角只靠嘴炮。",
    },
    {
        "name": "危机逃生",
        "keywords": ["追杀", "逃", "危机", "围堵", "倒计时", "爆炸", "坍塌", "生死", "怪物"],
        "card": "【章节样板：危机逃生】\n结构节拍：\n1. 开场给即时危险：时间、空间、敌人或规则正在逼近。\n2. 明确失败代价：死、暴露、失去关键人物/物品或任务失败。\n3. 主角快速判断：用一个具体观察发现生路。\n4. 中段反噬：原计划出错，危险升级。\n5. 主角主动赌一把：用代价换出路。\n6. 暂时脱险但代价显现：受伤、暴露秘密或引出更大追踪者。\n避免：不要靠巧合逃生；不要连续描写奔跑却没有决策。",
    },
    {
        "name": "线索揭秘",
        "keywords": ["真相", "线索", "秘密", "调查", "证据", "档案", "监控", "谜团", "实验"],
        "card": "【章节样板：线索揭秘】\n结构节拍：\n1. 开场出现异常线索，必须具体可感：文件、痕迹、监控、话语漏洞。\n2. 主角提出错误假设，让读者跟着猜。\n3. 查证过程中遇到阻拦或误导。\n4. 中段揭开第一层真相，但它只解释一半问题。\n5. 发现更反常的新证据，推翻旧判断。\n6. 章末给出危险答案或关键名字。\n避免：不要用大段说明直接讲真相；线索必须推动行动。",
    },
    {
        "name": "副本破局",
        "keywords": ["副本", "规则", "游戏", "任务", "系统", "循环", "倒计时", "通关", "NPC"],
        "card": "【章节样板：副本破局】\n结构节拍：\n1. 开场展示规则限制或任务惩罚。\n2. 让普通解法失败，证明规则危险。\n3. 主角发现规则漏洞或隐藏条件。\n4. 用小试探验证漏洞，付出轻微代价。\n5. 借规则反杀/破局，让读者感到智商爽。\n6. 章末提示副本规则并非系统本意，背后有人操控。\n避免：不要让系统直接送答案；破局必须有推理过程。",
    },
    {
        "name": "情绪拉扯",
        "keywords": ["误会", "重逢", "告白", "背叛", "救", "婚", "爱", "恨", "亲情", "选择"],
        "card": "【章节样板：情绪拉扯】\n结构节拍：\n1. 开场让两方立场冲突或情绪错位。\n2. 对话表面克制，潜台词更锋利。\n3. 插入一个共同记忆/旧物/旧伤，唤起情绪。\n4. 一方做出伤人的选择，但有隐藏理由。\n5. 主角不靠哭诉，而靠行动证明立场。\n6. 章末留下关系变化：靠近、决裂、误会加深或新的承诺。\n避免：不要只写内心独白；情绪必须通过动作和选择表现。",
    },
    {
        "name": "升级兑现",
        "keywords": ["觉醒", "突破", "升级", "境界", "能力", "金手指", "修为", "技能", "传承"],
        "card": "【章节样板：升级兑现】\n结构节拍：\n1. 开场让主角能力不足的问题暴露。\n2. 给出升级条件或代价。\n3. 主角被逼到必须使用/突破的临界点。\n4. 升级过程伴随风险，不是无痛领奖。\n5. 新能力立刻用于解决眼前难题，完成爽点兑现。\n6. 章末揭示新能力的副作用或更高层规则。\n避免：不要只写光效和境界名；升级必须改变局势。",
    },
    {
        "name": "权谋博弈",
        "keywords": ["谈判", "布局", "交易", "势力", "联盟", "背叛", "权谋", "合同", "竞标"],
        "card": "【章节样板：权谋博弈】\n结构节拍：\n1. 开场给出利益桌面：谁要什么，谁怕什么。\n2. 对手先占上风，提出苛刻条件。\n3. 主角不正面硬刚，先抛出一个诱饵。\n4. 中段揭示主角提前布置的筹码。\n5. 局势反转，但主角也暴露部分底牌。\n6. 章末出现第三方入局，博弈升级。\n避免：不要让角色只说空话；每轮对话都要改变筹码。",
    },
    {
        "name": "过渡蓄势",
        "keywords": [],
        "card": "【章节样板：过渡蓄势】\n结构节拍：\n1. 承接上一章结果，先处理一个具体后果。\n2. 通过人物行动展示状态变化，不做流水账总结。\n3. 插入一个小冲突或小目标，让过渡章也有阻力。\n4. 更新关键关系、物品或规则，为后续大事件埋线。\n5. 章末明确下一章的行动方向。\n避免：不要整章聊天复盘；不要只解释设定而没有事件。",
    },
]


def select_pattern_by_keyword(text: str, chapter_num: int, num_chapters: int) -> dict:
    """基于关键词匹配选择样板（继承自原 patterns.py，无 LLM）。"""
    if not text:
        text = ""
    best = max(CHAPTER_PATTERNS, key=lambda p: sum(2 for k in p["keywords"] if k and k in text))
    if not any(k and k in text for p in CHAPTER_PATTERNS for k in p["keywords"]):
        # 0 命中：开局给压迫反击，其他阶段给过渡蓄势
        if chapter_num <= max(3, int(num_chapters * 0.08)):
            best = CHAPTER_PATTERNS[0]
        else:
            best = CHAPTER_PATTERNS[-1]
    return best


# ═══════════════════════════════════════════════════════
# 角色声音预设（继承自原 voice.py，纯 Python）
# ═══════════════════════════════════════════════════════

VOICE_PRESETS = [
    {
        "keys": ["主角", "男主", "女主", "核心"],
        "voice": "目标感强，少解释，多用判断句和行动句；被压迫时先反问再推进局面",
        "avoid": "长篇自我剖白、空泛鸡汤、被动等待别人救场",
    },
    {
        "keys": ["反派", "敌", "对手", "压迫", "退婚", "上司", "掌权"],
        "voice": "话里带压迫和利益计算，常用规则、身份、代价逼人让步",
        "avoid": "无脑咆哮、只会辱骂、把计划全部说透",
    },
    {
        "keys": ["智囊", "军师", "谋士", "冷静", "理性", "医生", "技术"],
        "voice": "句子克制，常用条件判断和风险提示，情绪不外露",
        "avoid": "突然热血、夸张感叹、没有证据就下结论",
    },
    {
        "keys": ["伙伴", "朋友", "闺蜜", "兄弟", "青梅", "亲近", "盟友"],
        "voice": "语气更生活化，会打断、吐槽或替主角担心，带关系里的熟悉感",
        "avoid": "像旁白一样解释设定、每句话都端着说",
    },
    {
        "keys": ["长辈", "师父", "导师", "父", "母", "家主", "掌门"],
        "voice": "更重分寸和试探，少说废话，常把情绪藏在命令或提醒里",
        "avoid": "过度絮叨、直接把所有秘密交代干净",
    },
    {
        "keys": ["恋人", "暧昧", "未婚", "妻", "夫", "爱慕"],
        "voice": "有潜台词和情绪停顿，用动作、沉默和反问承载关系张力",
        "avoid": "直白告白堆砌、油腻情话、脱离剧情的情感戏",
    },
]


def _text(value: Any, limit: int = 90) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "、".join(str(item) for item in value if item)
    elif isinstance(value, dict):
        value = "；".join(f"{k}:{v}" for k, v in value.items() if v not in ("", None, [], {}))
    value = str(value).strip().replace("\n", " ")
    if len(value) > limit:
        return value[:limit].rstrip() + "..."
    return value


def _merge_card_fields(card: dict[str, Any]) -> dict[str, Any]:
    fields = card.get("fields") if isinstance(card.get("fields"), dict) else {}
    merged = {**fields}
    for key in ("name", "role", "traits", "current_status", "goal", "relationship_to_protagonist", "note"):
        if key in card and card.get(key) not in ("", None):
            merged.setdefault(key, card.get(key))
    return merged


def _card_name(card: dict[str, Any]) -> str:
    fields = _merge_card_fields(card)
    return _text(card.get("name") or fields.get("name") or fields.get("姓名"), 30)


def _card_blob(card: dict[str, Any]) -> str:
    fields = _merge_card_fields(card)
    return " ".join(
        _text(fields.get(key), 120)
        for key in ("name", "role", "角色", "身份", "traits", "性格",
                    "current_status", "goal", "relationship_to_protagonist", "note")
        if fields.get(key)
    )


def _voice_rule_for(card: dict[str, Any]) -> tuple[str, str]:
    blob = _card_blob(card)
    matched_voice: list[str] = []
    matched_avoid: list[str] = []
    for preset in VOICE_PRESETS:
        if any(key in blob for key in preset["keys"]):
            matched_voice.append(preset["voice"])
            matched_avoid.append(preset["avoid"])
    if not matched_voice:
        matched_voice.append("根据身份和当前目标说话，句式、词汇和情绪强度要与其他角色拉开差异")
        matched_avoid.append("所有角色都用同一种解释腔、旁白腔或客套腔")
    return "；".join(dict.fromkeys(matched_voice[:2])), "；".join(dict.fromkeys(matched_avoid[:2]))


def _is_card_relevant(card: dict[str, Any], chapter_text: str) -> bool:
    name = _card_name(card)
    if name and name in chapter_text:
        return True
    fields = _merge_card_fields(card)
    for key in ("role", "角色", "身份", "relationship_to_protagonist"):
        value = _text(fields.get(key), 40)
        if value and value in chapter_text:
            return True
    return False


def _cast_from_refinement(state: NovelState) -> list[dict[str, Any]]:
    refinement = state.get("concept_refinement") or {}
    cast = refinement.get("main_cast") if isinstance(refinement, dict) else []
    if not isinstance(cast, list):
        return []
    cards = []
    for item in cast:
        if isinstance(item, dict):
            name = _text(item.get("name"), 30)
            if name:
                cards.append({"name": name, "fields": item, "card_type": "character"})
    return cards


def _dedupe_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for card in cards:
        name = _card_name(card)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(card)
    return result


def _format_character_line(card: dict[str, Any]) -> str:
    fields = _merge_card_fields(card)
    name = _card_name(card)
    role = _text(fields.get("role") or fields.get("角色") or fields.get("身份"), 45)
    traits = _text(fields.get("traits") or fields.get("性格"), 60)
    goal = _text(fields.get("goal") or fields.get("目标"), 60)
    relation = _text(fields.get("relationship_to_protagonist") or fields.get("关系"), 60)
    voice, avoid = _voice_rule_for(card)
    parts = [f"- {name}"]
    meta = " / ".join(item for item in (role, relation) if item)
    if meta:
        parts.append(f"（{meta}）")
    parts.append(f"：说话方式：{voice}")
    if traits:
        parts.append(f"；性格落点：{traits}")
    if goal:
        parts.append(f"；当前目标：{goal}")
    parts.append(f"；避免：{avoid}")
    return "".join(parts)


MAX_VOICE_CHARACTERS = 6


def build_voice_text(state: NovelState) -> str:
    """纯 Python 构建角色声音约束文本（无 LLM）。"""
    novel_id = state.get("novel_id", DEFAULT_NOVEL_ID)
    bible = StoryBible(novel_id)
    chapter_text = "\n".join(
        str(state.get(key, "") or "")
        for key in ("chapter_outline", "chapter_controls_text", "story_so_far", "global_outline")
    )

    cards: list[dict[str, Any]] = []
    try:
        cards.extend(bible.get_entity_cards("character"))
    except Exception:
        pass
    cards.extend(_cast_from_refinement(state))
    cards = _dedupe_cards(cards)

    selected = [c for c in cards if _is_card_relevant(c, chapter_text)]
    if not selected:
        selected = cards[:MAX_VOICE_CHARACTERS]
    else:
        selected = selected[:MAX_VOICE_CHARACTERS]

    lines = ["【本章角色声音约束】"]
    if selected:
        lines.extend(_format_character_line(c) for c in selected)
    else:
        lines.append("- 当前缺少明确角色卡：写作时仍需让主角、对手、盟友的对白在句式、词汇、情绪强度上明显区分。")

    lines.extend([
        "【对白执行规则】",
        "1. 每段关键对白前后必须穿插动作、神态、心理或场景变化，不写连续问答记录。",
        "2. 每个主要角色至少有一种稳定的说话习惯、压迫方式或情绪遮掩方式。",
        "3. 冲突对白必须推动筹码变化：暴露信息、逼迫选择、改变关系或制造下一步行动。",
        "4. 避免所有角色都用同一种解释腔；该沉默时用动作和停顿承载潜台词。",
    ])
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
# 读者期待台账（继承自原 promise.py，纯 Python）
# ═══════════════════════════════════════════════════════

def _text2(value: Any, limit: int = 140) -> str:
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
                    return "；".join(_text2(part, 100) for part in parts if part)
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
        value = _text2(controls.get(key), 100)
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
                content = _text2(item.get("content"), 100)
                target = item.get("target_chapter")
                if content:
                    result.append(f"第{target or chapter_num}章伏笔：{content}")
            else:
                content = _text2(item, 100)
                if content:
                    result.append(content)
    return result


def build_promise_text(state: NovelState) -> str:
    """纯 Python 构建读者期待台账（无 LLM）。"""
    chapter_num = int(state.get("current_chapter", 1) or 1)
    total = int(state.get("num_chapters", 100) or 100)

    chapter_outline = _text2(state.get("chapter_outline"), 220)
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

    lines.extend([
        "【执行规则】",
        "1. 本章开头 300 字内必须让读者知道当前压力、目标或悬念。",
        "2. 本章中段必须兑现一个小承诺：反击、发现、关系变化、能力进展或信息反转。",
        "3. 本章结尾必须留下一个具体可追读的问题，不能只写情绪口号。",
        "4. 如果回收旧伏笔，必须同时制造新的更高层级期待，维持连载牵引。",
    ])
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
# 戏剧卡：1 次 LLM 输出
# ═══════════════════════════════════════════════════════

def strip_json_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```json"):
        t = t[7:].strip()
    elif t.startswith("```"):
        t = t[3:].strip()
    if t.endswith("```"):
        t = t[:-3].strip()
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    return t.strip()


def parse_brief_json(raw_text: str) -> dict[str, str]:
    """解析 LLM 输出的 brief JSON。失败时返回带空字符串的完整 dict（避免 KeyError）。"""
    empty = {
        "pattern_card": "",
        "voice_card": "",
        "promise_card": "",
        "drama_card": "",
    }
    if not raw_text:
        return empty
    text = strip_json_fence(raw_text)

    # 1) 标准 JSON 解析
    data = None
    try:
        data = json.loads(text)
    except Exception:
        pass

    # 2) 修复尾部逗号后再试
    if data is None:
        text2 = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            data = json.loads(text2)
        except Exception:
            pass

    # 3) 尝试抽取第一个 { ... } 块
    if data is None:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                pass

    # 4) 截断场景：直接 regex 提取各 key 的字符串值
    if data is None or not isinstance(data, dict):
        data = {}
        # 匹配 "key": "value" 或 "key": value（无引号）
        for key in ("pattern_card", "voice_card", "promise_card", "drama_card"):
            # 尝试引号字符串
            m = re.search(
                rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)"',
                text,
                re.DOTALL,
            )
            if m:
                data[key] = m.group(1)
                continue
            # 尝试无引号字符串（到下一个逗号或右括号）
            m = re.search(
                rf'"{re.escape(key)}"\s*:\s*"([^"]*?)(?:"|,|\n|\}})',
                text,
                re.DOTALL,
            )
            if m:
                data[key] = m.group(1)
                continue

    if not isinstance(data, dict):
        return empty

    return {
        "pattern_card": str(data.get("pattern_card", "")).strip(),
        "voice_card": str(data.get("voice_card", "")).strip(),
        "promise_card": str(data.get("promise_card", "")).strip(),
        "drama_card": str(data.get("drama_card", "")).strip(),
    }


# ═══════════════════════════════════════════════════════
# 主节点
# ═══════════════════════════════════════════════════════

def build_world_rules_text(bible: StoryBible) -> str:
    rules = bible.get_world_rules()
    if not rules:
        return "暂无"
    return "\n".join(f"- [{item.get('category', '规则')}] {item.get('rule_text', '')}" for item in rules)


def build_author_instructions(state: NovelState) -> str:
    """汇总灵感碎片 + 控制项 + 待处理伏笔。"""
    parts: list[str] = []
    bible = StoryBible(state.get("novel_id", DEFAULT_NOVEL_ID))
    try:
        inspirations = bible.get_pending_inspirations()
    except Exception:
        inspirations = []
    chapter_num = int(state.get("current_chapter", 1) or 1)
    try:
        hooks = bible.get_pending_plot_hooks(chapter_num)
    except Exception:
        hooks = []

    if hooks:
        parts.append("【本章待处理伏笔】")
        for h in hooks:
            parts.append(f"- {h.get('content', '')}")
    if inspirations:
        parts.append("【作者灵感素材】")
        for ins in inspirations:
            parts.append(f"- {ins.get('content', '')} (标签: {ins.get('tags', '')})")
            try:
                bible.mark_inspiration_used(ins["id"])
            except Exception:
                pass

    controls_text = state.get("chapter_controls_text", "")
    if controls_text:
        parts.append(f"【本章写作控制面板】\n{controls_text}")
    return "\n".join(parts) if parts else "暂无"


def compose_chapter_brief(state: NovelState) -> dict:
    """LangGraph 节点：合并 pattern + voice + promise + drama_card。

    Returns:
        dict 包含:
        - chapter_pattern_card  （向后兼容旧字段）
        - character_voice_guide （向后兼容旧字段）
        - reader_promise_guide （向后兼容旧字段）
        - chapter_drama_card    （向后兼容旧字段）
        - chapter_brief          （合并后的整段文本，writer 优先使用）
    """
    chapter_num = state["current_chapter"]
    num_chapters = state.get("num_chapters", 100)
    report_progress(f"正在合成第 {chapter_num} 章创作指令包...", "planning")
    print(f"\n[BriefComposer] 正在合成第{chapter_num}章创作指令包...")

    # 上下文截断：避免长篇累积导致 prompt 超 32K token 限制
    try:
        from models import truncate_state_context
        state = truncate_state_context(state)
    except Exception as e:
        print(f"  ⚠️ 截断上下文失败: {e}")

    novel_id = state.get("novel_id", DEFAULT_NOVEL_ID)
    bible = StoryBible(novel_id)
    story_phase = get_story_phase(chapter_num, num_chapters)

    # 1. 准备硬约束输入
    pattern_text = "\n".join([
        state.get("novel_genre", ""),
        state.get("novel_style", ""),
        state.get("chapter_outline", ""),
        state.get("chapter_controls_text", ""),
        story_phase,
    ])
    selected_pattern = select_pattern_by_keyword(pattern_text, chapter_num, num_chapters)
    pattern_card_hint = selected_pattern["card"]
    print(f"  [Brief] 推荐样板：{selected_pattern['name']}")

    # 2. 纯 Python：voice + promise 文本（不调 LLM）
    voice_text = build_voice_text(state)
    promise_text = build_promise_text(state)
    print("  [Brief] 已生成 voice/promise 硬约束文本")

    # 3. 准备 LLM 输入
    world_rules_str = build_world_rules_text(bible)
    entity_cards_context = bible.get_entity_cards_context()
    structured_status = state.get("structured_status", "{}")
    if entity_cards_context:
        structured_status = f"{structured_status}\n\n{entity_cards_context}"
    style_fingerprint = bible.get_style_fingerprint_context()
    novel_style = state.get("novel_style", "风格不限")
    if style_fingerprint:
        novel_style = f"{novel_style}\n\n{style_fingerprint}"

    author_instructions = build_author_instructions(state)

    # 注入 style_preset_block（从 state 读取）
    style_preset_key = state.get("style_preset_key", "")
    preset_block = build_style_preset_prompt_block(style_preset_key)
    if not preset_block:
        preset_block = "（未选择风格预设，按小说类型与现有文风指纹自由发挥）"

    # 4. 单次 LLM 调用：让 LLM 把 voice/promise/drama 整合 + 微调 + 生成 drama_card
    prompt = BRIEF_COMPOSER_PROMPT.format(
        chapter_num=chapter_num,
        story_phase=story_phase,
        chapter_outline=state.get("chapter_outline", "暂无"),
        pattern_hint=pattern_card_hint,
        story_so_far=state.get("story_so_far", "目前是第一章，故事刚刚开始。"),
        bible_context=state.get("bible_context", "暂无"),
        structured_status=structured_status,
        world_rules=world_rules_str,
        novel_style=novel_style,
        voice_text=voice_text,
        promise_text=promise_text,
        author_instructions=author_instructions,
        style_preset_block=preset_block,
    )

    # SYSTEM prompt 也注入 preset
    system_with_preset = BRIEF_COMPOSER_SYSTEM.format(style_preset_block=preset_block)

    raw = call_llm(
        role="planner",
        system_prompt=system_with_preset,
        prompt=prompt,
        temperature=0.55,
        max_tokens=2400,
    )
    parsed = parse_brief_json(raw)

    # 5. 合并：LLM 输出优先，缺失时用纯 Python 兜底
    final_pattern = parsed["pattern_card"] or pattern_card_hint
    final_voice = parsed["voice_card"] or voice_text
    final_promise = parsed["promise_card"] or promise_text
    final_drama = parsed["drama_card"] or "本章需要明确冲突、爽点兑现、主角主动选择和章末钩子。"

    chapter_brief = "\n\n".join([
        final_pattern,
        final_voice,
        final_promise,
        final_drama,
    ]).strip()

    if not parsed.get("drama_card"):
        report_progress("戏剧卡 LLM 解析失败，已用兜底", "planning")
    else:
        report_progress("创作指令包已合成", "planning")
    print("  [BriefComposer] 创作指令包已合成")

    return {
        # 向后兼容旧字段
        "chapter_pattern_card": final_pattern,
        "character_voice_guide": final_voice,
        "reader_promise_guide": final_promise,
        "chapter_drama_card": final_drama,
        # 新字段：writer 优先消费
        "chapter_brief": chapter_brief,
    }
