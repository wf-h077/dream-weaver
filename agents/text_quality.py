from __future__ import annotations

import difflib
import re
from typing import Any


TITLE_RE = re.compile(r"^第\s*[一二三四五六七八九十百千万零\d]+\s*章")


def split_long_paragraph(paragraph: str, soft_limit: int = 120, hard_limit: int = 220) -> list[str]:
    text = (paragraph or "").strip()
    if len(text) <= hard_limit:
        return [text] if text else []
    parts = re.split(r"(?<=[。！？；…])", text)
    chunks: list[str] = []
    buf = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if buf and len(buf) + len(part) > hard_limit:
            chunks.append(buf.strip())
            buf = part
        else:
            buf += part
        if len(buf) >= soft_limit and re.search(r"[。！？；…]$", buf):
            chunks.append(buf.strip())
            buf = ""
    if buf.strip():
        chunks.append(buf.strip())
    return chunks or [text]


def paragraph_key(paragraph: str) -> str:
    return re.sub(r"\s+", "", paragraph or "")


def sentence_key(sentence: str) -> str:
    return re.sub(r"[\s，。！？；：“”\"'、,.!?;:]+", "", sentence or "")


def split_chapter_paragraphs(content: str) -> tuple[str, list[str]]:
    text = (content or "").strip()
    if not text:
        return "", []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    raw_lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not raw_lines:
        return "", []
    title = ""
    if TITLE_RE.match(raw_lines[0]):
        title = raw_lines[0]
        raw_lines = raw_lines[1:]

    paragraphs: list[str] = []
    for line in raw_lines:
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if len(line) > 220:
            paragraphs.extend(split_long_paragraph(line))
        else:
            paragraphs.append(line)
    return title, paragraphs


def normalize_chapter_text(content: str, dedupe: bool = False, dedupe_threshold: float = 0.7) -> str:
    title, paragraphs = split_chapter_paragraphs(content)
    cleaned: list[str] = []
    seen: set[str] = set()
    previous_key = ""
    # P2 修复：跳过与 title 高度重复的 paragraph（AI 经常输出"第 1 章 标题\n标题\n正文"两次）
    # 用相似度匹配，因为 title 带"第X章"前缀，paragraph_key 不能简单比较
    if title and dedupe:
        # 把 title 去掉"第X章"前缀后也加入 seen
        title_stripped = TITLE_RE.sub("", title).strip()
        if title_stripped:
            seen.add(paragraph_key(title_stripped))
    for paragraph in paragraphs:
        # 跳过所有看起来像"第X章 标题"的行（保留首个 title）
        if TITLE_RE.match(paragraph) and len(paragraph) < 60:
            continue
        key = paragraph_key(paragraph)
        if not key:
            continue
        if dedupe and len(key) >= 4:
            # 0) 与 title 高度重复 → 跳过（修复"标题写两遍"）
            if title and key in seen:
                continue
            # 0.5) 与 title_stripped 模糊匹配（如 paragraph="暴雨中的羞辱与觉醒"，title="第1章 暴雨中的羞辱与觉醒"）
            if title:
                stripped = TITLE_RE.sub("", title).strip()
                if stripped:
                    stripped_key = paragraph_key(stripped)
                    if len(stripped_key) >= 4 and stripped_key in key:
                        # paragraph 包含完整 title_stripped（典型重复）
                        continue
            # 1) 精确重复
            if key in seen:
                continue
            # 2) 与前一段近似（局部去重，避免 patcher 修补后重复）
            if previous_key:
                similarity = difflib.SequenceMatcher(None, previous_key, key).ratio()
                if similarity >= dedupe_threshold:
                    continue
            # 3) 与最近 5 段内任一段近似（防 patch 跨多段插入重复）
            for prev_key in list(seen)[-5:]:
                sim = difflib.SequenceMatcher(None, prev_key, key).ratio()
                if sim >= dedupe_threshold:
                    break
            else:
                seen.add(key)
                cleaned.append(paragraph)
                previous_key = key
                continue
            continue
        cleaned.append(paragraph)
        previous_key = key
        if dedupe and len(key) >= 4:
            seen.add(key)
    rows = ([title] if title else []) + cleaned
    return "\n\n".join(rows).strip()


# ═══════════════════════════════════════════════════════
# AI 写作常见格式问题清理（保存前最后一里）
# ═══════════════════════════════════════════════════════

import re as _re_for_ai_clean


def clean_ai_format_artifacts(content: str) -> str:
    """清理 AI 写作常见格式问题（保存前调用）。

    处理项：
    1. 删除表情符（网文平台不支持）
    2. 删除 markdown 残留
    3. 折叠连续标点
    4. 统一引号（全角）
    5. 删除空段 / 单字符段
    6. 合并单字换行
    7. 补全未闭合引号
    8. 清除 `` 残留
    9. 折叠连续空行
    10. 删除首尾空白
    """
    if not content:
        return content
    text = content

    # 0) 清除 <think> 残留（防御性，正常不应有）
    text = _re_for_ai_clean.sub(r"<think>.*?</think>", "", text, flags=_re_for_ai_clean.DOTALL)
    text = _re_for_ai_clean.sub(r"<think>.*?(?=\n\n|\Z)", "", text, flags=_re_for_ai_clean.DOTALL)

    # 1) 删除表情符（保留中文标点）
    # 范围：U+1F300-U+1FAFF 各种 emoji + U+2600-U+27BF 符号 + 一些零散
    text = _re_for_ai_clean.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF"
        r"\U0001F600-\U0001F64F\U0001F900-\U0001F9FF"
        r"\u2700-\u27BF\u2600-\u26FF]+",
        "",
        text,
    )

    # 2) 删除 markdown 残留
    # 标题: # ## ### 等
    text = _re_for_ai_clean.sub(r"^#{1,6}\s+", "", text, flags=_re_for_ai_clean.MULTILINE)
    # 加粗: **text** 或 __text__
    text = _re_for_ai_clean.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = _re_for_ai_clean.sub(r"__(.+?)__", r"\1", text)
    # 斜体: *text* 或 _text_
    text = _re_for_ai_clean.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"\1", text)
    # 行内代码: `text`
    text = _re_for_ai_clean.sub(r"`([^`\n]+)`", r"\1", text)
    # 代码块
    text = _re_for_ai_clean.sub(r"```[^\n]*\n.*?\n```", "", text, flags=_re_for_ai_clean.DOTALL)
    # 链接: [text](url)
    text = _re_for_ai_clean.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # 列表项
    text = _re_for_ai_clean.sub(r"^\s*[-*+]\s+", "", text, flags=_re_for_ai_clean.MULTILINE)
    text = _re_for_ai_clean.sub(r"^\s*\d+\.\s+", "", text, flags=_re_for_ai_clean.MULTILINE)
    # 引用
    text = _re_for_ai_clean.sub(r"^>\s+", "", text, flags=_re_for_ai_clean.MULTILINE)

    # 3) 统一引号（全角）
    # 英文双引号 → 中文双引号
    # 注意：保留嵌套关系——遇到开头/结尾时切换
    out_chars: list[str] = []
    open_quote = True
    for ch in text:
        if ch == '"':
            out_chars.append("\u201c" if open_quote else "\u201d")
            open_quote = not open_quote
        elif ch == "'":
            out_chars.append("\u2018" if open_quote else "\u2019")
            open_quote = not open_quote
        else:
            out_chars.append(ch)
    text = "".join(out_chars)
    # 半角单引号 → 全角（更彻底）
    text = text.replace("'", "\u2018").replace("\u2019\u2018", "\u2019")

    # 4) 折叠连续标点
    # 3 个以上连续标点 → 2 个
    text = _re_for_ai_clean.sub(r"\.{3,}", "……", text)  # 英文 ...
    text = _re_for_ai_clean.sub(r"…{2,}", "……", text)  # 多个省略号
    text = _re_for_ai_clean.sub(r"！{2,}", "！！", text)
    text = _re_for_ai_clean.sub(r"？{2,}", "？？", text)
    text = _re_for_ai_clean.sub(r"，{2,}", "，", text)
    text = _re_for_ai_clean.sub(r"。{2,}", "。", text)
    text = _re_for_ai_clean.sub(r"～{2,}", "～", text)
    text = _re_for_ai_clean.sub(r"~{2,}", "～", text)
    # 混合标点折叠
    text = _re_for_ai_clean.sub(r"[。！？]{2,}", lambda m: m.group(0)[0] * 2, text)
    # 标题前的多余点: 第1章 标题.....  → 第1章 标题
    text = _re_for_ai_clean.sub(r"^第.{1,30}章[.]{2,}", lambda m: m.group(0).rstrip("."), text)

    # 5) 删除空段 / 单字符段
    paragraphs = text.split("\n")
    new_paragraphs = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        # 单字符段（"。" "，" 等）直接合并到上一段
        if len(p) <= 1 and new_paragraphs:
            new_paragraphs[-1] = new_paragraphs[-1].rstrip() + p
            continue
        # 只有标点的段
        if _re_for_ai_clean.fullmatch(r"[\s，。！？、…—\-~～!?,.\u201c\u201d\u2018\u2019]+", p):
            if new_paragraphs:
                new_paragraphs[-1] = new_paragraphs[-1].rstrip() + p
            continue
        new_paragraphs.append(p)
    text = "\n".join(new_paragraphs)

    # 6) 合并单字换行（如果一行只有 1-3 个字且不是对话，合并到上一段）
    lines = text.split("\n")
    merged: list[str] = []
    for line in lines:
        stripped = line.strip()
        # 标题行不合并
        if TITLE_RE.match(stripped):
            merged.append(line)
            continue
        # 单字行（且不是对话），合并到上一段
        if (
            merged
            and 1 <= len(stripped) <= 3
            and not stripped.startswith(("「", "\"", "'", "\u201c", "\u2018"))
            and not stripped.endswith(("」", "\"", "'", "\u201d", "\u2019"))
            and not _re_for_ai_clean.search(r"[，。！？；…\u201d\u2019]", stripped[-1:])
        ):
            merged[-1] = merged[-1].rstrip() + stripped
        else:
            merged.append(line)
    text = "\n".join(merged)

    # 7) 折叠连续空行（3+ → 2）
    text = _re_for_ai_clean.sub(r"\n{3,}", "\n\n", text)

    # 8) 删除首尾空白
    text = text.strip()

    return text


def extract_chapter_title(content: str, chapter_num: int | None = None) -> str:
    text = (content or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    for line in text.split("\n")[:12]:
        line = line.strip()
        if not line:
            continue
        direct = TITLE_RE.match(line)
        if direct:
            return line[:60]
        embedded = re.search(r"第\s*[一二三四五六七八九十百千万零\d]+\s*章[^\n。！？；：:]*", line)
        if embedded:
            return embedded.group(0).strip()[:60]
    if chapter_num:
        return f"第{chapter_num}章"
    return ""


def ensure_chapter_title(content: str, chapter_num: int, old_content: str = "") -> str:
    normalized = normalize_chapter_text(content, dedupe=True)
    title, paragraphs = split_chapter_paragraphs(normalized)
    if title:
        return normalized
    fallback_title = extract_chapter_title(old_content, chapter_num) or f"第{chapter_num}章"
    rows = [fallback_title] + paragraphs
    return "\n\n".join(row for row in rows if row).strip()


def first_body_paragraph(content: str) -> str:
    _, paragraphs = split_chapter_paragraphs(content)
    return paragraphs[0] if paragraphs else ""


def looks_like_fake_fix(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return True
    if len(text) < 300 and re.search(r"(已修复|修复完成|建议|问题已经|无法修复|以下是)", text):
        return True
    return False


def looks_like_broken_opening(content: str) -> bool:
    first = first_body_paragraph(content)
    if not first:
        return True
    if len(first) <= 8:
        return True
    if re.match(r"^[\u4e00-\u9fff]{1,2}[。！？]", first):
        return True
    if re.match(r"^[，。！？；、）】”’]", first):
        return True
    if re.match(r"^(了|着|见证|知道|发现|因此|于是|然而|但是|但|而|这才|终于)", first) and len(first) < 35:
        return True
    return False


def detect_repetition_issues(content: str) -> list[dict[str, Any]]:
    title, paragraphs = split_chapter_paragraphs(content)
    issues: list[dict[str, Any]] = []
    if not paragraphs:
        return issues

    keys = [paragraph_key(p) for p in paragraphs if paragraph_key(p)]
    exact_duplicate_count = len(keys) - len(set(keys))
    if exact_duplicate_count > 0:
        issues.append({
            "category": "重复段落",
            "severity": "must_fix" if exact_duplicate_count >= 2 else "warning",
            "message": f"发现 {exact_duplicate_count} 处完全重复段落，读感会显得廉价。",
            "suggestion": "删除重复段落，改为新的动作、信息增量、对话推进或冲突变化。",
        })

    near_duplicate_pairs = 0
    for index in range(1, len(keys)):
        if len(keys[index]) < 24 or len(keys[index - 1]) < 24:
            continue
        if difflib.SequenceMatcher(None, keys[index - 1], keys[index]).ratio() >= 0.88:
            near_duplicate_pairs += 1
    if near_duplicate_pairs:
        issues.append({
            "category": "近似重复",
            "severity": "must_fix" if near_duplicate_pairs >= 2 else "warning",
            "message": f"发现 {near_duplicate_pairs} 组相邻段落表达高度相似。",
            "suggestion": "压缩近似表达，保留一处情绪，另一处改成新的剧情动作或信息。",
        })

    sentence_counts: dict[str, int] = {}
    for sentence in re.split(r"(?<=[。！？；…])", "\n".join(paragraphs)):
        key = sentence_key(sentence)
        if len(key) >= 12:
            sentence_counts[key] = sentence_counts.get(key, 0) + 1
    repeated_sentences = [key for key, count in sentence_counts.items() if count >= 2]
    if repeated_sentences:
        issues.append({
            "category": "重复句子",
            "severity": "must_fix" if len(repeated_sentences) >= 3 else "warning",
            "message": f"发现 {len(repeated_sentences)} 个句子或短语重复出现。",
            "suggestion": "删去重复句，改用具体动作、感官细节、人物选择或新信息替代。",
        })

    long_paragraphs = [p for p in paragraphs if len(p) > 220]
    if long_paragraphs:
        issues.append({
            "category": "段落过长",
            "severity": "warning",
            "message": f"发现 {len(long_paragraphs)} 个超长段落，不利于移动端阅读。",
            "suggestion": "按动作、对话、心理和场景变化拆分段落，每段 1-3 句。",
        })

    filler_patterns = [
        r"他不知道的是",
        r"她不知道的是",
        r"空气仿佛凝固",
        r"心中.*一动",
        r"眼神.*复杂",
        r"没有人知道",
    ]
    filler_hits = sum(len(re.findall(pattern, content)) for pattern in filler_patterns)
    if filler_hits >= 5:
        issues.append({
            "category": "套路化表达",
            "severity": "warning",
            "message": f"发现 {filler_hits} 处高频套路化表达。",
            "suggestion": "减少模板句，用具体动作、道具、选择和信息变化替代表情绪空转。",
        })

    return issues


def format_repetition_issues(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return "未发现明显重复或低质格式问题。"
    lines = []
    for item in issues:
        lines.append(
            f"- [{item.get('severity', 'warning')}] {item.get('category', '重复风险')}："
            f"{item.get('message', '')} 建议：{item.get('suggestion', '')}"
        )
    return "\n".join(lines)
