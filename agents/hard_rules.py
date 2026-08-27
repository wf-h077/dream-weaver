"""硬规则一致性校验（不调 LLM，纯 Python + 正则 + JSON 解析）

检测类型：
1. 死亡角色复活
2. 物品归属冲突
3. 境界倒退
4. 已毁/已失物品被继续使用
5. 地点规则违背（基础版）

用途：作为 editor 节点的补充风险源，对 LLM 自身一致性做"硬约束"校验。
"""
from __future__ import annotations

import json
import re
from typing import Any


# ═══════════════════════════════════════════════════════
# 关键词与模式
# ═══════════════════════════════════════════════════════

# 死亡/已故/陨落 关键词（用于在 status / fields 中识别）
DEATH_KEYWORDS = [
    "死亡", "已故", "已逝", "陨落", "殒命", "身死", "身死道消",
    "魂飞魄散", "形神俱灭", "牺牲", "殉情", "仙逝", "坐化", "圆寂",
    "亡故", "驾鹤", "魂归", "gone", "dead", "deceased",
]

# 物品被毁/丢失/封印 关键词
ITEM_LOST_KEYWORDS = [
    "已毁", "已碎", "已丢失", "已封印", "已消失", "已上交", "已销毁",
    "被毁", "被夺", "被封印", "被没收", "已转交", "已赠出", "丢失",
    "消失", "毁坏", "毁灭", "销毁", "化为灰烬", "碎裂", "崩解",
    "已不在", "已无",
]

# 主动行为动词（用于检测死亡角色"复活"）
ACTION_VERBS = [
    "说", "说道", "说:", "说：", "道", "喝道", "冷笑", "点头", "摇头",
    "出手", "攻击", "防御", "躲避", "闪避", "后退", "前进",
    "跑", "走", "站", "坐", "握", "抓住", "松开",
    "看", "望", "盯", "凝视", "回头", "转身", "扭头",
    "张嘴", "闭嘴", "皱眉", "笑", "哭", "怒", "吼", "喝",
    "拍", "打", "抓", "扔", "挥", "拉", "推", "踹", "踢", "刺", "砍",
    "举起", "放下", "抬起", "压低",
    "冲", "扑", "跃", "飞", "闪", "退", "攻", "挡", "截",
]

# 中文网文常见境界名（按出现频次粗排）
REALM_PATTERNS = [
    r"炼气期", r"练气期", r"筑基期", r"结丹期", r"金丹期", r"元婴期",
    r"化神期", r"合体期", r"大乘期", r"渡劫期", r"地仙境", r"天仙境",
    r"斗者", r"斗师", r"大斗师", r"斗灵", r"斗王", r"斗皇", r"斗宗",
    r"斗尊", r"斗圣", r"斗帝",
    r"武者", r"武师", r"武宗", r"武王", r"武帝", r"武圣",
    r"F级", r"E级", r"D级", r"C级", r"B级", r"A级",
    r"S级", r"SS级", r"SSS级", r"SR级", r"UR级",
    r"一级", r"二级", r"三级", r"四级", r"五级",
    r"六级", r"七级", r"八级", r"九级", r"十级",
    r"一转", r"二转", r"三转", r"四转", r"五转",
    r"六转", r"七转", r"八转", r"九转",
    r"黄阶", r"玄阶", r"地阶", r"天阶", r"王阶", r"皇阶", r"圣阶", r"帝阶",
    r"青铜", r"白银", r"黄金", r"铂金", r"钻石", r"星耀", r"王者",
]

# 境界强度排序（数值越大越强）—— 简化版，覆盖主流体系
REALM_RANK = {}
for i, realm in enumerate([
    # 修仙体系
    "炼气期", "练气期", "筑基期", "结丹期", "金丹期", "元婴期",
    "化神期", "合体期", "大乘期", "渡劫期", "地仙境", "天仙境",
    # 斗破体系
    "斗者", "斗师", "大斗师", "斗灵", "斗王", "斗皇", "斗宗",
    "斗尊", "斗圣", "斗帝",
    # 武道体系
    "武者", "武师", "武宗", "武王", "武帝", "武圣",
    # 等级体系
    "F级", "E级", "D级", "C级", "B级", "A级", "S级", "SS级", "SSS级", "SR级", "UR级",
    "一级", "二级", "三级", "四级", "五级", "六级", "七级", "八级", "九级", "十级",
    "一转", "二转", "三转", "四转", "五转", "六转", "七转", "八转", "九转",
    "黄阶", "玄阶", "地阶", "天阶", "王阶", "皇阶", "圣阶", "帝阶",
    "青铜", "白银", "黄金", "铂金", "钻石", "星耀", "王者",
]):
    REALM_RANK[realm] = i


# ═══════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════

def _parse_json_maybe(value: Any) -> Any:
    """把字符串 JSON 解析为 dict/list，失败返回原值。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _ensure_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _ensure_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            data = json.loads(value)
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def _is_text_in(value: Any, keywords: list[str]) -> bool:
    """检查 value 的字符串表示中是否含任一关键词。"""
    if value is None:
        return False
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return any(kw in text for kw in keywords)


def _extract_realms(text: str) -> set[str]:
    """从文本中提取所有出现的境界名。"""
    found = set()
    for pattern in REALM_PATTERNS:
        for m in re.finditer(pattern, text):
            found.add(m.group(0))
    return found


def _safe_find(content: str, target: str) -> list[int]:
    """在 content 中找 target 所有出现位置。"""
    positions = []
    start = 0
    while True:
        idx = content.find(target, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + len(target)
    return positions


# ═══════════════════════════════════════════════════════
# 检测 1：死亡角色复活
# ═══════════════════════════════════════════════════════

def detect_dead_character_resurrection(
    chapter_content: str,
    entity_status: Any,
    entity_cards: Any,
) -> list[dict]:
    """死亡/已故角色在本章出现主动行为 → must_fix

    数据源：
    - entity_status（dict 或 JSON 字符串）：{角色名: {状态字段}}
    - entity_cards（list of dict）：角色卡 / 物品卡

    判定：设定中含死亡/已故关键词的角色，在本章中作为主语+主动动词出现。
    """
    risks: list[dict] = []
    if not chapter_content:
        return risks

    status_dict = _ensure_dict(_parse_json_maybe(entity_status))
    cards = _ensure_list(_parse_json_maybe(entity_cards))

    # 汇总死亡角色
    dead_chars: dict[str, str] = {}  # name -> 死亡来源描述

    for name, fields in status_dict.items():
        if not isinstance(name, str) or not name or len(name) > 30:
            continue
        if _is_text_in(fields, DEATH_KEYWORDS):
            dead_chars[name] = "entity_status"

    for card in cards:
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or card.get("fields", {}).get("name") or "").strip()
        if not name or name in dead_chars:
            continue
        card_type = card.get("card_type", "")
        # 角色卡才检查；物品卡可能在描述"被毁"
        if card_type and card_type != "character" and "角色" not in card_type:
            continue
        if _is_text_in(card.get("fields", {}), DEATH_KEYWORDS):
            dead_chars[name] = f"{card_type or 'character'}_card"

    if not dead_chars:
        return risks

    # 检测本章中死亡角色是否有主动行为
    verbs_pattern = "|".join(re.escape(v) for v in ACTION_VERBS)
    for name, source in dead_chars.items():
        if len(name) < 2 or len(name) > 20:
            continue
        # 模式：人名 + 0-20 字符 + 主动动词
        pattern = re.compile(
            rf"({re.escape(name)})(.{{0,20}})({verbs_pattern})",
            re.UNICODE,
        )
        for m in pattern.finditer(chapter_content):
            full = m.group(0)
            # 排除明显是"回忆/描述/遗言"语境
            context_before = chapter_content[max(0, m.start() - 30):m.start()]
            if any(kw in context_before for kw in ["回忆", "梦里", "梦中", "曾", "当年", "以前", "遗物", "墓", "坟", "遗像", "雕像", "的魂", "之魂", "魂魄", "鬼魂", "幻象", "虚影"]):
                continue
            risks.append({
                "category": "硬规则/死亡角色复活",
                "severity": "must_fix",
                "message": (
                    f"角色「{name}」在设定（{source}）中标记为已死亡/已故，"
                    f"但本章出现了主动行为：「{full[:50]}」"
                ),
                "suggestion": (
                    f"删除该主动行为或改为回忆/遗物/幻象描写，"
                    f"不可让「{name}」作为活人行动"
                ),
                "evidence": full[:120],
            })
            break  # 每个角色最多报一次

    return risks


# ═══════════════════════════════════════════════════════
# 检测 2：物品归属冲突
# ═══════════════════════════════════════════════════════

# 物品归属字段候选名
OWNER_FIELD_KEYS = ["持有者", "持有", "主人", "归属", "owner", "belongs_to", "持剑者", "执掌者", "掌管"]


def _extract_owner_from_fields(fields: Any) -> str:
    """从 card.fields 中找持有者。"""
    if not isinstance(fields, dict):
        return ""
    for key in OWNER_FIELD_KEYS:
        val = fields.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    # 也尝试搜描述性字段中的归属线索
    for key, val in fields.items():
        if not isinstance(val, str):
            continue
        if "的" in val and any(kw in val for kw in ["剑", "刀", "玉佩", "法宝", "灵器", "灵宝", "丹", "炉"]):
            return val.strip()
    return ""


def detect_item_ownership_conflict(
    chapter_content: str,
    entity_cards: Any,
) -> list[dict]:
    """物品被不属于持有者的人使用 → must_fix

    简化版：检测"X 拿起/拔出/使用 Y（物品）"形式，但物品的持有者不是 X。
    """
    risks: list[dict] = []
    if not chapter_content:
        return risks

    cards = _ensure_list(_parse_json_maybe(entity_cards))
    item_owner_map: dict[str, str] = {}  # item_name -> owner_name

    for card in cards:
        if not isinstance(card, dict):
            continue
        card_type = card.get("card_type", "")
        if card_type and card_type != "item" and "物品" not in card_type:
            continue
        name = str(card.get("name") or card.get("fields", {}).get("name") or "").strip()
        if not name or len(name) > 30:
            continue
        owner = _extract_owner_from_fields(card.get("fields", {}))
        if owner:
            item_owner_map[name] = owner

    if not item_owner_map:
        return risks

    # 检测本章中物品被使用 + 检查使用者是否和持有者一致
    use_verbs = ["拿起", "拔出", "取出", "拔出", "祭出", "抛出", "使用", "捏碎",
                 "挥动", "催动", "激活", "开启", "戴上", "握紧", "祭起",
                 "引动", "驱动", "释放", "放", "抽", "掏", "举", "抬", "拿"]
    use_pattern = "|".join(re.escape(v) for v in use_verbs)

    # 模式：人名 + 使用动词 + 物品名（顺序）
    # 也检测：物品名 + 使用动词 + 人名（被动/所属）
    name_pattern = re.compile(
        rf"([\u4e00-\u9fff]{{2,8}}?)\s*({use_pattern})\s*([\u4e00-\u9fff]{{2,10}}?)",
        re.UNICODE,
    )

    for item_name, owner in item_owner_map.items():
        if len(item_name) < 2:
            continue
        # 在本章中找包含 item_name 的句子
        for match in re.finditer(re.escape(item_name), chapter_content):
            pos = match.start()
            # 取前后 40 字符作为上下文
            ctx_start = max(0, pos - 40)
            ctx_end = min(len(chapter_content), pos + len(item_name) + 40)
            context = chapter_content[ctx_start:ctx_end]
            # 提取最近的人名（2-8 字中文字符，且不是物品名本身）
            name_m = re.search(r"([\u4e00-\u9fff]{2,8})", context)
            if not name_m:
                continue
            actor = name_m.group(1)
            if actor == item_name or item_name in actor:
                continue
            # 检查 actor 是否就是 owner
            if actor == owner or owner in actor or actor in owner:
                continue
            # 可能是"他/她"指代 owner，跳过
            if actor in ("他", "她", "它", "此人", "那人", "主角", "那人影"):
                continue
            risks.append({
                "category": "硬规则/物品归属冲突",
                "severity": "warning",  # 降为 warning 因为可能是误判
                "message": (
                    f"物品「{item_name}」设定持有者为「{owner}」，"
                    f"但本章上下文出现了「{actor}」与该物品的关联"
                ),
                "suggestion": (
                    f"确认「{actor}」是否真的可以使用「{item_name}」，"
                    f"或将其改为「{owner}」的动作"
                ),
                "evidence": context[:80],
            })
            break  # 每个物品最多报一次

    return risks


# ═══════════════════════════════════════════════════════
# 检测 3：境界倒退
# ═══════════════════════════════════════════════════════════════

def detect_realm_regression(
    chapter_content: str,
    entity_status: Any,
) -> list[dict]:
    """角色在 entity_status 中已记录境界 X，本章却出现"跌落至 Y"且 Y < X → warning

    注意：合理退境（如自愿散功、破境失败）不算冲突，所以这个检测偏保守。
    """
    risks: list[dict] = []
    if not chapter_content:
        return risks

    status_dict = _ensure_dict(_parse_json_maybe(entity_status))
    if not status_dict:
        return risks

    for name, fields in status_dict.items():
        if not isinstance(name, str) or not isinstance(fields, dict):
            continue
        current_realm = None
        for key in ("境界", "修为", "realm", "level", "等级"):
            if key in fields and isinstance(fields[key], str):
                current_realm = fields[key]
                break
        if not current_realm or current_realm not in REALM_RANK:
            continue

        # 在本章中查找该角色出现的"境界跌落"语句
        if name not in chapter_content:
            continue

        # 跌落线索
        fall_keywords = ["跌落", "倒退", "退境", "散功", "破境失败", "境界崩塌",
                         "修为尽失", "退回", "降为", "降到", "退到"]
        for kw in fall_keywords:
            for m in re.finditer(rf"{re.escape(name)}.{{0,30}}{re.escape(kw)}.{{0,30}}", chapter_content):
                snippet = m.group(0)
                # 在该片段中找境界
                realms_in_snippet = _extract_realms(snippet)
                # 如果有比当前境界更低的，且非自愿线索
                lower_realms = [r for r in realms_in_snippet
                                if r in REALM_RANK and REALM_RANK[r] < REALM_RANK[current_realm]]
                if lower_realms:
                    # 检查是否是"自愿/暂避"等合理场景
                    if any(safe_kw in snippet for safe_kw in ["主动", "自愿", "故意", "暂避", "暂时", "压制", "封印自身"]):
                        continue
                    risks.append({
                        "category": "硬规则/境界倒退",
                        "severity": "warning",
                        "message": (
                            f"角色「{name}」当前境界 {current_realm}，"
                            f"但本章出现「{snippet[:40]}」，可能倒退到 {lower_realms}"
                        ),
                        "suggestion": (
                            f"确认是剧情需要的合理退境（如散功、破境失败），"
                            f"否则修正章节或更新 entity_status"
                        ),
                        "evidence": snippet[:120],
                    })
                    break

    return risks


# ═══════════════════════════════════════════════════════
# 检测 4：已毁/已失物品被继续使用
# ═══════════════════════════════════════════════════════

def detect_lost_item_reuse(
    chapter_content: str,
    entity_cards: Any,
) -> list[dict]:
    """物品卡标记为已毁/已失/已封印，本章却被描写为可用 → must_fix"""
    risks: list[dict] = []
    if not chapter_content:
        return risks

    cards = _ensure_list(_parse_json_maybe(entity_cards))
    lost_items: dict[str, str] = {}

    for card in cards:
        if not isinstance(card, dict):
            continue
        card_type = card.get("card_type", "")
        if card_type and card_type != "item" and "物品" not in card_type:
            continue
        name = str(card.get("name") or card.get("fields", {}).get("name") or "").strip()
        if not name or len(name) > 30:
            continue
        fields = card.get("fields", {})
        if _is_text_in(fields, ITEM_LOST_KEYWORDS):
            lost_items[name] = "物品卡标记为已毁/已失"

    if not lost_items:
        return risks

    use_verbs = ["拿起", "拔出", "取出", "使用", "挥动", "催动", "激活", "戴上",
                 "握紧", "祭起", "引动", "驱动", "释放", "拔出", "掏出", "捏住"]
    use_pattern = "|".join(re.escape(v) for v in use_verbs)

    for item_name, reason in lost_items.items():
        if len(item_name) < 2:
            continue
        # 在本章中找该物品 + 使用动词的搭配
        for m in re.finditer(
            rf"({re.escape(item_name)})(.{{0,5}})({use_pattern})",
            chapter_content,
        ):
            full = m.group(0)
            # 排除"已毁的剑仍然..."这种本身就是描述丢失的语境
            ctx_before = chapter_content[max(0, m.start() - 20):m.start()]
            if any(kw in ctx_before for kw in ["碎", "失", "毁", "封", "化", "不"]):
                continue
            risks.append({
                "category": "硬规则/已毁物品被使用",
                "severity": "must_fix",
                "message": (
                    f"物品「{item_name}」{reason}，但本章被描写为可用：「{full[:40]}」"
                ),
                "suggestion": (
                    f"删除「{item_name}」的使用场景，或更新物品卡移除「已毁/已失」标记"
                ),
                "evidence": full[:120],
            })
            break  # 每个物品最多报一次

    return risks


# ═══════════════════════════════════════════════════════
# 检测 5：地点规则违背（基础版）
# ═══════════════════════════════════════════════════════

def detect_location_rule_violation(
    chapter_content: str,
    world_rules: Any,
) -> list[dict]:
    """检测世界规则中的硬性禁令是否被本章违反 → warning

    简化策略：扫描 world_rules 中含"禁"/"不能"/"不可"等否定词的规则，
    检查本章是否包含规则中提到的实体。
    """
    risks: list[dict] = []
    if not chapter_content:
        return risks

    rules = _ensure_list(_parse_json_maybe(world_rules))
    if not rules:
        return risks

    forbidden_keywords = ["禁止", "不能", "不可", "严禁", "不允许", "禁制", "禁令"]
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_text = str(rule.get("rule_text", "") or rule.get("text", "")).strip()
        if not rule_text:
            continue
        if not any(kw in rule_text for kw in forbidden_keywords):
            continue
        # 提取规则中的关键实体（简单按"禁 X"模式）
        # 模式：禁止/不能 + 实体名词
        entity_match = re.search(r"(禁止|不能|不可|严禁|不允许).{0,3}?([\u4e00-\u9fff]{2,10})", rule_text)
        if not entity_match:
            continue
        entity = entity_match.group(2)
        if entity in chapter_content and len(entity) >= 2:
            # 不一定就是违反，可能是讨论规则本身
            # 简单启发：如果 entity 周围 30 字符内出现"用/做/进行"等主动动词，可能违反
            for m in re.finditer(re.escape(entity), chapter_content):
                ctx = chapter_content[max(0, m.start() - 30):min(len(chapter_content), m.end() + 30)]
                if any(v in ctx for v in ["使用", "进行", "做", "施展", "催动", "启动", "踏入", "进入"]):
                    risks.append({
                        "category": "硬规则/世界规则违背",
                        "severity": "warning",
                        "message": (
                            f"世界规则提到「{entity}」相关禁止/限制：{rule_text[:60]}"
                            f"，但本章出现相关主动行为"
                        ),
                        "suggestion": "确认是否剧情豁免，否则删除相关描写",
                        "evidence": ctx[:80],
                    })
                    break

    return risks


# ═══════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════

def run_hard_rules_check(
    chapter_content: str,
    entity_status: Any = None,
    entity_cards: Any = None,
    world_rules: Any = None,
) -> list[dict]:
    """运行所有硬规则检查，返回合并后的风险列表。"""
    all_risks: list[dict] = []
    all_risks.extend(detect_dead_character_resurrection(chapter_content, entity_status, entity_cards))
    all_risks.extend(detect_item_ownership_conflict(chapter_content, entity_cards))
    all_risks.extend(detect_realm_regression(chapter_content, entity_status))
    all_risks.extend(detect_lost_item_reuse(chapter_content, entity_cards))
    all_risks.extend(detect_location_rule_violation(chapter_content, world_rules))
    return all_risks
