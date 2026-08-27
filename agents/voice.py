"""Character voice guide node.

This module turns existing character cards and project cast data into a
deterministic dialogue constraint card for the current chapter. It avoids an
extra LLM call so the writing pipeline stays fast and predictable.
"""
from __future__ import annotations

from typing import Any

from memory import DEFAULT_NOVEL_ID, StoryBible
from state import NovelState
from task_progress import report_progress


MAX_CHARACTERS = 6

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


def _merge_fields(card: dict[str, Any]) -> dict[str, Any]:
    fields = card.get("fields") if isinstance(card.get("fields"), dict) else {}
    merged = {**fields}
    for key in ("name", "role", "traits", "current_status", "goal", "relationship_to_protagonist", "note"):
        if key in card and card.get(key) not in ("", None):
            merged.setdefault(key, card.get(key))
    return merged


def _card_name(card: dict[str, Any]) -> str:
    fields = _merge_fields(card)
    return _text(card.get("name") or fields.get("name") or fields.get("姓名"), 30)


def _card_blob(card: dict[str, Any]) -> str:
    fields = _merge_fields(card)
    return " ".join(
        _text(fields.get(key), 120)
        for key in (
            "name",
            "role",
            "角色",
            "身份",
            "traits",
            "性格",
            "current_status",
            "goal",
            "relationship_to_protagonist",
            "note",
        )
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


def _is_relevant(card: dict[str, Any], chapter_text: str) -> bool:
    name = _card_name(card)
    if name and name in chapter_text:
        return True
    fields = _merge_fields(card)
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
    fields = _merge_fields(card)
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


def build_character_voice_guide(state: NovelState) -> dict:
    chapter_num = state.get("current_chapter", 1)
    report_progress(f"正在生成第 {chapter_num} 章角色声音约束...", "planning")

    novel_id = state.get("novel_id", DEFAULT_NOVEL_ID)
    bible = StoryBible(novel_id)
    chapter_text = "\n".join(
        str(state.get(key, "") or "")
        for key in (
            "chapter_outline",
            "chapter_drama_card",
            "chapter_controls_text",
            "story_so_far",
            "global_outline",
        )
    )

    cards = []
    try:
        cards.extend(bible.get_entity_cards("character"))
    except Exception:
        cards = []
    cards.extend(_cast_from_refinement(state))
    cards = _dedupe_cards(cards)

    selected = [card for card in cards if _is_relevant(card, chapter_text)]
    if not selected:
        selected = cards[:MAX_CHARACTERS]
    else:
        selected = selected[:MAX_CHARACTERS]

    if selected:
        lines = ["【本章角色声音约束】"]
        lines.extend(_format_character_line(card) for card in selected)
    else:
        lines = [
            "【本章角色声音约束】",
            "- 当前缺少明确角色卡：写作时仍需让主角、对手、盟友的对白在句式、词汇、情绪强度上明显区分。",
        ]

    lines.extend(
        [
            "【对白执行规则】",
            "1. 每段关键对白前后必须穿插动作、神态、心理或场景变化，不写连续问答记录。",
            "2. 每个主要角色至少有一种稳定的说话习惯、压迫方式或情绪遮掩方式。",
            "3. 冲突对白必须推动筹码变化：暴露信息、逼迫选择、改变关系或制造下一步行动。",
            "4. 避免所有角色都用同一种解释腔；该沉默时用动作和停顿承载潜台词。",
        ]
    )
    guide = "\n".join(lines)
    print(f"  [角色声音] 已生成第{chapter_num}章对白约束")
    report_progress("角色声音约束已生成", "planning")
    return {"character_voice_guide": guide}
