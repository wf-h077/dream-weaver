"""跨章一致性分析

读取 batch_e2e.py 生成的章节，提取每章关键实体，对比一致性：
1. 角色名是否稳定（同一角色是否被叫成不同名字）
2. 物品/法宝是否一致
3. 境界/修为描述是否一致
4. 地点/势力是否一致
5. 状态面板是否累积正确

使用：
  python analyze_consistency.py output/batch_1787400000
  python analyze_consistency.py output/batch_1787400000 --chapter 5
"""
import sys
import os
import json
import re
import argparse
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, '.')


def safe_print(s):
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode("ascii"), flush=True)


# ═══════════════════════════════════════════════════════
# 启发式实体提取（不调 LLM，纯规则）
# ═══════════════════════════════════════════════════════

# 修真境界关键词
REALM_PATTERNS = [
    r"炼气[一二三四五六七八九十]?层", r"炼体[一二三四五六七八九十]?重",
    r"筑基[初期中期后期圆满]?", r"金丹[初期中期后期圆满]?",
    r"元婴[初期中期后期圆满]?", r"化神[初期中期后期圆满]?",
    r"凝元境", r"聚元境",
    r"斗[者师灵王皇宗圣帝]?", r"武[者师宗王帝圣]?",
    r"[ABCDEF]级", r"[一二三四五六七八九十]级",
    r"黄[阶品]", r"玄[阶品]", r"地[阶品]", r"天[阶品]",
]

# 物品关键字
ITEM_HINTS = [
    "剑", "刀", "枪", "戟", "锤", "镜", "钟", "塔", "鼎", "环",
    "丹", "药", "符", "玉", "佩", "镯", "珠", "瓶", "旗", "印",
    "功法", "秘籍", "心法", "武技", "神功",
    "灵根", "丹田", "经脉", "识海",
    "令牌", "圣旨", "玉简", "玉佩", "戒", "项链",
]

# 势力 / 地点关键字
FACTION_HINTS = ["家", "宗", "派", "门", "阁", "堂", "宫", "府", "国", "城", "镇", "山", "谷", "派"]


def extract_realms(text: str) -> set:
    """提取所有境界词"""
    realms = set()
    for pattern in REALM_PATTERNS:
        for m in re.finditer(pattern, text):
            realms.add(m.group(0))
    return realms


def extract_person_names(text: str) -> list:
    """启发式提取人名。

    改进点：
    1. 称谓提取更严格（要 2-3 字 + 必须以"X 长老"形式出现，"三位长老"不算）
    2. 排除明显是动作/副词的 2-3 字片段
    3. 用更长的"动作"模式（必须有完整的 X 转身/X 点头 等），避免"山猛地""晌没再"误报
    4. 加更多停用词
    """
    names = []

    # 停用词（不是人名）
    STOPWORDS = {
        "他", "她", "它", "你", "我", "我们", "他们", "她们", "自己", "众人",
        "有人", "无人", "一个", "那个", "某个", "这", "那", "这种", "那种",
        "三", "四", "五", "六", "七", "八", "九", "十", "几", "两",
        "山", "水", "火", "风", "林", "云", "天", "地", "海", "山猛地",
        "体内", "心脉", "剑柄", "心底", "心口", "手里", "怀中", "眼底", "眼中",
        "突然", "忽然", "瞬间", "刹那", "片刻", "晌", "早上", "中午", "晚上",
        "真是", "真的", "假的", "是", "不是", "也", "就", "都", "还", "才",
        "今日", "明日", "昨日", "现在", "刚才", "以后", "以后", "前面", "后面",
    }

    # 模式 1：完整称谓（"X 长老"、"X 族长"等），X 必须是 2-3 字且不在停用词
    TITLE_SUFFIXES = ["长老", "族长", "家主", "宗主", "掌门", "公子", "小姐", "先生",
                      "前辈", "姑娘", "小子", "丫鬟", "管事", "老祖", "族老",
                      "大公子", "二公子", "大小姐", "二小姐", "大叔", "二哥",
                      "师弟", "师妹", "师父", "师尊", "师兄", "师姐",
                      "二叔", "三叔", "大伯", "二爷", "老爷", "夫人", "太太"]
    for suffix in TITLE_SUFFIXES:
        for m in re.finditer(rf"([\u4e00-\u9fff]{{2,3}}?){re.escape(suffix)}", text):
            name = m.group(1)
            if name not in STOPWORDS and len(name) >= 2:
                # 过滤"三位长老"——前面有"几位"等
                prev_start = max(0, m.start() - 2)
                prev = text[prev_start:m.start()]
                if prev in ("三位", "四位", "几位", "数位", "这位", "那位", "一", "两", "几"):
                    continue
                # 过滤"林家三长老"——前面是"家"
                if prev.endswith("家"):
                    continue
                names.append(name)

    # 模式 2：对话标记（"X 道："、"X 喝道："、"X 笑道："）
    DIALOG_VERBS = ["道", "喝道", "说道", "笑道", "冷道", "沉声道", "低声道", "轻声道",
                    "怒道", "喝问", "质问道", "追问道", "沉吟道", "哑声道", "厉声道",
                    "缓缓道", "沉声", "淡淡道", "平静道", "开口", "抢道"]
    for verb in DIALOG_VERBS:
        for m in re.finditer(rf"([\u4e00-\u9fff]{{2,3}}?){re.escape(verb)}[：:\"\"\u201c\u201d]", text):
            name = m.group(1)
            if name not in STOPWORDS and len(name) >= 2:
                names.append(name)

    # 模式 3：动作主语（X 转身、X 笑 等）—— 必须前面是句子开头或逗号
    # 且 X 必须是常见的中文姓氏（避免"山猛地"等误报）
    COMMON_SURNAMES = set("林赵钱孙李周吴郑王冯陈楚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁")
    ACTION_VERBS = [
        # 基础动作
        "转身", "抬头", "点头", "摇头", "回头", "扭头", "侧身", "回首",
        "起身", "离身", "躬身", "欠身", "侧身", "挺身",
        "睁眼", "闭眼", "眨眼", "眯眼", "抬眼", "垂眼", "转眼", "转眼间",
        "皱眉", "笑", "大笑", "冷笑", "苦笑", "微笑", "嬉笑",
        "伸手", "握手", "松手", "放手", "摆手", "抬手", "垂手", "出手", "收手",
        "迈步", "跨步", "退步", "上前", "上前一步", "上前两步",
        "站", "坐", "蹲", "跪", "趴", "躺", "跳", "跃", "飞", "走", "跑",
        "看", "望", "瞧", "瞅", "盯", "瞄", "瞥", "环顾", "扫视",
        "听", "闻", "问", "答", "说", "讲", "喊", "叫", "吼", "嚷", "嚷道",
        "咬", "吻", "吞", "吐", "喝", "嚼",
        "叹", "叹气", "叹息",
        "怒", "恼", "惊", "喜", "悲", "忧", "惧", "怕", "恨", "爱", "怜",
        "怔", "愣", "呆", "傻", "愣神",
        "握拳", "挥手", "摇头", "点头", "拱手", "抱拳", "作揖", "行礼", "躬身",
        "低声道", "高声道", "朗声道", "轻声道", "沉声道",
        "咬牙", "怒喝", "吼道", "沉声", "厉声", "哑声", "笑声", "哭声",
    ]
    for verb in ACTION_VERBS:
        for m in re.finditer(rf"([\u4e00-\u9fff]{{2}}){re.escape(verb)}", text):
            name = m.group(1)
            if name in STOPWORDS:
                continue
            # 必须以常见姓氏开头
            if name[0] not in COMMON_SURNAMES:
                continue
            # 前面必须是标点或行首
            prev_start = max(0, m.start() - 1)
            if prev_start < m.start() and text[prev_start] not in ("。", "！", "？", "\n", "，", " ", "「", "\"", "\u201c", "（", "：", "；", "、"):
                if not (prev_start >= 1 and text[prev_start-1:prev_start+1] in ("他转", "她转", "他点", "她点", "他回", "她回")):
                    continue
            names.append(name)

    # 模式 4：常见姓氏 + 1-2 字（X + 名），不限动词
    # 例如"林尘""赵雨彤""林远山"等
    # 用 2 字（姓 + 单字名）或 3 字（姓 + 双字名）
    FAMILY_WORDS_EXCLUDE = {"林家", "赵家", "李家", "王家", "张家", "陈家", "杨家",
                            "黄家", "周家", "吴家", "郑家", "孙家", "钱家"}
    for m in re.finditer(rf"([\u4e00-\u9fff]{{1}})([\u4e00-\u9fff]{{1,2}}?)(?=[，。！？\s\u201c\u201d\u300c\u300d、：；])", text):
        surname = m.group(1)
        given = m.group(2)
        full = surname + given
        if full in STOPWORDS:
            continue
        if surname in COMMON_SURNAMES and len(full) >= 2 and len(full) <= 3:
            if full in FAMILY_WORDS_EXCLUDE:
                continue
            # 排除"林三"+"少爷" 这类（实际是"林三少爷"）
            after = text[m.end():m.end()+3]
            if any(after.startswith(s) for s in ["少爷", "公子", "小姐", "先生", "大人"]):
                continue
            names.append(full)

    return names


def extract_items(text: str) -> set:
    """启发式提取物品（X + 物品后缀）"""
    items = set()
    # 模式：X + 剑/刀/佩/镜/玉...
    for hint in ITEM_HINTS:
        for m in re.finditer(rf"[\u4e00-\u9fff]{{1,8}}{re.escape(hint)}", text):
            item = m.group(0)
            if len(item) <= 12:  # 避免过长误判
                items.add(item)
    return items


def extract_factions_locations(text: str) -> set:
    """提取势力/地点"""
    items = set()
    for hint in FACTION_HINTS:
        for m in re.finditer(rf"[\u4e00-\u9fff]{{1,6}}{re.escape(hint)}", text):
            item = m.group(0)
            if len(item) <= 8:
                items.add(item)
    return items


# ═══════════════════════════════════════════════════════
# 跨章一致性分析
# ═══════════════════════════════════════════════════════

def analyze_chapter(chapter_num: int, text: str) -> dict:
    """单章实体提取。"""
    return {
        "chapter": chapter_num,
        "word_count": len(text),
        "realms": extract_realms(text),
        "names": extract_person_names(text),
        "items": extract_items(text),
        "factions": extract_factions_locations(text),
    }


def cross_chapter_compare(per_chapter: list[dict]) -> dict:
    """跨章一致性对比。

    Returns:
        {
            "stable_names": 跨章稳定出现的角色名（高频）
            "drift_names": 在某章出现但其他章不出现的（角色名漂移）
            "stable_items": 稳定物品
            "drift_items": ...
            "realms_per_chapter": 每章境界
            "common_names": 所有章都有的（交集）
            "issues": 一致性问题列表
        }
    """
    name_counts = Counter()
    item_counts = Counter()
    realm_per_chapter = {}

    for ch in per_chapter:
        for n in ch["names"]:
            name_counts[n] += 1
        for i in ch["items"]:
            item_counts[i] += 1
        realm_per_chapter[ch["chapter"]] = ch["realms"]

    n_chapters = len(per_chapter)

    # 稳定角色名（出现 ≥ 60% 章）
    stable_names = {n: c for n, c in name_counts.items() if c >= max(2, n_chapters * 0.6)}
    # 漂移角色名（只出现 1 章）
    drift_names = {n: c for n, c in name_counts.items() if c == 1 and n not in stable_names}

    stable_items = {i: c for i, c in item_counts.items() if c >= max(2, n_chapters * 0.6)}
    drift_items = {i: c for i, c in item_counts.items() if c == 1 and i not in stable_items}

    # 所有章都有的角色（主角名应该在这里）
    common_names = {n for n, c in name_counts.items() if c == n_chapters}

    # 找潜在的角色名漂移（相似但不同名，如"林风" vs "林渊"）
    issues = []
    # 用常见姓氏过滤——避免"赵家大" vs "赵家二"误报
    COMMON_SURNAMES = set("林赵钱孙李周吴郑王冯陈楚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁")
    # 排除词（明显不是人名）
    EXCLUDE_FROM_DRIFT = {
        "林家", "赵家", "李家", "王家", "张家", "陈家", "杨家", "黄家", "周家",
        "宗门", "门派", "圣地", "洞府", "宫殿",
    }
    for n1 in name_counts:
        for n2 in name_counts:
            if n1 >= n2:
                continue
            # 跳过非人名
            if n1 in EXCLUDE_FROM_DRIFT or n2 in EXCLUDE_FROM_DRIFT:
                continue
            # 必须有共同姓氏
            if not (n1[0] in COMMON_SURNAMES and n2[0] in COMMON_SURNAMES):
                continue
            # 必须有差异（只差一个字）
            if len(set(n1) ^ set(n2)) != 1:
                continue
            # 共享字符 ≥ 1
            if set(n1) & set(n2):
                if (n1 in stable_names or name_counts[n1] >= 2) and (n2 in stable_names or name_counts[n2] >= 2):
                    issues.append(f"可能角色名漂移: '{n1}' vs '{n2}'（相似但出现多次）")

    return {
        "n_chapters": n_chapters,
        "stable_names": stable_names,
        "drift_names": drift_names,
        "common_names": common_names,
        "stable_items": stable_items,
        "drift_items": drift_items,
        "realms_per_chapter": realm_per_chapter,
        "issues": issues,
        "name_counts": dict(name_counts),
        "item_counts": dict(item_counts),
    }


# ═══════════════════════════════════════════════════════
# 状态面板对比（来自 state_NNN.json）
# ═══════════════════════════════════════════════════════

def compare_state_panels(states: list[dict]) -> dict:
    """跨章状态面板累积对比。

    Returns:
        {
            "panels_per_chapter": {ch: panel_dict},
            "growth": 从第 1 章到最后一章的成长轨迹
        }
    """
    panels = {}
    for s in states:
        ch = s["chapter"]
        panel_str = s.get("structured_status", "{}")
        try:
            panel = json.loads(panel_str) if panel_str else {}
        except Exception:
            panel = {}
        panels[ch] = panel

    return {
        "panels_per_chapter": panels,
    }


# ═══════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="跨章一致性分析")
    parser.add_argument("batch_dir", help="batch_e2e 生成的目录，如 output/batch_1787400000")
    parser.add_argument("--max-chars", type=int, default=2000, help="每章最多分析多少字（避免全章正则太慢）")
    args = parser.parse_args()

    batch_dir = Path(args.batch_dir)
    if not batch_dir.is_dir():
        safe_print(f"[ERROR] {batch_dir} 不是有效目录")
        return 1

    # 加载所有章节
    chapter_files = sorted(batch_dir.glob("chapter_*.txt"))
    if not chapter_files:
        safe_print(f"[ERROR] {batch_dir} 里没有 chapter_*.txt")
        return 1

    safe_print("=" * 60)
    safe_print(f"跨章一致性分析：{len(chapter_files)} 章")
    safe_print("=" * 60)

    per_chapter = []
    for cf in chapter_files:
        text = cf.read_text(encoding="utf-8")
        # 只取前 N 字符分析（避免整章正则太慢）
        sample = text[:args.max_chars]
        ch_num = int(re.search(r"chapter_(\d+)", cf.name).group(1))
        per_chapter.append(analyze_chapter(ch_num, sample))
        safe_print(f"  加载第 {ch_num} 章: {len(text)} 字（分析前 {args.max_chars}）")

    # 跨章对比
    safe_print("")
    safe_print("-" * 60)
    safe_print("跨章对比")
    safe_print("-" * 60)
    result = cross_chapter_compare(per_chapter)

    safe_print(f"\n  跨章稳定角色名（出现 ≥ 60% 章）：")
    for n, c in sorted(result["stable_names"].items(), key=lambda x: -x[1])[:10]:
        safe_print(f"    {n}: 出现 {c} 次")

    safe_print(f"\n  跨章稳定物品（出现 ≥ 60% 章）：")
    for i, c in sorted(result["stable_items"].items(), key=lambda x: -x[1])[:5]:
        safe_print(f"    {i}: 出现 {c} 次")

    safe_print(f"\n  所有章都有的角色（应该看到主角名）：")
    if result["common_names"]:
        for n in result["common_names"]:
            safe_print(f"    {n}")
    else:
        # 用 stable_names 中次数最多的（主角候选）
        if result["stable_names"]:
            top = max(result["stable_names"].items(), key=lambda x: x[1])
            safe_print(f"    （{top[0]} 出现 {top[1]} 次 - 可能是主角名）")
        else:
            safe_print("    （未检测到稳定的角色名）")

    safe_print(f"\n  每章境界：")
    for ch, realms in result["realms_per_chapter"].items():
        if realms:
            safe_print(f"    第 {ch} 章: {sorted(realms)}")
        else:
            safe_print(f"    第 {ch} 章: （无）")

    safe_print(f"\n  漂移角色名（只出现 1 次的）：")
    for n, c in sorted(result["drift_names"].items(), key=lambda x: -x[1])[:15]:
        safe_print(f"    {n}: {c}")

    if result["issues"]:
        safe_print(f"\n  ⚠️ 一致性问题：")
        for issue in result["issues"][:10]:
            safe_print(f"    - {issue}")

    # 状态面板对比
    state_files = sorted(batch_dir.glob("state_*.json"))
    if state_files:
        safe_print("")
        safe_print("-" * 60)
        safe_print("状态面板累积对比")
        safe_print("-" * 60)
        states = []
        for sf in state_files:
            s = json.loads(sf.read_text(encoding="utf-8"))
            states.append(s)
        panel_result = compare_state_panels(states)
        for ch, panel in panel_result["panels_per_chapter"].items():
            safe_print(f"\n  第 {ch} 章 状态面板:")
            if not panel:
                safe_print(f"    （空）")
                continue
            for char, status in list(panel.items())[:5]:
                if isinstance(status, dict):
                    safe_print(f"    {char}: {status}")
                else:
                    safe_print(f"    {char}: {status}")

    # 报告总结
    safe_print("")
    safe_print("=" * 60)
    safe_print("质量评估")
    safe_print("=" * 60)

    # 评估
    n_stable_chars = len(result["stable_names"])
    n_issues = len(result["issues"])

    if n_stable_chars >= 1 and n_issues == 0:
        safe_print("  ✅ 优秀：")
        main_chars = sorted(result["stable_names"].items(), key=lambda x: -x[1])[:3]
        safe_print(f"     - 主角名跨章稳定（{main_chars[0][0]} 出现 {main_chars[0][1]} 次）")
        safe_print(f"     - {n_stable_chars} 个稳定角色，{len(result['drift_names'])} 个漂移")
        safe_print(f"     - 0 个一致性问题")
    elif n_stable_chars >= 1:
        safe_print("  ⚠️  一般：")
        main_chars = sorted(result["stable_names"].items(), key=lambda x: -x[1])[:3]
        safe_print(f"     - 主角名稳定（{main_chars[0][0]} 出现 {main_chars[0][1]} 次）")
        safe_print(f"     - {n_issues} 个一致性问题需要关注")
    else:
        safe_print("  ❌ 待改进：")
        safe_print(f"     - 没有跨章稳定的角色名（可能在不同章用不同称呼）")
        safe_print(f"     - 检查 prompt 是否显式声明角色名")

    return 0


if __name__ == "__main__":
    sys.exit(main())
