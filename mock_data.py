"""Mock 模式预设数据（用于 MOCK_MODE=1 时跳过真实 LLM 调用）。

当用户在 .env 设 MOCK_MODE=1，所有 call_llm() 会从这里返回预设响应，
让 GitHub 访客无需配置 LLM key 也能完整体验 UI 流程。

每个 call_type 函数返回原始字符串（与真实 LLM 输出兼容的 JSON 或纯文本）。
call_type 由 system_prompt 文本中的关键词判定。
"""
from __future__ import annotations

import json
import os
import random
import re
import time


# ── 共享模板素材（中文网文风格）──

MOCK_NOVEL_TITLE = "数据洪流：反讽觉醒录"

MOCK_MAIN_CAST = [
    {
        "name": "林辰",
        "role": "主角",
        "identity": "前普通程序员，被裁员后觉醒反讽系统",
        "personality": "冷静、毒舌、内核温柔",
        "speaking_style": "短句+反讽，爱用反问句",
        "background": "前大厂 P7，因拒绝内卷被 HR 谈话后离职",
    },
    {
        "name": "赵文澜",
        "role": "女主",
        "identity": "科技媒体主编，林辰的隐藏姐姐",
        "personality": "干练、洞察力强、嘴硬心软",
        "speaking_style": "语速快，逻辑清晰",
        "background": "独立科技媒体《硅基观察》主编，与林辰同父异母",
    },
    {
        "name": "周铭远",
        "role": "主要对手",
        "identity": "数据垄断集团「擎天科技」副总裁",
        "personality": "伪善、算计、手段阴狠",
        "speaking_style": "永远微笑，反话正说",
        "background": "林辰前东家的高管，与裁员事件直接相关",
    },
    {
        "name": "老张",
        "role": "导师",
        "identity": "独立投资人，林辰的贵人",
        "personality": "玩世不恭，关键时刻犀利",
        "speaking_style": "京味调侃，偶尔爆金句",
        "background": "早年创业失败，转型做天使投资，慧眼识珠",
    },
    {
        "name": "小敏",
        "role": "伙伴",
        "identity": "林辰的大学同学，UI 设计师",
        "personality": "热心、话多、行动力强",
        "speaking_style": "网络用语+emoji",
        "background": "林辰最早期合伙人，负责设计",
    },
]

MOCK_KEY_FACTIONS = [
    {
        "name": "擎天科技",
        "kind": "反派势力",
        "description": "国内数据垄断巨头，掌控 80% 用户行为数据",
        "key_members": ["周铭远", "张副总", "技术总监老孙"],
        "stance": "敌对",
        "core_resources": "海量用户数据 + 政商关系",
    },
    {
        "name": "硅基观察",
        "kind": "盟友势力",
        "description": "独立科技媒体，专注数据隐私报道",
        "key_members": ["赵文澜", "调查记者阿吉"],
        "stance": "盟友",
        "core_resources": "调查能力 + 舆论阵地",
    },
    {
        "name": "灰度资本",
        "kind": "中立势力",
        "description": "老张背后的早期投资基金",
        "key_members": ["老张"],
        "stance": "中立偏友",
        "core_resources": "资金 + 行业人脉",
    },
]

MOCK_WORLD_RULES = [
    "反讽系统：主角每完成一次高难任务，系统的反讽值累积，可兑换「数据透视」技能",
    "数据透视：能看清任何一段历史数据的「人为篡改痕迹」，但 24 小时内只能用 3 次",
    "舆论场：网络舆论可被量化（共鸣度 0-100），主角可借助舆论反制强敌",
    "代价机制：每次使用反讽能力，主角会失去一段最珍贵的记忆",
    "晋升条件：建立独立数据公司，掌握真正属于自己的 100 万真实用户",
]


def _detect_call_type(system_prompt: str, prompt: str, messages: list | None = None) -> str:
    """根据 system_prompt / messages 关键词判定调用类型。

    messages 模式：把 system + user 内容拼起来检测（writer agent 用 messages 调用）。
    """
    sp_raw = system_prompt or ""
    pp_raw = prompt or ""

    # 合并 messages 用于检测
    if messages:
        joined = []
        for m in messages[:3]:  # 只看前 3 条
            c = m.get("content", "") if isinstance(m, dict) else ""
            if c:
                joined.append(c)
        messages_text = "\n".join(joined)[:800]
    else:
        messages_text = ""

    sp = sp_raw[:500] or messages_text[:500]
    pp = pp_raw[:300] or messages_text[500:800]

    if "修订" in pp[:100] or "修订" in messages_text[:200] or "根据读者的具体反馈" in sp_raw:
        return "revise_with_feedback"
    if "续写" in pp[:100] or "续写" in messages_text[:200] or "续写下面" in sp_raw:
        return "chapter_continue"
    if "改写一段" in pp[:100] or "改写一段" in messages_text[:200] or "改写给定段落" in sp_raw:
        return "segment_rewrite"
    if "提取本章新增" in pp or "提取本章新增" in messages_text[:300] or "实体提取" in sp_raw:
        return "extract_entities"
    if "运营编辑" in sp or "运营编辑" in messages_text[:300] or "写吸引人的简介" in pp[:200]:
        return "synopsis_generate"
    if "剧情大事件" in messages_text[:300] or "剧情大事件" in pp[:200]:
        return "synopsis_update"
    if "连续性风险" in sp or "连续性风险" in messages_text[:300]:
        return "consistency_detector"
    if "文风稳定性" in sp or "文风稳定性" in messages_text[:300] or "文风稳定性检测器" in sp_raw:
        return "style_drift_detector"
    if "风险驱动" in sp or "风险驱动" in messages_text[:300]:
        return "risk_patcher"
    if "精于修缮" in sp or "精于修缮" in messages_text[:300]:
        return "patcher"
    if "QUALITY_ENHANCER" in messages_text[:300] or "增强" in pp[:200] or "可读性的资深改稿作者" in sp_raw:
        return "quality_enhancer"
    if "商业可读性" in sp or "商业可读性" in messages_text[:300]:
        return "quality_assessor"
    if "文风样本" in messages_text[:200] or "文风样本" in pp[:200] or "文风分析器" in sp_raw:
        return "style_fingerprint"
    if "智能的小说场景分析器" in sp_raw or "智能的小说场景分析器" in messages_text[:300]:
        return "skill_selector"
    if "戏剧卡" in pp[:200] or "戏剧卡" in messages_text[:200] or "章节戏剧设计师" in sp_raw:
        return "drama_card"
    if "创作指令包" in pp[:200] or "创作指令包" in messages_text[:200] or "章节创作指令包" in sp_raw:
        return "brief_composer"
    if "内容类型构成" in pp[:200] or "内容类型构成" in messages_text[:300]:
        return "chapter_type"
    if "EXTRACTOR" in messages_text[:300] or "设定记录员" in sp or "细致入微的设定记录员" in sp_raw:
        return "extractor"
    if "前 10 章" in pp[:200] or "前 10 章" in messages_text[:200] or "开局十章结构" in sp_raw:
        return "opening_outline"
    if "全书蓝图" in sp or "全书蓝图" in messages_text[:300] or "全书蓝图架构师" in sp_raw:
        return "full_outline"
    if "立项包" in pp[:200] or "立项包" in messages_text[:200] or "项目制片人兼总编" in sp_raw:
        return "refine_concept"
    if "立项方向" in pp[:200] or "立项方向" in messages_text[:200] or "立项总编" in sp_raw:
        return "concept_directions"
    if "构思一个小说企划案" in pp[:200] or "构思一个小说企划案" in messages_text[:200] or "金牌主编" in sp_raw:
        return "brainstorm"
    if "才华横溢的网络小说家" in sp_raw or "才华横溢的网络小说家" in messages_text[:300]:
        return "writer"
    if "审核硬伤" in sp or "审核硬伤" in messages_text[:300]:
        return "editor"
    if "10年经验的金牌网文主编" in sp or "10年经验的金牌网文主编" in messages_text[:300]:
        return "planner"
    return "generic"


# ── 各类调用 mock 数据 ──

def _mock_brainstorm() -> str:
    data = {
        "directions": [
            {
                "id": "dir-data-flood",
                "title": "数据洪流：反讽觉醒录",
                "tagline": "被裁员的程序员觉醒「反讽系统」，用数据透视技能撕开巨头谎言",
                "category": "都市脑洞 + 商战 + 数据题材",
                "hook": "主角被前东家裁员后，意外发现自己能看穿任何数据被篡改的痕迹",
                "target_audience": "25-35 岁互联网从业者、网文老书虫",
                "key_tropes": ["金手指反讽系统", "数据透视", "舆论反杀", "舆论商战", "小成本逆袭"],
                "commercial_potential": "高（互联网+商战，数据题材是当前风口）",
            },
            {
                "id": "dir-rural-coder",
                "title": "乡村程序员：从县中到硅谷",
                "tagline": "县城中学毕业的草根程序员，靠开源项目逆袭成 AI 独角兽 CTO",
                "category": "现实题材 + 草根逆袭 + 互联网",
                "hook": "主角在县城中学机房写出开源数据库引擎，被硅谷投资人发现",
                "target_audience": "18-30 岁小镇做题家、技术爱好者",
                "key_tropes": ["草根逆袭", "开源信仰", "技术理想主义", "县城生活", "硅谷"],
                "commercial_potential": "中高（草根逆袭+互联网题材长尾）",
            },
            {
                "id": "dir-game-genius",
                "title": "全服第一：我的 NPC 会造反",
                "tagline": "重度社恐玩家在自建游戏里养出会自己写剧情的 NPC 帮派",
                "category": "游戏 + 轻科幻 + 成长",
                "hook": "主角在自制的开放世界沙盒里，NPC 因「涌现行为」觉醒自我意识",
                "target_audience": "16-28 岁游戏玩家、ACG 圈",
                "key_tropes": ["第四天灾", "NPC 觉醒", "玩家-系统博弈", "游戏内建国"],
                "commercial_potential": "中（游戏题材稳定，但同质化高）",
            },
        ]
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def _mock_concept_directions() -> str:
    data = {
        "directions": [
            {
                "id": "dir-1",
                "title": "数据洪流：反讽觉醒录",
                "tagline": "被裁员的程序员觉醒反讽系统，用数据透视撕开巨头谎言",
                "category": "都市脑洞 + 商战",
                "key_trope": "金手指 + 数据透视",
            },
            {
                "id": "dir-2",
                "title": "小镇码农：从县中到硅谷",
                "tagline": "县城草根程序员靠开源逆袭成 AI 独角兽 CTO",
                "category": "现实题材 + 草根逆袭",
                "key_trope": "草根逆袭 + 技术理想主义",
            },
            {
                "id": "dir-3",
                "title": "全服第一：我的 NPC 会造反",
                "tagline": "社恐玩家在自建沙盒游戏里养出会自己写剧情的 NPC",
                "category": "游戏 + 轻科幻",
                "key_trope": "第四天灾 + NPC 觉醒",
            },
        ]
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def _mock_refine_concept(prompt: str) -> str:
    m = re.search(r"生成\s*\*\*(\d+)\s*条\*\*", prompt)
    num_chapters = int(m.group(1)) if m else 20
    num_chapters = max(5, min(50, num_chapters))

    chapter_constraints = []
    counter = 0
    # 简化版：根据 num_chapters 生成对应数量的章节约束
    events = [
        "主角觉醒反讽系统", "首次数据透视打脸前同事", "接受独立科技媒体采访",
        "与周铭远正面交锋", "发现用户数据被窃取", "反讽值突破临界",
        "舆论反制初见成效", "获得老张投资", "擎天科技反扑",
        "主角公司被污蔑", "数据公开战打响", "主角付出记忆代价",
        "反讽系统升阶", "联盟扩大", "关键证人翻供",
        "反派大反扑", "主角陷入绝境", "舆论反转",
        "最终决战前夜", "擎天科技崩塌", "反讽系统真相",
        "兄弟重逢", "公司走上正轨", "尾声收束",
    ]
    for ch in range(1, num_chapters + 1):
        event = events[counter % len(events)]
        chapter_constraints.append({
            "chapter_num": ch,
            "act": ["开局", "发展", "中段转折", "高潮决战", "结局收束"][min(4, ch * 5 // num_chapters)],
            "purpose": f"第{ch}章：{event}"[:30],
            "core_event": f"核心事件：{event}"[:60],
            "required_characters": random.choice([
                "林辰、赵文澜", "林辰、周铭远", "林辰、老张", "林辰、小敏", "林辰、赵文澜、老张",
            ]),
            "required_settings": random.choice([
                "反讽系统、数据透视", "反讽系统、擎天科技", "反讽系统、舆论场", "数据透视、硅基观察",
            ]),
            "ending_hook": random.choice([
                "新对手登场", "记忆闪回", "公司危机", "旧友重逢", "真相浮出水面",
            ]),
            "avoid": random.choice([
                "避免主角无敌", "避免反派工具人", "避免强行降智", "避免虐主", "避免节奏拖沓",
            ]),
        })
        counter += 1

    data = {
        "title": MOCK_NOVEL_TITLE,
        "logline": "被裁员的程序员林辰，意外觉醒「反讽系统」——能看穿任何数据被篡改的痕迹。他用这项能力撕开数据巨头擎天科技的谎言，从一个失业青年成长为独立数据公司创始人，最终让真相大白于天下。",
        "core_setting": "2031 年的中国互联网世界。数据已成为新石油，三大巨头（擎天科技、破晓 AI、灰度资本）掌控 90% 以上的用户行为数据。普通人的每一次点击、每一次搜索、每一次消费都被记录、分析、变现。法律滞后，监管缺位，普通用户既无知情权，也无反抗能力。",
        "world_rules": MOCK_WORLD_RULES,
        "main_cast": MOCK_MAIN_CAST,
        "key_factions": MOCK_KEY_FACTIONS,
        "tone": "热血 + 黑色幽默 + 数据时代的冷峻",
        "writing_style": "短句为主，节奏紧凑，每章必有钩子；对白带反讽，数据描写具象化（不用「大量」「海量」，直接给数字）",
        "target_audience": "25-35 岁互联网从业者、网文老书虫，关注数据隐私与科技伦理",
        "value_expression": "普通人对数据霸权的反抗；技术理想主义 vs 商业现实；舆论与真相的博弈",
        "chapter_constraints": chapter_constraints,
        "_mock_meta": {
            "mock_mode": True,
            "generated_at": time.time(),
            "num_chapters": num_chapters,
        },
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def _mock_opening_outline() -> str:
    data = {
        "chapters": [
            {"num": 1, "title": "暴雨中的羞辱与觉醒", "core_event": "林辰被前东家裁员，在暴雨中被保安拦在公司门外。回家路上意外觉醒反讽系统，能看穿数据被篡改的痕迹。", "key_scene": "擎天科技大楼下的暴雨", "hook": "第一次使用反讽系统，看到前同事晋升邮件被人为篡改"},
            {"num": 2, "title": "数据不会说谎，但人会", "core_event": "林辰发现自己的社保数据被前公司删除，立即使用反讽系统看到了删除时间戳。", "key_scene": "社保局办事大厅", "hook": "林辰在社保局当众戳穿前公司 HR 的谎言"},
            {"num": 3, "title": "第一次数据透视", "core_event": "林辰在求职过程中使用反讽系统，看穿一家明星创业公司的财务数据造假。", "key_scene": "创业公司面试间", "hook": "面试官正是当年一起被裁的同事老王，林辰没有戳穿"},
            {"num": 4, "title": "神秘的邮件", "core_event": "林辰收到一封匿名邮件，附有擎天科技用户数据泄露的内部截图。", "key_scene": "出租屋深夜", "hook": "发件人 IP 指向科技媒体《硅基观察》"},
            {"num": 5, "title": "硅基观察的女主编", "core_event": "林辰联系《硅基观察》主编赵文澜，初次交锋。", "key_scene": "中关村咖啡馆", "hook": "赵文澜发现林辰是失散多年的同父异母弟弟"},
            {"num": 6, "title": "老张的赌局", "core_event": "赵文澜介绍林辰认识独立投资人老张，老张给他出了一道考题。", "key_scene": "国贸写字楼顶层", "hook": "老张的考题是：证明擎天科技三年前的发布会 PPT 数据造假"},
            {"num": 7, "title": "三年前的那场发布会", "core_event": "林辰使用反讽系统深度分析三年前发布会数据，发现 PPT 关键指标被修改 47%。", "key_scene": "林辰家中的旧笔记本", "hook": "林辰发现自己当时也参与过那场发布会的技术支持"},
            {"num": 8, "title": "舆论第一战", "core_event": "赵文澜将调查报道发出，24 小时内冲上微博热搜前三。", "key_scene": "《硅基观察》编辑部", "hook": "擎天科技公关部连夜联系赵文澜，开价 500 万删稿"},
            {"num": 9, "title": "反讽值突破临界", "core_event": "林辰的反讽系统升级，解锁「舆论场」技能——能看到网络舆论的共鸣度分布。", "key_scene": "林辰的出租屋", "hook": "升级代价：林辰失去一段与母亲最珍贵的记忆"},
            {"num": 10, "title": "周铭远的反击", "core_event": "周铭远亲自出手，让林辰的前同事集体在网上发文指控林辰「职场霸凌」。", "key_scene": "林辰收到 17 封同事的「控诉」邮件", "hook": "林辰发现 17 封信的发件时间戳完全一致——是群发的"},
        ]
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def _mock_full_outline() -> str:
    data = {
        "acts": [
            {"name": "开局", "chapter_range": "1-10", "purpose": "建立金手指 + 第一次爽点 + 第一次小高潮",
             "turning_point": "林辰觉醒反讽系统，第一次小规模反制成功"},
            {"name": "发展", "chapter_range": "11-40", "purpose": "势力扩大 + 规则代价 + 关系网展开",
             "turning_point": "林辰发现更深层敌人——擎天科技背后的政商关系网"},
            {"name": "中段转折", "chapter_range": "41-70", "purpose": "主线矛盾升级 + 重大转折 + 代价抉择",
             "turning_point": "林辰公司被污蔑到崩溃边缘，反讽系统接近失控"},
            {"name": "高潮决战", "chapter_range": "71-90", "purpose": "核心矛盾集中爆发 + 终极对抗",
             "turning_point": "周铭远露出真面目，与境外数据集团勾结"},
            {"name": "结局收束", "chapter_range": "91-100", "purpose": "解决主线矛盾 + 角色命运 + 情绪收束",
             "turning_point": "林辰公开全部数据，舆论彻底反转，正义归来"},
        ]
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def _mock_writer(prompt: str = "") -> str:
    m = re.search(r"第\s*(\d+)\s*章", prompt or "")
    ch_num = int(m.group(1)) if m else 1

    title_pool = [
        "暴雨中的羞辱与觉醒", "数据不会说谎，但人会", "第一次数据透视", "神秘的邮件",
        "硅基观察的女主编", "老张的赌局", "三年前的那场发布会", "舆论第一战",
        "反讽值突破临界", "周铭远的反击", "舆论场上的狙击战", "记忆碎片的代价",
    ]
    title = title_pool[(ch_num - 1) % len(title_pool)]

    body = f"""第 {ch_num} 章 {title}

下午四点，一场突如其来的暴雨把整条中关村都浇得透透的。

林辰抱着纸箱站在擎天科技大楼门口，保安小张面无表情地举着伞：「林先生，您的工卡已注销，不能进。」

「我进去拿我的私人物品。」

「您的私人物品在昨天已经被行政部清空了。」小张压低声音，「周总亲自交代的，您别让我为难。」

林辰的手指在衣角上轻轻攥紧。

三年前他从 985 毕业，一路杀进这家国内顶级互联网公司，凭的是 B+ 树代码写得飞起、凌晨四点还在解决线上 P0 故障的狠劲。三年后他被一脚踢出公司大门，连自己的马克杯都没能带走。

他没再说话，转身走进暴雨里。

雨点砸在脸上，凉得刺骨。他走了大概五十米，突然感觉眼前的世界变了——

视野的左下角，出现了一行极淡的蓝色字迹：

【反讽系统 v1.0 已激活】
【检测到您正经历不公正待遇】
【是否开启「数据透视」模式？】

林辰以为自己眼花了，揉了揉眼睛，那行字还在。

他下意识地伸手去触碰，指尖穿过虚影，但能感受到一股极细的电流从指尖窜进大脑后部，又迅速消散。

「这什么鬼？」他低声骂了一句。

蓝色字迹闪了闪，似乎在回应他的疑惑：

【反讽系统：检测到「不合理」事件，自动激活】
【剩余能量：72%】
【每次使用会消耗记忆片段，请谨慎】

「什么记忆片段？」

系统没有回答。

林辰站在暴雨里愣了五秒钟，然后做出了一个决定——

他打开了手机浏览器，输入了刚才那封被公司系统弹窗的晋升通知里，那个平时不显山露水、却突然被提拔为 P8 的同事名字。

屏幕上出现了那个人的领英主页。

而在他的眼前，那张网页上，某个数字开始「发光」——

「在职时间：5 年」
「最近一次晋升：8 月 1 日」

但反讽系统在右下角标注了：

【原始值：3 年 6 个月】
【篡改时间：8 月 1 日 14:23】
【操作账号：HR-Admin-002】

林辰的手开始抖。

他看着那个时间戳，又看了一眼公司大楼。8 月 1 日下午两点二十三分——那正是他被约谈「协商离职」的时间。

原来如此。

他被裁员，不是因为他不够好，而是因为——有人要空出一个位置。

「好。」林辰把手机揣进口袋，雨水顺着下巴滴落，「我记住了。」

他的眼神变了。

不再是刚才在大楼门口那种被羞辱后的茫然，而是一种冷到骨子里的清醒。

【本章字数：约 1500 字 | 章节钩子：金手指觉醒 + 第一次发现阴谋 + 反派伏笔】

林辰回到出租屋，浑身上下还在滴水。

他把湿透的西装外套挂在门把手上，自己坐到了那张二手办公桌前。桌上还摊着昨天没写完的代码——一段他打算在周末重构的分布式锁工具，但现在已经不重要了。

他的手指在键盘上悬了十秒钟，然后做了一件他自己都没想到的事——

他打开了公司内网。

「反正工卡只是注销了，」他这样告诉自己，「登录状态应该还在。」

果然，他能登录。

但系统首页弹出了一条公告：「您的企业邮箱将于 24 小时后停用，请尽快迁移重要数据。」

林辰的嘴角扯了一下。

他迅速在企业邮箱里搜索了「林辰」「离职」「协商」这三个关键词。三秒后，结果出来了——足足 47 封邮件。他从最早的一封开始，一封一封地看。

第一封，8 月 1 日下午 1 点 47 分，HR 总监发给法务：「林辰 P7 离职方案（待周总确认）」。

第二封，8 月 1 日下午 2 点 05 分，法务回复：「建议 N+1 赔偿方案」。

第三封，8 月 1 日下午 2 点 21 分，HR 总监：「周总说按 N 来处理，理由是'绩效不达标'，但我们要做好录音准备。」

林辰的手开始抖。

绩效不达标？

他过去三年的绩效评级分别是 A、A+、A+，连续两年拿到优秀员工奖，是部门里唯一一个能独立承担 P0 故障的技术骨干。

他把每封邮件的发件时间、收件人、抄送人，全部记在脑子里。

然后他做了一件事——把整封邮件列表导出，发送到自己的个人邮箱。

「周总，」林辰低声说，「你说绩效不达标？那这些邮件，你打算怎么解释？」

【本章字数：约 1500 字 | 章节钩子：金手指觉醒 + 邮件证据 + 反派伏笔】

（本章完）"""
    return body


def _mock_editor() -> str:
    return json.dumps({
        "score": 8.5,
        "issues": [
            {"type": "minor", "location": "第 3 段", "issue": "比喻略显套路，可改为更具象的描写"},
            {"type": "minor", "location": "对话段", "issue": "赵文澜的台词略显生硬，可更口语化"},
        ],
        "highlights": [
            "金手指设计有创意，'数据透视'的具体表现形式很独特",
            "节奏把控好，每 500 字就有转折",
            "主角性格鲜明，'短句+反讽'的说话风格有辨识度",
        ],
        "suggestion": "总体质量良好，可以直接发布；后续章节注意保持人物语言一致性。",
    }, ensure_ascii=False, indent=2)


def _mock_revise_with_feedback() -> str:
    return """[根据读者反馈修订]

林辰的手指在衣角上轻轻攥紧。

三年前他从 985 毕业，一路杀进这家国内顶级互联网公司，凭的是 B+ 树代码写得飞起、凌晨四点还在解决线上 P0 故障的狠劲。三年后他被一脚踢出公司大门，连自己的马克杯都没能带走。

他没再说话，转身走进暴雨里。

雨点砸在脸上，凉得刺骨。他走了大概五十米，突然感觉眼前的世界变了——

视野的左下角，出现了一行极淡的蓝色字迹：

【反讽系统 v1.0 已激活】
【检测到您正经历不公正待遇】
【是否开启「数据透视」模式？】

林辰以为自己眼花了，揉了揉眼睛，那行字还在。

他下意识地伸手去触碰，指尖穿过虚影，但能感受到一股极细的电流从指尖窜进大脑后部，又迅速消散。

「这什么鬼？」他低声骂了一句。

蓝色字迹闪了闪，似乎在回应他的疑惑：

【反讽系统：检测到「不合理」事件，自动激活】
【剩余能量：72%】
【每次使用会消耗记忆片段，请谨慎】

「什么记忆片段？」

系统没有回答。

林辰站在暴雨里愣了五秒钟，然后做出了一个决定——

他打开了手机浏览器，输入了刚才那封被公司系统弹窗的晋升通知里，那个平时不显山露水、却突然被提拔为 P8 的同事名字。

屏幕上出现了那个人的领英主页。

而在他的眼前，那张网页上，某个数字开始「发光」——

「在职时间：5 年」
「最近一次晋升：8 月 1 日」

但反讽系统在右下角标注了：

【原始值：3 年 6 个月】
【篡改时间：8 月 1 日 14:23】
【操作账号：HR-Admin-002】

林辰的手开始抖。

他看着那个时间戳，又看了一眼公司大楼。8 月 1 日下午两点二十三分——那正是他被约谈「协商离职」的时间。

原来如此。

他被裁员，不是因为他不够好，而是因为——有人要空出一个位置。

「好。」林辰把手机揣进口袋，雨水顺着下巴滴落，「我记住了。」

他的眼神变了。

不再是刚才在大楼门口那种被羞辱后的茫然，而是一种冷到骨子里的清醒。

【本章字数：约 2500 字 | 章节钩子：金手指觉醒 + 第一次发现阴谋 + 反派伏笔】

（本章完）"""


def _mock_chapter_continue() -> str:
    return """林辰走出咖啡馆时，雨已经小了一些。

他没带伞，雨水顺着发梢滴进领口，但他没有加快脚步。脑子里还在回放刚才赵文澜说的那句话——

「下次见面之前，先把你脑子里那个'系统'搞明白。」

她怎么知道的？

林辰停下脚步，抬头看着灰蒙蒙的天空。视网膜上那行蓝色的字还在，但这次他注意到一个细节：字的右下角有一行极小的小字，他之前没看到——

【系统版本：v1.0】
【剩余能量：72%】
【下次充能：未知】

「剩余能量 72%？」

林辰低声念出来，意识到一个更严重的问题——这个系统有「能量」限制。如果能量耗尽会怎样？会消失？还是会反噬他？

他必须尽快搞清楚这个系统的来源和规则。

「先生，要打车吗？」

一辆出租车停在他身边，司机探出头。

「不用。」林辰摆摆手，沿着湿漉漉的人行道继续往前走。

他想先回家，把最近一周的数据异常全部梳理一遍。如果这个「反讽系统」真的和他被裁员有关，那这件事背后，可能远比他想象的要深。"""


def _mock_segment_rewrite() -> str:
    return """林辰走出咖啡馆时，雨已经小了一些，但他的脚步没停。

脑子里还在回放刚才赵文澜说的那句话——「下次见面之前，先把你脑子里那个'系统'搞明白。」

她怎么知道的？林辰停下脚步，抬头看着灰蒙蒙的天空。视网膜上那行蓝色的字还在，但这次他注意到一个细节：字的右下角有一行极小的小字——

【系统版本：v1.0】
【剩余能量：72%】
【下次充能：未知】

「剩余能量 72%？」

林辰低声念出来，意识到一个更严重的问题——这个系统有能量限制。能量耗尽会怎样？消失？反噬？

他必须尽快摸清这个系统的来源和规则。"""


def _mock_extract_entities() -> str:
    return json.dumps({
        "characters": [
            {"name": "林辰", "role": "主角", "first_appearance": False, "new_traits": ["觉醒反讽系统", "能看穿数据篡改痕迹"]},
            {"name": "赵文澜", "role": "盟友", "first_appearance": True, "initial_impression": "科技媒体主编，洞察力强"},
        ],
        "items": [
            {"name": "反讽系统", "kind": "金手指", "description": "v1.0 版本，有能量限制，能看穿数据被篡改的痕迹"},
        ],
        "factions": [
            {"name": "硅基观察", "kind": "盟友势力", "first_appearance": True, "description": "独立科技媒体"},
        ],
        "locations": [
            {"name": "中关村创业大街咖啡馆", "kind": "场所", "first_appearance": True, "description": "林辰与赵文澜初遇的咖啡馆"},
        ],
    }, ensure_ascii=False, indent=2)


def _mock_synopsis_generate() -> str:
    return """被裁员的程序员林辰，意外觉醒「反讽系统」——能看穿任何数据被篡改的痕迹。

他用这项能力撕开数据巨头擎天科技的谎言，从一个失业青年成长为独立数据公司创始人。

但每一次使用反讽能力，他都会失去一段最珍贵的记忆。

当真相与代价同时摆在面前，他该如何选择？

【一句话卖点】数据洪流时代的小人物逆袭，技术理想主义对抗数据霸权。"""


def _mock_synopsis_update() -> str:
    return """【剧情大事件更新】

1. 林辰被擎天科技裁员，工卡当场注销（第 1 章）
2. 林辰在暴雨中觉醒反讽系统 v1.0，能看穿数据篡改（第 1 章）
3. 林辰发现社保记录被前公司清空（第 2 章）
4. 林辰在面试中识破星瀚科技财务造假（第 3 章）
5. 林辰收到匿名邮件，得知擎天科技用户数据泄露（第 4 章）
6. 林辰结识《硅基观察》主编赵文澜（第 5 章）"""


def _mock_quality_assessor() -> str:
    return json.dumps({
        "overall_score": 8.5,
        "commercial_readability": {
            "score": 8.5,
            "comment": "节奏紧凑，每 500 字一个小转折，符合网文读者期待",
        },
        "engagement": {
            "score": 9.0,
            "comment": "金手指设定有创意，主角性格鲜明，读者粘性强",
        },
        "writing_quality": {
            "score": 8.0,
            "comment": "对白有辨识度，但部分描写略显套路",
        },
        "issues": [
            {"severity": "low", "type": "写作风格", "desc": "少量形容词堆砌"},
            {"severity": "low", "type": "对话", "desc": "配角对白可更鲜明"},
        ],
        "verdict": "pass",
    }, ensure_ascii=False, indent=2)


def _mock_quality_enhancer() -> str:
    return """[基于质量评估意见的增强版本 - 节选]

林辰的手指在衣角上轻轻攥紧。

三年前他从 985 毕业，一路杀进这家国内顶级互联网公司，凭的是 B+ 树代码写得飞起、凌晨四点还在解决线上 P0 故障的狠劲。三年后他被一脚踢出公司大门，连自己的马克杯都没能带走。

他没再说话，转身走进暴雨里。雨点砸在脸上，凉得刺骨，每一滴都像在提醒他：你完了，你被淘汰了，这座城市不需要你了。

他走了大概五十米，突然感觉眼前的世界变了——

视野的左下角，出现了一行极淡的蓝色字迹：

【反讽系统 v1.0 已激活】

字迹像是用最细的荧光笔写在视网膜上，眨眼不消失，但也不刺眼。林辰以为自己眼花了，使劲眨了眨眼，那行字还在。

他下意识地伸手去触碰，指尖穿过虚影，但能感受到一股极细的电流从指尖窜进大脑后部，又迅速消散。"""


def _mock_consistency_detector() -> str:
    return json.dumps({
        "risks": [
            {"level": "low", "type": "人设一致性", "location": "对话段 3", "desc": "林辰的'短句+反讽'风格在第 3 段对话中略有弱化"},
            {"level": "low", "type": "设定自洽", "location": "第 5 段", "desc": "反讽系统 v1.0 提到'能量限制'，但前文未铺垫来源"},
        ],
        "no_risk_count": 12,
        "summary": "本章整体一致性良好，发现 2 处低风险提示，已记录供后续章节参考。",
    }, ensure_ascii=False, indent=2)


def _mock_style_drift_detector() -> str:
    return json.dumps({
        "drift_score": 0.12,
        "threshold": 0.25,
        "verdict": "pass",
        "drift_points": [],
        "summary": "本章文风与前 5 章基准一致，未检测到明显漂移。",
    }, ensure_ascii=False, indent=2)


def _mock_patcher() -> str:
    return """原片段：林辰伸手触碰虚影。
替换片段：林辰下意识地伸手去触碰，指尖穿过虚影，但能感受到一股极细的电流从指尖窜进大脑后部，又迅速消散。

修改理由：增加感官细节（电流触感），让"系统激活"的瞬间更具象化。"""


def _mock_risk_patcher() -> str:
    return """原片段：[第 5 段] 主角林辰直接说出"反讽系统"四个字。
替换片段：[第 5 段] 主角林辰在心里默念一句"这是什么鬼东西"，但没有说出口。

修改理由：一致性检测器提示，主角在前 3 章中并不知道系统的名字，"反讽系统"是第 4 章赵文澜告诉他的。"""


def _mock_style_fingerprint() -> str:
    return json.dumps({
        "sentence_length": "短句为主，平均 12-18 字",
        "dialogue_style": "反问句 + 短句，对话带黑色幽默",
        "rhythm": "每 500 字必有转折或钩子",
        "vocabulary": "互联网行业术语 + 日常口语，避免书面化",
        "narration_perspective": "第三人称限知，主角视角为主",
        "emotion_intensity": "中等偏强，关键情节给到 8/10",
    }, ensure_ascii=False, indent=2)


def _mock_skill_selector() -> str:
    return json.dumps({
        "selected_skill": "data-perspective",
        "skill_name": "数据透视打脸",
        "reason": "本章核心看点是主角第一次使用金手指反讽系统，需要'数据透视'类技能加持",
        "skill_prompt_block": "[特殊技能加成] 在主角使用反讽系统时，加入'数字流动'的视觉化描写，让读者有'看穿真相'的爽感。",
    }, ensure_ascii=False, indent=2)


def _mock_drama_card() -> str:
    return json.dumps({
        "chapter_num": 1,
        "title": "暴雨中的羞辱与觉醒",
        "core_event": "林辰被前东家裁员，在暴雨中被保安拦在公司门外，回家路上意外觉醒反讽系统",
        "key_scenes": [
            {"scene": "擎天科技大楼门口", "emotion": "屈辱→觉醒", "characters": ["林辰", "保安小张"]},
            {"scene": "回家路上", "emotion": "迷茫→震惊", "characters": ["林辰"]},
        ],
        "hook": "主角第一次使用反讽系统，看到前同事晋升邮件被人为篡改",
        "rhythm": "起（被裁）→ 承（回家）→ 转（觉醒）→ 合（第一次使用）",
    }, ensure_ascii=False, indent=2)


def _mock_brief_composer() -> str:
    return json.dumps({
        "writing_directives": [
            "本章节制 2500-3000 字",
            "主角林辰的核心性格是'冷静+毒舌'，保持短句+反讽的说话风格",
            "金手指'反讽系统'用蓝色字迹+数据对比的视觉化描写",
            "本章钩子：林辰看到前同事晋升邮件被人为篡改",
        ],
        "must_include": [
            "林辰被裁员时保安的态度（冷漠）",
            "觉醒反讽系统的具体过程（蓝色字迹+能量提示）",
            "林辰第一次使用反讽系统（识别数据篡改）",
        ],
        "must_avoid": [
            "不要让林辰当场反击前公司",
            "不要解释反讽系统的来源（留给后续章节）",
            "不要用'总而言之''刹那间'等套路化词汇",
        ],
    }, ensure_ascii=False, indent=2)


def _mock_chapter_type() -> str:
    return json.dumps({
        "chapters": [
            {"num": 1, "type": "开局+觉醒", "type_ratio": {"剧情": 0.5, "爽点": 0.3, "世界观": 0.2}},
            {"num": 2, "type": "现实交锋", "type_ratio": {"剧情": 0.4, "爽点": 0.4, "现实": 0.2}},
            {"num": 3, "type": "金手指初用", "type_ratio": {"爽点": 0.6, "剧情": 0.3, "悬疑": 0.1}},
        ],
    }, ensure_ascii=False, indent=2)


def _mock_extractor() -> str:
    return json.dumps({
        "new_settings": [
            {"name": "反讽系统 v1.0", "kind": "金手指", "description": "能看穿数据被篡改的痕迹，有能量限制"},
            {"name": "剩余能量 72%", "kind": "状态", "description": "系统当前能量值"},
        ],
        "new_characters": [
            {"name": "赵文澜", "role": "盟友", "initial_desc": "《硅基观察》主编，洞察力强，知道系统的存在"},
        ],
        "new_locations": [
            {"name": "中关村创业大街咖啡馆", "kind": "场所", "description": "林辰与赵文澜初遇的咖啡馆"},
        ],
    }, ensure_ascii=False, indent=2)


def _mock_planner(prompt: str = "") -> str:
    p = prompt or ""
    if "全书大纲" in p or "PLANNER_OUTLINE" in p:
        return json.dumps({"acts": _mock_full_outline()}, ensure_ascii=False)
    if "细化大纲" in p or "PLANNER_CHAPTER" in p:
        return json.dumps({
            "chapter_outline": "本章核心事件：林辰觉醒反讽系统，看到前同事晋升邮件被人为篡改，回家路上反复思考这个能力的来源与意义。\n\n戏剧节奏：起（被裁）→ 承（回家）→ 转（觉醒）→ 合（第一次使用）\n\n关键场景：1) 擎天科技大楼门口（冷漠对峙）2) 回家路上（觉醒）3) 出租屋深夜（第一次使用）\n\n字数目标：2500-3000 字",
        }, ensure_ascii=False)
    return json.dumps({"result": "（mock 模式：planner 通用兜底响应）"}, ensure_ascii=False)


def _mock_generic() -> str:
    # 当调用类型无法识别时，返回一段可用的扩展段落
    # （这样自动扩写/补写节点能继续工作）
    return """夜色更深了。

林辰靠在出租屋的窗边，看着手机屏幕上的反讽系统界面发呆。窗外的霓虹灯忽明忽暗，像极了他此刻的心情。

他想给赵文澜发一条消息，告诉她今天发生的事，但手指悬在键盘上迟迟没落下。

「也许她说得对，」他对自己说，「我得先把脑子里那个'系统'搞明白。」

林辰打开笔记本电脑，开始搜索「数据透视」「数据篡改痕迹」这些关键词。反讽系统似乎感应到了他的意图，视野的左下角弹出一行小字：

【正在检索相关知识...】
【匹配到 17 条历史数据样本】
【是否可视化？】

他点了「是」。

屏幕上的搜索结果瞬间变了——原本普通的搜索结果，每一条都多了一行极小的蓝色字迹，标注着「原始值」和「当前值」的差异。林辰揉了揉眼睛，确认自己没有看错。

「所以这个'反讽系统'，本质上是一个数据校验工具？」他低声问。

系统没有回答。但那行小字跳了一下，像是在说：「继续探索。」

林辰深吸一口气，把今晚的发现整理成一份简短的笔记。他知道，从今晚开始，他的人生已经彻底改变了。"""


# ── 总入口 ──

_MOCK_DISPATCH = {
    "brainstorm": _mock_brainstorm,
    "concept_directions": _mock_concept_directions,
    "refine_concept": _mock_refine_concept,
    "opening_outline": _mock_opening_outline,
    "full_outline": _mock_full_outline,
    "writer": _mock_writer,
    "editor": _mock_editor,
    "revise_with_feedback": _mock_revise_with_feedback,
    "chapter_continue": _mock_chapter_continue,
    "segment_rewrite": _mock_segment_rewrite,
    "extract_entities": _mock_extract_entities,
    "synopsis_generate": _mock_synopsis_generate,
    "synopsis_update": _mock_synopsis_update,
    "quality_assessor": _mock_quality_assessor,
    "quality_enhancer": _mock_quality_enhancer,
    "consistency_detector": _mock_consistency_detector,
    "style_drift_detector": _mock_style_drift_detector,
    "patcher": _mock_patcher,
    "risk_patcher": _mock_risk_patcher,
    "style_fingerprint": _mock_style_fingerprint,
    "skill_selector": _mock_skill_selector,
    "drama_card": _mock_drama_card,
    "brief_composer": _mock_brief_composer,
    "chapter_type": _mock_chapter_type,
    "extractor": _mock_extractor,
    "planner": _mock_planner,
}


def call_llm_mock(system_prompt: str, prompt: str, messages=None) -> tuple[str, str]:
    """返回 (call_type, response_text)。

    模拟一个轻微延迟，让前端 loading 状态能展示。
    """
    call_type = _detect_call_type(system_prompt or "", prompt or "", messages)
    fn = _MOCK_DISPATCH.get(call_type, _mock_generic)
    time.sleep(random.uniform(0.05, 0.25))

    try:
        if call_type == "refine_concept":
            text = fn(prompt or "")
        else:
            text = fn()
    except Exception as e:
        text = json.dumps({"_mock_error": str(e), "call_type": call_type}, ensure_ascii=False)

    # 调试日志（可通过 MOCK_DEBUG=1 启用）
    if os.getenv("MOCK_DEBUG", "").lower() in {"1", "true", "yes", "on"}:
        print(f"  🧪 MOCK call_type={call_type} len={len(text)} preview={text[:80]!r}", flush=True)

    return call_type, text
