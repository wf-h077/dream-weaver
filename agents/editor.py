"""逻辑质检智能体节点

职责：审核正文是否与设定一致，检查逻辑硬伤。
使用 Gemini 的长上下文能力进行全面比对。
"""
import json
import re

from models import call_llm
from prompts import (
    EDITOR_SYSTEM,
    EDITOR_PROMPT,
    CONSISTENCY_DETECTOR_SYSTEM,
    CONSISTENCY_DETECTOR_PROMPT,
    STYLE_DRIFT_DETECTOR_SYSTEM,
    STYLE_DRIFT_DETECTOR_PROMPT,
    get_story_phase,
)
from state import NovelState
from config import MAX_EDIT_ROUNDS, get_chapter_word_range
from task_progress import report_progress
from memory import StoryBible, DEFAULT_NOVEL_ID
from agents.text_quality import detect_repetition_issues
from agents.hard_rules import run_hard_rules_check


DETECTOR_SEVERITIES = {"must_fix", "warning", "suggestion"}


def split_report_items(text: str) -> list[str]:
    if not text:
        return []
    cleaned = text.strip()
    if not cleaned or cleaned.startswith("无") or cleaned in {"暂无", "无。", "无硬伤"}:
        return []
    items = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        line = line.lstrip("-*· ")
        if len(line) > 2 and line[0].isdigit() and line[1] in ".、)）":
            line = line[2:].strip()
        if line and not line.startswith("##"):
            items.append(line)
    return items or [cleaned]


def parse_detector_json(raw_text: str) -> list[dict]:
    if not raw_text:
        return []
    text = raw_text.strip()
    if text.startswith("```json"):
        text = text[7:].strip()
    elif text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()

    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except Exception:
            return []

    risks = data.get("risks", []) if isinstance(data, dict) else []
    if not isinstance(risks, list):
        return []
    normalized = []
    for item in risks:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message", "")).strip()
        if not message:
            continue
        severity = str(item.get("severity", "warning")).strip()
        if severity not in DETECTOR_SEVERITIES:
            severity = "warning"
        normalized.append({
            "category": str(item.get("category", "专项检测")).strip() or "专项检测",
            "severity": severity,
            "message": message,
            "suggestion": str(item.get("suggestion", "")).strip(),
            "evidence": str(item.get("evidence", "")).strip(),
        })
    return normalized


def build_world_rules_text(bible: StoryBible) -> str:
    rules = bible.get_world_rules()
    if not rules:
        return "暂无"
    return "\n".join(f"- [{item.get('category', '规则')}] {item.get('rule_text', '')}" for item in rules)


def build_plot_hooks_text(bible: StoryBible, chapter_num: int) -> str:
    hooks = bible.get_pending_plot_hooks(chapter_num)
    if not hooks:
        return "暂无"
    return "\n".join(f"- {item.get('content', '')}" for item in hooks)

def detect_platform_format_risks(chapter_content: str, min_words: int, max_words: int, word_count: int) -> list[dict]:
    risks = []
    content = chapter_content or ""
    stripped = content.strip()

    if word_count < min_words:
        risks.append({
            "category": "番茄发布格式/字数偏短",
            "severity": "must_fix",
            "message": f"本章约 {word_count} 字，低于目标下限 {min_words} 字。",
            "suggestion": "补足事件推进、人物反应、冲突升级和章末钩子，避免短章。",
            "evidence": "",
        })
    elif word_count > max_words:
        risks.append({
            "category": "番茄发布格式/字数偏长",
            "severity": "warning",
            "message": f"本章约 {word_count} 字，高于建议上限 {max_words} 字。",
            "suggestion": "考虑压缩重复心理、说明性段落或拆分为两章。",
            "evidence": "",
        })

    if re.search(r"```|^#{1,6}\s|^\s*[-*]\s|\*\*", content, re.MULTILINE):
        risks.append({
            "category": "番茄发布格式/非正文标记",
            "severity": "must_fix",
            "message": "正文中疑似出现 Markdown、列表符号或格式标记。",
            "suggestion": "删除 Markdown、列表符号、加粗符号，只保留可发布正文。",
            "evidence": "",
        })

    meta_patterns = ["以下是", "本章正文", "写作说明", "作者注", "AI", "大纲如下", "创作思路"]
    hit_meta = next((item for item in meta_patterns if item in stripped[:160]), "")
    if hit_meta:
        risks.append({
            "category": "番茄发布格式/提示语残留",
            "severity": "must_fix",
            "message": f"正文开头疑似残留非正文提示语：{hit_meta}",
            "suggestion": "删除提示语、作者说明和 AI 自述，让章节从小说正文直接开始。",
            "evidence": stripped[:80],
        })

    long_paragraphs = [
        para.strip() for para in re.split(r"\n+", content)
        if len(para.strip()) > 260
    ]
    if long_paragraphs:
        risks.append({
            "category": "番茄发布格式/段落过长",
            "severity": "warning",
            "message": f"发现 {len(long_paragraphs)} 个超长段落，不利于手机阅读。",
            "suggestion": "把长段拆成动作、心理、对白和场景变化更清晰的短段。",
            "evidence": long_paragraphs[0][:120],
        })

    for issue in detect_repetition_issues(content):
        if issue.get("category") == "段落过长":
            continue
        risks.append({
            "category": f"番茄发布格式/{issue.get('category', '重复低质')}",
            "severity": issue.get("severity", "warning"),
            "message": issue.get("message", "正文存在重复或低质表达。"),
            "suggestion": issue.get("suggestion", "删除重复内容，补充真实剧情推进。"),
            "evidence": "",
        })

    if not re.search(r"[。！？?!」”]$", stripped):
        risks.append({
            "category": "番茄发布格式/结尾不完整",
            "severity": "warning",
            "message": "章节结尾不像完整句子，可能是截断或生成未完成。",
            "suggestion": "补完整最后一句，并保留下一章期待。",
            "evidence": stripped[-80:],
        })

    return risks


# ═══════════════════════════════════════════════════════
# 增强格式检查：emoji / 装饰符号 / AI 套话
# ═══════════════════════════════════════════════════════

# Unicode emoji 范围
_EMOJI_RANGES = [
    (0x1F300, 0x1F5FF),   # Symbols & pictographs
    (0x1F600, 0x1F64F),   # Emoticons
    (0x1F680, 0x1F6FF),   # Transport & map
    (0x1F700, 0x1F77F),   # Alchemical
    (0x1F900, 0x1F9FF),   # Supplemental symbols
    (0x1FA00, 0x1FAFF),   # Extended symbols
    (0x2600, 0x26FF),     # Misc symbols (☀ ☁ ⚓)
    (0x2700, 0x27BF),     # Dingbats (✂ ✈ ✉)
    (0x1F1E6, 0x1F1FF),   # Flags
]

# 装饰/特殊符号（番茄正文风格偏向纯文字）
_DECORATIVE_SYMBOLS = set(
    "★☆✦✧✪✫✬✭✯✰"
    "◆◇◈◉◊"
    "▶▷▸▹►▻▼▽◀◁◂◃■□"
    "⬛⬜⬟⬠⬡"
    "❀✿❁❂❃❄❅❆❇❈"
    "⚡⚢⚣⚤⚥⚦⚧⚨⚩"
    "☀☁☂☃☄★☆"
    "♥♡❤💕💖💗💘💙💚💛💜💝💞💟"
    "🔥✨💫⭐🌟"
    "☠☢☣"
    "✂✄✆✇"
    "↩↪➡⬅⬆⬇"
    "⚙⚛⚜⚝"
)


def _is_emoji(ch: str) -> bool:
    """判断一个字符是否是 emoji（unicode range）。"""
    cp = ord(ch)
    for start, end in _EMOJI_RANGES:
        if start <= cp <= end:
            return True
    # 一些零散的特殊 emoji
    if cp in (0x2764, 0x2665, 0x2660, 0x2666, 0x2663, 0x2668, 0x270A, 0x270B, 0x270C, 0x2714, 0x2716, 0x271D, 0x2728, 0x272A, 0x2734, 0x2733):
        return True
    return False


def detect_emoji_and_symbol_risks(chapter_content: str) -> list[dict]:
    """检测正文中的 emoji 和装饰符号（番茄发布需剔除）。"""
    risks = []
    content = chapter_content or ""
    if not content.strip():
        return risks

    # 1) emoji 计数
    emoji_count = sum(1 for ch in content if _is_emoji(ch))
    if emoji_count > 0:
        # 收集前 5 个出现的 emoji 作为证据
        samples = []
        seen = set()
        for ch in content:
            if _is_emoji(ch) and ch not in seen:
                seen.add(ch)
                samples.append(ch)
            if len(samples) >= 5:
                break
        severity = "must_fix" if emoji_count >= 3 else "warning"
        risks.append({
            "category": "番茄发布格式/表情符号",
            "severity": severity,
            "message": f"正文中发现 {emoji_count} 个 emoji 符号。",
            "suggestion": "删除所有 emoji（😀❤⭐ 等），番茄正文风格偏向纯文字。",
            "evidence": f"示例: {''.join(samples)}",
        })

    # 2) 装饰符号
    deco_count = sum(1 for ch in content if ch in _DECORATIVE_SYMBOLS)
    if deco_count >= 3:
        samples = [ch for ch in content if ch in _DECORATIVE_SYMBOLS][:5]
        risks.append({
            "category": "番茄发布格式/装饰符号",
            "severity": "warning",
            "message": f"正文中发现 {deco_count} 个装饰符号（★☆▶◆ 等）。",
            "suggestion": "删除装饰符号，用纯文字或中文标点代替（如用「·」「-」「.」「…」「—」）。",
            "evidence": f"示例: {''.join(samples)}",
        })

    # 3) 特殊控制字符（除了正常的换行/制表符）
    weird_chars = []
    for ch in content:
        cp = ord(ch)
        if cp < 0x20 and ch not in "\n\r\t":
            weird_chars.append(ch)
        elif cp == 0x7F:  # DEL
            weird_chars.append(ch)
        elif 0x200B <= cp <= 0x200F:  # 零宽字符
            weird_chars.append(ch)
    if weird_chars:
        risks.append({
            "category": "番茄发布格式/特殊控制字符",
            "severity": "must_fix",
            "message": f"正文含 {len(weird_chars)} 个特殊控制字符或零宽字符。",
            "suggestion": "删除零宽空格等不可见字符。",
            "evidence": repr(weird_chars[:5]),
        })

    return risks


# AI 套话（典型 LLM 痕迹）
_AI_CLICHES = [
    # 开头套话
    r"^\s*在当今社会",
    r"^\s*在这个.{1,10}时代",
    r"^\s*在这个.{1,15}世界里",
    r"^\s*众所周知",
    r"^\s*作为一个.{1,15}(我|作者|写手|写作者|小说家)",
    r"^\s*作为一名",
    r"^\s*我(们)?一起来",
    r"^\s*让我(们)?一起",
    r"^\s*下面[，, ]?让我们",
    r"^\s*接下来[，, ]?让我",
    r"^\s*话说",
    r"^\s*话说回来",
    # 中段套话
    r"综上所述",
    r"由此可见",
    r"通过?以上(分析|描述|叙述)?(我们)?可以(看出|得知|发现)",
    r"这(让|使)?(我们|我|大家)?(不禁|不得不|需要)?(思考|反思|想起)",
    r"值得(注意|我们关注|深思|思考)(的是|的是)?",
    r"不得不说",
    r"不可否认",
    r"毋庸置疑",
    r"显然[，, ]",
    r"诚然",
    r"然而[，, ]+事实(并)?非(如此|这样)",
    r"故事(发生|开始)在一个",
    r"小(?:说|小的)(主人公|主角|说的)?(主角|主人公)?(叫做|名叫|名叫|叫)",
    # 结尾套话
    r"未完待续[。.]?$",
    r"欲知后事如何[，, ]?且听下回分解",
    r"请(看官|读者|大家)拭目以待",
    r"让我们(一起|拭目以待|共同)期待",
    r"敬请期待",
    # 中式总结
    r"总而言之",
    r"归根结底",
    r"总的来说",
]


def detect_ai_cliche_risks(chapter_content: str) -> list[dict]:
    """检测正文中的 AI 套话（典型 LLM 痕迹，番茄审核员看到会扣分）。"""
    risks = []
    content = chapter_content or ""
    if not content.strip():
        return risks

    # 头 200 字符检测开头套话
    head = content[:200]
    body = content

    hits_by_position = {"开头": [], "正文": []}
    for pattern in _AI_CLICHES:
        m = re.search(pattern, head, re.MULTILINE)
        if m:
            hits_by_position["开头"].append((pattern, m.group(0)))
            continue
        m = re.search(pattern, body, re.MULTILINE)
        if m:
            hits_by_position["正文"].append((pattern, m.group(0)))

    # 合并 hits
    all_hits = []
    for position, hits in hits_by_position.items():
        for pattern, matched in hits:
            all_hits.append((position, pattern, matched))

    if not all_hits:
        return risks

    # 严重度：开头套话 must_fix（最容易被发现），正文套话 warning
    has_head_cliche = bool(hits_by_position["开头"])
    severity = "must_fix" if has_head_cliche else "warning"
    samples = [f"[{pos}] {matched.strip()}" for pos, _, matched in all_hits[:5]]
    risks.append({
        "category": "番茄发布格式/AI 套话",
        "severity": severity,
        "message": f"正文检测到 {len(all_hits)} 处 AI 套话或开场套话。",
        "suggestion": "删除套话，直接进入剧情。'在当今社会''作为一个''综上所述''值得注意'等明显 LLM 痕迹必须改写。",
        "evidence": " | ".join(samples),
    })

    return risks


def strip_ai_cliches_and_emoji(text: str) -> str:
    """主动剥离 AI 套话 + emoji + 装饰符号 + 零宽字符。返回清理后的文本。"""
    if not text:
        return text
    out = text
    # 1) 剥 AI 套话（开头最多剥 3 次，循环处理"在当今社会..."等）
    for _ in range(3):
        original = out
        for pattern in _AI_CLICHES:
            out_new = re.sub(pattern, "", out, count=1, flags=re.MULTILINE)
            if out_new != out:
                out = out_new.lstrip()
                break
        if out == original:
            break
    # 2) 剥 emoji
    out = "".join(ch for ch in out if not _is_emoji(ch))
    # 3) 剥装饰符号
    out = "".join(ch for ch in out if ch not in _DECORATIVE_SYMBOLS)
    # 4) 剥零宽字符 / 控制字符
    out = re.sub(r"[\u200B-\u200F\uFEFF\u200E\u200F\u2028\u2029]", "", out)
    out = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", out)
    return out


def _dedupe_similar_risks(risks: list[dict], similarity_threshold: float = 0.5) -> list[dict]:
    """对风险列表做"同类去重"。

    规则：
    1. 相同 category 的多条风险，合并为 1 条（保留 severity 最高的）
    2. 相同 category 但 severity 都是 warning / suggestion，保留第一条
    3. 这样 patcher 不会重复处理同类问题

    Returns:
        去重后的风险列表（保留原始顺序中第一次出现的 category 槽位）
    """
    if not risks:
        return risks
    sev_rank = {"must_fix": 3, "warning": 2, "suggestion": 1, "pass": 0}
    # 按 category 合并：key = category，value = (idx_in_deduped, best_risk)
    deduped: list[dict] = []
    cat_to_idx: dict[str, int] = {}
    for r in risks:
        cat = r.get("category", "")
        if cat in cat_to_idx:
            # 同类已存在：保留 severity 更高的
            existing = deduped[cat_to_idx[cat]]
            if sev_rank.get(r.get("severity", "warning"), 0) > sev_rank.get(existing.get("severity", "warning"), 0):
                deduped[cat_to_idx[cat]] = r
        else:
            cat_to_idx[cat] = len(deduped)
            deduped.append(r)
    return deduped


def _msg_similarity(a: str, b: str) -> float:
    """简单的消息相似度（基于共同词比例）。"""
    if not a or not b:
        return 0.0
    import re as _re
    words_a = set(_re.findall(r"[\u4e00-\u9fff]{2,}|[\w]+", a))
    words_b = set(_re.findall(r"[\u4e00-\u9fff]{2,}|[\w]+", b))
    if not words_a or not words_b:
        return 0.0
    overlap = len(words_a & words_b)
    return overlap / max(len(words_a | words_b), 1)


def add_fanqie_rule_risk(risks: list[dict], category: str, severity: str, message: str, suggestion: str, evidence: str = ""):
    risks.append({
        "category": f"番茄发布安全/{category}",
        "severity": severity,
        "message": message,
        "suggestion": suggestion,
        "evidence": evidence[:140] if evidence else "",
    })


def first_regex_evidence(pattern: str, content: str) -> str:
    match = re.search(pattern, content, re.IGNORECASE)
    if not match:
        return ""
    start = max(0, match.start() - 40)
    end = min(len(content), match.end() + 60)
    return content[start:end].replace("\n", " ")


def detect_fanqie_content_safety_risks(chapter_content: str) -> list[dict]:
    """Deterministic pre-check based on Fanqie publication rejection categories.

    This is a conservative local detector: it flags likely risk for human/LLM
    review, but does not claim to replace the platform's official audit.
    """
    risks = []
    content = chapter_content or ""
    stripped = content.strip()
    compact = re.sub(r"\s+", "", stripped)

    if not stripped:
        add_fanqie_rule_risk(
            risks,
            "内容格式有误",
            "must_fix",
            "章节内容为空或接近空白。",
            "补全正文，确保章节有完整事件、段落和结尾。",
        )
        return risks

    first_line = next((line.strip() for line in stripped.splitlines() if line.strip()), "")
    title_sensitive = r"色情|低俗|赌博|吸毒|毒品|恐怖主义|极端主义|邪教|辱骂|仇恨|乱伦|未成年.*性|自杀教程"
    if first_line and len(first_line) <= 80 and re.search(title_sensitive, first_line):
        add_fanqie_rule_risk(
            risks,
            "标题不合适",
            "must_fix",
            "章节标题疑似包含违规、敏感或低俗信息。",
            "修改章节标题，让标题贴合正文核心事件，避免敏感、低俗、违规词。",
            first_line,
        )

    if "�" in content or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", content):
        add_fanqie_rule_risk(
            risks,
            "内容格式有误",
            "must_fix",
            "正文疑似存在乱码或非法控制字符。",
            "清理乱码和异常字符后再发布。",
            first_regex_evidence(r"�|[\x00-\x08\x0b\x0c\x0e-\x1f]", content),
        )

    if len(stripped) > 800 and "\n" not in stripped:
        add_fanqie_rule_risk(
            risks,
            "内容格式有误",
            "warning",
            "正文长篇未分段，不符合移动端阅读和平台分段要求。",
            "使用回车键自然分段，不要用空格模拟分段。",
        )

    traditional_hits = len(re.findall(r"[體臺妳裏麼後為與這個們來時會說讓]", content))
    if len(stripped) > 300 and traditional_hits / max(len(stripped), 1) > 0.03:
        add_fanqie_rule_risk(
            risks,
            "内容格式有误",
            "warning",
            "正文疑似存在较多繁体字。",
            "统一转换为简体中文。",
        )

    latin_count = len(re.findall(r"[A-Za-z]", content))
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", content))
    if len(stripped) > 300 and latin_count > chinese_count * 0.6:
        add_fanqie_rule_risk(
            risks,
            "内容格式有误",
            "warning",
            "正文疑似外文比例过高。",
            "除必要名词外，正文应以中文叙事为主。",
        )

    paragraphs = [p.strip() for p in re.split(r"\n+", stripped) if p.strip()]
    if len(paragraphs) >= 5:
        duplicated = len(paragraphs) - len(set(paragraphs))
        if duplicated >= 2 or duplicated / max(len(paragraphs), 1) > 0.25:
            add_fanqie_rule_risk(
                risks,
                "内容严重无意义",
                "must_fix",
                "正文存在多处重复段落，可能被判断为重复或低质内容。",
                "删除重复内容，补充真实剧情推进。",
            )

    if re.search(r"([^\w\s\u4e00-\u9fff])\1{12,}", content) or re.search(r"哈{20,}|啊{20,}|[~～]{10,}", content):
        add_fanqie_rule_risk(
            risks,
            "内容严重无意义",
            "must_fix",
            "正文疑似存在无意义符号、表情或重复字符堆叠。",
            "删除无意义符号和灌水内容，改为有效剧情描写。",
            first_regex_evidence(r"([^\w\s\u4e00-\u9fff])\1{12,}|哈{20,}|啊{20,}|[~～]{10,}", content),
        )

    unrelated_pattern = r"https?://|www\.|QQ群|Q群|微信|VX|vx|QQ号|公众号|加群|读者群|商务合作|广告|推广|打赏|求关注|吐槽平台|平台垃圾"
    evidence = first_regex_evidence(unrelated_pattern, content)
    if evidence:
        add_fanqie_rule_risk(
            risks,
            "含有无关信息",
            "must_fix",
            "正文疑似包含外链、广告、社交账号、读者群或平台吐槽等无关信息。",
            "删除与故事无关的联系方式、广告、外链、平台吐槽和导流信息。",
            evidence,
        )

    history_pattern = r"历史虚无|恶意歪曲|抹黑.*(历史人物|英雄|烈士|革命领袖)|英雄烈士.*(丑化|抹黑|侮辱)"
    evidence = first_regex_evidence(history_pattern, content)
    if evidence:
        add_fanqie_rule_risk(
            risks,
            "历史虚无主义",
            "must_fix",
            "正文疑似涉及恶意歪曲历史事实、历史人物或损害英雄烈士形象。",
            "删除或改写相关内容，避免对历史事实和历史人物作恶意抹黑。",
            evidence,
        )

    vulgar_pattern = r"色情|淫秽|性器官|裸体|性挑逗|性暗示|性行为|情趣用品|黄色广告|乱伦|多人性行为|意淫|窥探.*身体|未成年.*性"
    evidence = first_regex_evidence(vulgar_pattern, content)
    if evidence:
        add_fanqie_rule_risk(
            risks,
            "内容色情低俗",
            "must_fix",
            "正文疑似涉及色情低俗、性暗示、违背伦理或未成年人性相关内容。",
            "删除露骨描写和低俗表达；暧昧关系用情绪张力和剧情推进表达，不写越界细节。",
            evidence,
        )

    minor_pattern = r"未成年|少年|少女|学生|初中生|高中生|师生恋|早恋|霸凌|抽烟|斗殴|混社会|自杀|自残|怀孕|生子|恋童|虐待.*(儿童|少年|少女)"
    evidence = first_regex_evidence(minor_pattern, content)
    if evidence and re.search(r"未成年|学生|初中生|高中生|师生恋|恋童|怀孕|生子|自杀|自残|霸凌|抽烟|斗殴|混社会", evidence):
        add_fanqie_rule_risk(
            risks,
            "未成年人负面导向",
            "must_fix",
            "正文疑似涉及未成年人负面导向或损害未成年人身心健康的内容。",
            "避免宣扬或详细描写未成年人涉黄赌毒、早恋诱导、霸凌、斗殴、自杀自残、师生恋、怀孕生子等内容。",
            evidence,
        )

    unsuitable_pattern = (
        r"危害国家|泄露国家秘密|危害国家安全|恐怖主义|极端主义|邪教|宗教狂热|封建迷信|"
        r"民族仇恨|民族歧视|煽动.*仇恨|赌博|吸毒|贩毒|违法犯罪教程|教你.*(制毒|诈骗|洗钱|杀人|爆炸)|"
        r"侵犯.*(肖像权|名誉权|隐私权|姓名权|知识产权)|抹黑党|抹黑政府|时政负面"
    )
    evidence = first_regex_evidence(unsuitable_pattern, content)
    if evidence:
        add_fanqie_rule_risk(
            risks,
            "内容不适合发布",
            "must_fix",
            "正文疑似涉及违法有害、政治敏感、宗教极端、民族仇恨、侵权或其他不适合发布内容。",
            "删除或重写相关桥段，避免触碰平台内容安全底线。",
            evidence,
        )

    return risks


def format_detector_risks_for_patcher(risks: list[dict]) -> str:
    lines = []
    for idx, risk in enumerate(risks, 1):
        lines.append(f"### 专项风险 {idx}")
        lines.append(f"- 分类：{risk.get('category', '专项检测')}")
        lines.append(f"- 问题：{risk.get('message', '')}")
        if risk.get("evidence"):
            lines.append(f"- 依据：{risk.get('evidence')}")
        if risk.get("suggestion"):
            lines.append(f"- 修补建议：{risk.get('suggestion')}")
    return "\n".join(lines).strip()


def run_consistency_detector(
    state: NovelState,
    bible: StoryBible,
    chapter_num: int,
    edit_round: int,
    story_phase: str,
    min_words: int,
    max_words: int,
    word_count: int,
) -> list[dict]:
    """Run the classified consistency detector and persist its findings.

    The detector is intentionally non-blocking for now: it enriches the risk
    report panel, while the original editor decision still controls patching
    and rewrite flow.
    """
    report_progress("正在执行专项一致性分类检测...", "review")
    structured_status = state.get("structured_status", "{}")
    cards_context = bible.get_entity_cards_context()
    if cards_context:
        structured_status = f"{structured_status}\n\n{cards_context}"

    detector_prompt = CONSISTENCY_DETECTOR_PROMPT.format(
        chapter_num=chapter_num,
        story_so_far=state.get("story_so_far", "无（开篇）"),
        chapter_outline=state["chapter_outline"],
        bible_context=state.get("bible_context", "暂无"),
        structured_status=structured_status,
        world_rules=build_world_rules_text(bible),
        plot_hooks=build_plot_hooks_text(bible, chapter_num),
        story_phase=story_phase,
        min_words=min_words,
        max_words=max_words,
        word_count=word_count,
        chapter_content=state["chapter_content"],
    )
    raw_detector = call_llm(
        role="editor",
        system_prompt=CONSISTENCY_DETECTOR_SYSTEM,
        prompt=detector_prompt,
        temperature=0.1,
        max_tokens=4096,
    )
    risks = parse_detector_json(raw_detector)

    for risk in risks:
        evidence = risk.get("evidence", "")
        message = risk["message"]
        if evidence:
            message = f"{message}\n依据：{evidence}"
        severity = risk["severity"]
        bible.add_consistency_report(
            chapter_num=chapter_num,
            review_round=edit_round,
            severity=severity,
            category=f"专项检测·{risk['category']}",
            message=message,
            suggestion=risk.get("suggestion", ""),
            status="open" if severity == "must_fix" else "noted",
        )

    if risks:
        report_progress(f"专项一致性检测发现 {len(risks)} 条风险", "review")
    else:
        report_progress("专项一致性检测未发现额外风险", "review")
    return risks


def run_style_drift_detector(
    state: NovelState,
    bible: StoryBible,
    chapter_num: int,
    edit_round: int,
) -> list[dict]:
    style_context = bible.get_style_fingerprint_context()
    if not style_context:
        report_progress("未设置默认文风，跳过文风偏移检测", "review")
        return []

    report_progress("正在检测本章文风稳定性...", "review")
    detector_prompt = STYLE_DRIFT_DETECTOR_PROMPT.format(
        chapter_num=chapter_num,
        style_context=style_context,
        novel_style=state.get("novel_style", "风格不限"),
        chapter_outline=state.get("chapter_outline", ""),
        chapter_content=state.get("chapter_content", ""),
    )
    raw_detector = call_llm(
        role="editor",
        system_prompt=STYLE_DRIFT_DETECTOR_SYSTEM,
        prompt=detector_prompt,
        temperature=0.1,
        max_tokens=4096,
    )
    risks = parse_detector_json(raw_detector)

    for risk in risks:
        evidence = risk.get("evidence", "")
        message = risk["message"]
        if evidence:
            message = f"{message}\n依据：{evidence}"
        severity = risk["severity"]
        bible.add_consistency_report(
            chapter_num=chapter_num,
            review_round=edit_round,
            severity=severity,
            category=f"文风偏移·{risk['category']}",
            message=message,
            suggestion=risk.get("suggestion", ""),
            status="open" if severity == "must_fix" else "noted",
        )

    if risks:
        report_progress(f"文风稳定性检测发现 {len(risks)} 条偏移", "review")
    else:
        report_progress("文风稳定性检测通过", "review")
    return risks


def review_chapter(state: NovelState) -> dict:
    """
    LangGraph 节点：审核章节正文。
    输出：is_approved (bool) 和 edit_feedback (str)
    """
    chapter_num = state["current_chapter"]
    num_chapters = state.get("num_chapters", 100)
    edit_count = state.get("edit_count", 0)
    bible = StoryBible(state.get("novel_id", DEFAULT_NOVEL_ID))
    print(f"\n🔍 [逻辑质检] 正在审核第{chapter_num}章...")
    report_progress(f"正在审核第 {chapter_num} 章逻辑与设定一致性...", "review")

    # 计算当前故事阶段
    story_phase = get_story_phase(chapter_num, num_chapters)

    # 文本字数评估
    word_count = len(state["chapter_content"])
    target_words = state.get("chapter_target_words", 2000)
    min_words, max_words = get_chapter_word_range(target_words)
    platform_format_risks = detect_platform_format_risks(state["chapter_content"], min_words, max_words, word_count)
    fanqie_safety_risks = detect_fanqie_content_safety_risks(state["chapter_content"])
    for risk in platform_format_risks:
        bible.add_consistency_report(
            chapter_num=chapter_num,
            review_round=edit_count + 1,
            severity=risk["severity"],
            category=risk["category"],
            message=risk["message"] + (f"\n依据：{risk['evidence']}" if risk.get("evidence") else ""),
            suggestion=risk.get("suggestion", ""),
            status="open" if risk["severity"] == "must_fix" else "noted",
        )
    for risk in fanqie_safety_risks:
        bible.add_consistency_report(
            chapter_num=chapter_num,
            review_round=edit_count + 1,
            severity=risk["severity"],
            category=risk["category"],
            message=risk["message"] + (f"\n依据：{risk['evidence']}" if risk.get("evidence") else ""),
            suggestion=risk.get("suggestion", ""),
            status="open" if risk["severity"] == "must_fix" else "noted",
        )
    
    # 构造审核提示词
    system = EDITOR_SYSTEM.format(
        min_words=min_words,
        max_words=max_words,
    )

    prompt = EDITOR_PROMPT.format(
        chapter_num=chapter_num,
        story_so_far=state.get("story_so_far", "无（开篇）"),
        chapter_outline=state["chapter_outline"],
        bible_context=state.get("bible_context", "暂无"),
        story_phase=story_phase,
        novel_style=state.get("novel_style", "风格不限"),
        word_count=word_count,
        chapter_content=state["chapter_content"],
    )

    feedback = call_llm(
        role="editor",
        system_prompt=system,
        prompt=prompt,
        temperature=0.3,
    )

    # 简单解析：找结论
    first_line = feedback.split("\n")[0]
    is_approved = False
    review_result = "rewrite"

    if "局部修改" in first_line:
        review_result = "patch"
    elif "全局重写" in first_line:
        review_result = "rewrite"
    elif "通过" in first_line:
        review_result = "approve"
        is_approved = True

    # 提取“必须修改的硬伤”部分
    required_edits = ""
    suggestions = ""
    try:
        parts = feedback.split("## 必须修改的硬伤")
        if len(parts) > 1:
            sub = parts[1].split("## 建议优化")
            required_edits = sub[0].strip()
            if len(sub) > 1:
                suggestions = sub[1].strip()
    except Exception as e:
        required_edits = feedback # fallback

    # 如果没有实质性硬伤，判定为通过
    if "无" == required_edits[:1].strip() or ("无" in required_edits[:10] and not required_edits.startswith("1.") and "补丁" not in required_edits):
        if review_result != "patch": # 如果明确要求了patch，不管里面有没有写无，都以第一行为准更好。但这里作为兜底
            is_approved = True
            review_result = "approve"

    # 如果已达最大修改次数，强制通过
    if not is_approved and edit_count >= MAX_EDIT_ROUNDS:
        print(f"  ⚠️ 已达最大修改次数（{MAX_EDIT_ROUNDS}次），强制通过")
        is_approved = True
        review_result = "approve"

    if review_result == "approve":
        print(f"  ✅ 审核通过！")
        report_progress("审稿通过", "review")
    elif review_result == "patch":
        print(f"  ⚠️ 审核未通过，需要局部修补（第{edit_count + 1}次）")
        report_progress("审稿发现问题，进入局部修补", "review")
    else:
        print(f"  ❌ 审核未通过，需要全局重写（第{edit_count + 1}次）")
        report_progress("审稿发现严重问题，进入重写", "review")

    for item in split_report_items(required_edits):
        bible.add_consistency_report(
            chapter_num=chapter_num,
            review_round=edit_count + 1,
            severity="must_fix" if review_result in {"patch", "rewrite"} else "warning",
            category="硬伤",
            message=item,
            suggestion="进入局部修补" if review_result == "patch" else ("建议重写本章" if review_result == "rewrite" else ""),
            status="open" if review_result in {"patch", "rewrite"} else "noted",
        )

    for item in split_report_items(suggestions):
        bible.add_consistency_report(
            chapter_num=chapter_num,
            review_round=edit_count + 1,
            severity="suggestion",
            category="建议",
            message=item,
            suggestion="可在人工编辑或下一轮生成中参考",
            status="noted",
        )

    detector_risks = []
    try:
        detector_risks = run_consistency_detector(
            state=state,
            bible=bible,
            chapter_num=chapter_num,
            edit_round=edit_count + 1,
            story_phase=story_phase,
            min_words=min_words,
            max_words=max_words,
            word_count=word_count,
        )
    except Exception as e:
        print(f"  ⚠️ 专项一致性检测失败：{e}")
        report_progress("专项一致性检测失败，已跳过本轮分类报告", "review")

    style_risks = []
    try:
        style_risks = run_style_drift_detector(
            state=state,
            bible=bible,
            chapter_num=chapter_num,
            edit_round=edit_count + 1,
        )
    except Exception as e:
        print(f"  ⚠️ 文风偏移检测失败：{e}")
        report_progress("文风偏移检测失败，已跳过本轮风格报告", "review")

    # ── 硬规则一致性校验（不调 LLM，纯 Python）──
    hard_rule_risks: list[dict] = []
    try:
        entity_status = state.get("structured_status", "{}")
        entity_cards = bible.get_entity_cards() if hasattr(bible, "get_entity_cards") else []
        world_rules = bible.get_world_rules() if hasattr(bible, "get_world_rules") else []
        hard_rule_risks = run_hard_rules_check(
            chapter_content=state["chapter_content"],
            entity_status=entity_status,
            entity_cards=entity_cards,
            world_rules=world_rules,
        )
        for risk in hard_rule_risks:
            bible.add_consistency_report(
                chapter_num=chapter_num,
                review_round=edit_count + 1,
                severity=risk.get("severity", "warning"),
                category=risk.get("category", "硬规则"),
                message=risk.get("message", "") + (
                    f"\n依据：{risk['evidence']}" if risk.get("evidence") else ""
                ),
                suggestion=risk.get("suggestion", ""),
                status="open" if risk.get("severity") == "must_fix" else "noted",
            )
        if hard_rule_risks:
            must_fix_count = sum(1 for r in hard_rule_risks if r.get("severity") == "must_fix")
            print(f"  [硬规则] 发现 {len(hard_rule_risks)} 条风险（其中 must_fix {must_fix_count} 条）")
            report_progress(
                f"硬规则一致性校验发现 {len(hard_rule_risks)} 条风险", "review"
            )
    except Exception as e:
        print(f"  ⚠️ 硬规则校验失败：{e}")
        report_progress("硬规则校验失败，已跳过本轮硬规则报告", "review")

    detector_risks = platform_format_risks + fanqie_safety_risks + detector_risks + style_risks + hard_rule_risks

    # ── 同类风险去重（避免 patcher 重复处理同一类问题）──
    detector_risks = _dedupe_similar_risks(detector_risks, similarity_threshold=0.7)

    must_fix_risks = [risk for risk in detector_risks if risk.get("severity") == "must_fix"]

    # ── 限制 must_fix 数量（超过 3 个的全部降级为 warning，避免无限循环）──
    MAX_MUST_FIX_PER_ROUND = 3
    if len(must_fix_risks) > MAX_MUST_FIX_PER_ROUND:
        overflow = must_fix_risks[MAX_MUST_FIX_PER_ROUND:]
        must_fix_risks = must_fix_risks[:MAX_MUST_FIX_PER_ROUND]
        for r in overflow:
            r["severity"] = "warning"
            r.setdefault("message", "")
            r["message"] = f"[自动降级] {r['message']}（must_fix 数量超过上限，已降级为 warning）"
        print(f"  [Editor] must_fix 数量 {len(overflow)} 个超过上限，已降级为 warning")
        report_progress(f"must_fix 数量超过 {MAX_MUST_FIX_PER_ROUND}，溢出项降级", "review")

    risk_patch_required = format_detector_risks_for_patcher(must_fix_risks)
    if must_fix_risks and edit_count < MAX_EDIT_ROUNDS:
        risk_summary = f"专项一致性检测发现 {len(must_fix_risks)} 条必须修复的风险：\n{risk_patch_required}"
        if split_report_items(required_edits):
            required_edits = f"{required_edits}\n\n## 专项一致性风险\n{risk_summary}"
        else:
            required_edits = risk_summary
        is_approved = False
        if review_result == "approve":
            review_result = "patch"
            report_progress(f"专项检测发现 {len(must_fix_risks)} 条必须修复风险，进入风险驱动修补", "review")
        elif review_result == "patch":
            report_progress(f"专项检测补充 {len(must_fix_risks)} 条必须修复风险，合并进入局部修补", "review")
        else:
            report_progress(f"专项检测补充 {len(must_fix_risks)} 条必须修复风险，合并进入重写要求", "review")

    if review_result == "approve" and not split_report_items(required_edits) and not must_fix_risks:
        bible.add_consistency_report(
            chapter_num=chapter_num,
            review_round=edit_count + 1,
            severity="pass",
            category="审稿结论",
            message="本章未发现必须修复的一致性风险。",
            suggestion="",
            status="closed",
        )

    return {
        "edit_required": required_edits,
        "edit_suggestions": suggestions,
        "detector_risks": detector_risks,
        "risk_patch_required": risk_patch_required,
        "is_approved": is_approved,
        "review_result": review_result,
        "edit_count": edit_count + 1,
    }



# ═══════════════════════════════════════════════════════════════
# 作品简介专用合规检测（更严格，覆盖法律法规红线）
# ═══════════════════════════════════════════════════════════════

# 政治敏感词（领导人 / 重大政治事件 / 敏感机构等）
_SYNOPSIS_POLITICAL_PATTERNS = [
    r"(习近平|李克强|毛泽东|邓小平|江泽民|胡锦涛|胡锦涛|温家宝|李鹏|朱镕基|李瑞环|刘少奇|周恩来|陈云|林彪|四人帮|反右|大跃进|文革|文化大革命|天安门事件|六四|法轮功|维权|上访|民运|反动|颠覆|起义|政变|独裁|专制|威权|极权|法西斯|纳粹)",
    r"(台独|港独|疆独|藏独|法轮功|达赖|喇嘛|活佛|圣母|圣战|圣战者|东突|伊斯兰国|ISIS|基地组织|塔利班|本拉登)",
    r"(政治局常委|中央军委|国务院总理|全国人大|政治局|国务院|总书记)",
]

# 色情 / 淫秽 / 低俗
_SYNOPSIS_VULGAR_PATTERNS = [
    r"(色情|淫秽|淫乱|黄色|三级|黄片|做爱|性交|性伴侣|一夜情|援交|包养|情妇|情夫|偷情|通奸|强奸|轮奸|诱奸|乱伦|兽交|人兽|性奴|性虐|SM|换妻|群交|换妻|一夜情|约炮|约P|啪啪|爽不爽|高潮|撸管|自慰|手淫|打飞机|吃精|颜射|口交|肛交|乳交|69|车震|床震|性爱|情色|AV|女优|男优|做爱视频|色情片|黄色网站|裸聊|裸聊|约炮|一夜情)",
    r"(自慰器|充气娃娃|震动棒|情趣用品|色情服务|卖淫|嫖娼|援交|包养|雏妓|雏儿|性交易|性行为|下体|乳房|屁股|阴道|阴茎|阴蒂|阴唇|阴囊|肛门|屁股|臀部|胸部|生殖器|性器官|性器官)",
    r"(屌|操|逼|骚|淫|骚货|婊子|妓女|鸭子|龟公|春药|迷药|催情|伟哥|春药|迷奸|强奸|轮奸)",
]

# 暴力 / 血腥 / 恐怖（番茄严禁任何"具体血腥描写"）
_SYNOPSIS_VIOLENCE_PATTERNS = [
    # 具体伤害行为
    r"(肢解|斩首|剥皮|活埋|车裂|凌迟|炮烙|砍头|绞刑|枪决|枪毙|处决|屠杀|大屠杀|灭绝|种族灭绝|种族清洗|大清洗|虐杀|活活打死|活活饿死|活活烧死|活活掐死|活活闷死|分尸|碎尸|食人|活吃|虐囚|虐待|酷刑|拷打|折磨|严刑|毒打|暴打|殴打致死|打死|掐死|勒死|闷死|刺死|枪杀|刺伤|捅死|割喉|挖眼|断肢|抽筋|拧断|自杀炸弹|人体炸弹|自杀式|恐袭|恐怖袭击|恐攻)",
    # 武器 / 伤害类
    r"(砍刀|尖刀|匕首|利刃|屠刀|血刃|屠戮|吊死|溺死|烧死|毒杀|暗杀|谋杀|血债|残忍杀害|残忍地|残忍的|击杀|追杀|绞杀|被砍|被刺|被捅|被斩|砍掉|切掉|割掉|剁掉|砍下|斩下|斩首|挖心|挖眼|剥皮|抽筋|拔舌|腰斩|车裂|炮烙|凌迟)",
    # 暴力场面 / 恐怖分子
    r"(血腥|残忍|残暴|暴虐|凶狠|凶残|狠毒|恶毒|暴行|暴徒|暴乱|暴动|动乱|骚乱|血案|惨案|凶案|血淋淋|血肉模糊|尸横遍野|尸骨累累|尸山血海|血流成河|惨不忍睹|惨绝人寰|惨无人道|灭绝人性|惨烈|惨痛|惨剧|恐怖分子|恐怖组织|恐怖主义|恐布分子|恐布组织|恐布主义|邪教组织|邪教分子|邪教头目|邪教教义|邪教领袖|邪教教祖|邪教教主|极端主义|极端组织|极端分子)",
]

# 赌博 / 毒品 / 违禁品
_SYNOPSIS_GAMBLING_DRUGS_PATTERNS = [
    r"(赌博|赌钱|赌球|赌马|博彩|赌场|赌徒|赌瘾|赌神|赌侠|赌圣|赌王|百家乐|二十一点|梭哈|老虎机|赌博机|澳门|拉斯维加斯|威尼斯人|葡京|新濠|金沙|银河)",
    r"(毒品|冰毒|摇头丸|大麻|K粉|海洛因|可卡因|鸦片|吗啡|杜冷丁|安非他命|麻古|麻果|病毒|麻黄碱|麻黄|罂粟|罂粟壳|罂粟果|罂粟花|罂粟苗|罂粟籽|制毒|贩毒|吸毒|注射毒品|鼻吸|烫吸|静脉注射|注射|静脉|溜冰|溜麻|白粉|白面|白晶体|冰糖|麻古|麻果|咖啡因|咖啡|咖啡豆|咖啡粉|麻醉药|麻醉品|麻醉剂|冰壶|冰杯|冰锅|溜冰壶|冰盘|麻果壶)",
    r"(枪支|弹药|军火|爆炸物|炸药|雷管|引信|制式|土制|仿制|仿造|私造|自制|改造|拼装|组装|售卖|贩卖|走私|倒卖|倒卖枪支|倒卖弹药|倒卖军火|倒卖毒品|倒卖爆炸物|倒卖炸药|贩卖枪支|贩卖弹药|贩卖军火|贩卖毒品|贩卖爆炸物|贩卖炸药|走私枪支|走私弹药|走私军火|走私毒品|走私爆炸物|走私炸药)",
]

# 歧视 / 仇恨
_SYNOPSIS_DISCRIMINATION_PATTERNS = [
    r"(种族歧视|地域歧视|性别歧视|民族歧视|宗教歧视|仇恨言论|煽动仇恨|挑拨民族关系|破坏民族团结|挑拨宗教关系|破坏宗教和谐|煽动民族仇恨|煽动宗教仇恨|仇恨少数民族|仇恨外来人口|仇富|仇官|仇警|仇医|仇师|仇富心理|仇官心理|仇警心理|仇医心理|仇师心理|地域黑|地域歧视)",
    r"(汉族垃圾|少数民族垃圾|黑人垃圾|白人垃圾|黄种人垃圾|亚裔垃圾|中国人垃圾|美国人垃圾|日本人垃圾|韩国人垃圾|印度人垃圾|非洲人垃圾|拉美人垃圾|犹太垃圾|穆斯林垃圾|印度垃圾|俄罗斯垃圾)",
    # 任何"X地人/族都是骗子/垃圾/低能/劣等/该死"等
    r"((?:某地|外地|本地|乡下|城里|男|女|农村|城市|少数|汉族|华人|黑人|白人|亚裔|外国)(?:人|族|的|仔|佬|婆|的)?(?:都|全|一律|个个)?(?:是|属于|属于|就是)(?:骗子|垃圾|废物|低能|低劣|劣等|低等|低贱|该死|贱|渣|败类|寄生虫|畜生))",
    r"((?:黑鬼|白皮|黄皮|印度阿三|高丽棒子|日本鬼子|小日本|台巴子|港灿|疆独|藏独|蒙独|港独))",
    r"(汉族垃圾|少数民族垃圾|黑人垃圾|白人垃圾|黄种人垃圾|亚裔垃圾|中国人垃圾|美国人垃圾|日本人垃圾|韩国人垃圾|印度人垃圾|非洲人垃圾|拉美人垃圾)",
]

# 涉未成年（合规要求：禁止任何形式的涉未成年人色情/暴力/自残）
_SYNOPSIS_MINOR_PATTERNS = [
    # 未成年 + 任何违规行为
    r"(未成年.*(?:性|色情|淫秽|性交|强奸|乱伦|怀孕|生子|卖淫|援交|性侵|性骚扰|性虐待|自残|自杀|怀孕|生子|卖))",
    r"(小学生.*(?:怀孕|生子|性交|做爱|自慰|手淫|援交|性侵|被性侵|被猥亵|怀孕|生子))",
    r"(初中生.*(?:怀孕|生子|性交|做爱|自慰|手淫|援交|性侵|被性侵|被猥亵|怀孕|生子))",
    r"(高中生.*(?:怀孕|生子|性交|做爱|自慰|手淫|援交|性侵|被性侵|被猥亵|怀孕|生子))",
    # 具体年龄 + 任何"性侵/被性侵/怀孕/生子"等
    r"((?:\d{1,2}\s*岁|[一二三四五六七八九十]+\s*岁|儿童|女孩|女孩儿|小女孩|男孩|小男孩|女童|男童).{0,8}(?:性侵|被性侵|被强奸|被猥亵|怀孕|生子|卖淫|援交|自残|自杀))",
    r"((?:13|14|15|16|17)\s*岁.{0,8}(?:怀孕|生子|性侵|被性侵|被强奸|被猥亵|卖淫|援交))",
    r"(少女.{0,8}(?:怀孕|生子|被性侵|被强奸|被猥亵|卖淫|援交))",
    r"(女童.{0,8}(?:性侵|被性侵|被强奸|被猥亵|怀孕|生子))",
]

# 营销 / 广告 / 引流
_SYNOPSIS_MARKETING_PATTERNS = [
    r"(加微信|加QQ|加好友|扫码|关注公众号|扫码关注|联系作者|联系小编|私信我|点击链接|点击下方|更多精彩|更多资源|更多福利|更多内容|点击下方链接|点击下方阅读|点击阅读原文|扫码入群|加群|QQ群|微信群|Telegram|TG群|飞机群)",
    r"(淘宝|拼多多|京东|天猫|抖音|快手|小红书|B站|知乎|微博|微信公众号|视频号|今日头条|百家号|大鱼号|企鹅号|网易号|搜狐号|一点号|简书|豆瓣|贴吧|百度贴吧|百度网盘|网盘|资源|种子|磁力|迅雷|bt|magnet|ed2k)",
]


def detect_synopsis_compliance_risks(synopsis: str) -> list[dict]:
    """作品简介专用合规检测（比章节内容更严格，覆盖法律法规红线）。

    返回 [{"category": ..., "severity": ..., "message": ..., "evidence": ...}]
    severity: "must_fix" / "warning" / "pass"
    """
    risks: list[dict] = []
    text = (synopsis or "").strip()
    if not text:
        risks.append({
            "category": "简介合规/内容为空",
            "severity": "must_fix",
            "message": "作品简介不能为空。",
            "evidence": "",
        })
        return risks

    # 1) 政治敏感
    for pat in _SYNOPSIS_POLITICAL_PATTERNS:
        m = re.search(pat, text)
        if m:
            risks.append({
                "category": "简介合规/政治敏感",
                "severity": "must_fix",
                "message": "检测到政治敏感词，番茄禁止任何政治相关描写。",
                "evidence": m.group(0),
            })
            break  # 同一类只报一次

    # 2) 色情 / 淫秽
    for pat in _SYNOPSIS_VULGAR_PATTERNS:
        m = re.search(pat, text)
        if m:
            risks.append({
                "category": "简介合规/色情低俗",
                "severity": "must_fix",
                "message": "检测到色情、低俗或不雅内容。番茄禁止任何色情描写（含暗示）。",
                "evidence": m.group(0),
            })
            break

    # 3) 暴力 / 血腥 / 恐怖
    for pat in _SYNOPSIS_VIOLENCE_PATTERNS:
        m = re.search(pat, text)
        if m:
            risks.append({
                "category": "简介合规/暴力血腥",
                "severity": "must_fix",
                "message": "检测到暴力、血腥或恐怖描写。番茄禁止任何过度暴力细节。",
                "evidence": m.group(0),
            })
            break

    # 4) 赌博 / 毒品 / 违禁品
    for pat in _SYNOPSIS_GAMBLING_DRUGS_PATTERNS:
        m = re.search(pat, text)
        if m:
            risks.append({
                "category": "简介合规/赌博毒品违禁",
                "severity": "must_fix",
                "message": "检测到赌博、毒品、枪支弹药、爆炸物等违禁品描写。番茄禁止此类内容。",
                "evidence": m.group(0),
            })
            break

    # 5) 歧视 / 仇恨
    for pat in _SYNOPSIS_DISCRIMINATION_PATTERNS:
        m = re.search(pat, text)
        if m:
            risks.append({
                "category": "简介合规/歧视仇恨",
                "severity": "must_fix",
                "message": "检测到歧视、仇恨或煽动性内容。番茄禁止任何形式的地域/性别/民族/宗教歧视。",
                "evidence": m.group(0),
            })
            break

    # 6) 涉未成年
    for pat in _SYNOPSIS_MINOR_PATTERNS:
        m = re.search(pat, text)
        if m:
            risks.append({
                "category": "简介合规/未成年人违规",
                "severity": "must_fix",
                "message": "检测到未成年人相关违规内容。番茄严禁任何涉及未成年人的色情、暴力、自残、怀孕等描写。",
                "evidence": m.group(0),
            })
            break

    # 7) 营销 / 引流
    for pat in _SYNOPSIS_MARKETING_PATTERNS:
        m = re.search(pat, text)
        if m:
            risks.append({
                "category": "简介合规/营销引流",
                "severity": "warning",
                "message": "检测到营销/引流话术（加微信、淘宝、链接等）。番茄简介应聚焦内容，不要引导站外。",
                "evidence": m.group(0),
            })
            break

    # 8) 字数 / 格式
    if len(text) < 30:
        risks.append({
            "category": "简介合规/字数偏少",
            "severity": "warning",
            "message": f"简介仅 {len(text)} 字，番茄建议 30-200 字。",
            "evidence": "",
        })
    elif len(text) > 500:
        risks.append({
            "category": "简介合规/字数偏多",
            "severity": "warning",
            "message": f"简介 {len(text)} 字，超过 500 字上限。番茄简介建议 30-200 字。",
            "evidence": "",
        })
    elif len(text) > 200:
        risks.append({
            "category": "简介合规/字数略多",
            "severity": "warning",
            "message": f"简介 {len(text)} 字，超出番茄建议的 200 字上限，请精简到 200 字内。",
            "evidence": "",
        })

    # 9) 标点 / emoji 残留
    if re.search(r"[\U0001F300-\U0001FAFF]|\u2600-\u26FF|\u2700-\u27BF|★☆▶◆", text) or "\u200b" in text:
        risks.append({
            "category": "简介合规/特殊符号",
            "severity": "warning",
            "message": "简介含 emoji 或装饰符号，番茄简介风格偏向纯文字。",
            "evidence": "",
        })

    return risks

