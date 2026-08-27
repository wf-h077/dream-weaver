"""局部修补节点 + 模糊匹配引擎

修补策略（按优先级）：
1. 精确字符串匹配
2. strip 后精确匹配
3. 标点/空白规范化后匹配（全角/半角、空格/换行）
4. 行级 difflib 滑动窗口找最相似连续行块
5. 句子级 difflib 找最相似句（target 为单句时）

阈值根据 target 长度动态调整，避免短字符串误替换。
"""
import difflib
import json
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from models import call_llm
from prompts import PATCHER_SYSTEM, PATCHER_PROMPT, RISK_PATCHER_SYSTEM, RISK_PATCHER_PROMPT
from task_progress import report_progress
from memory import StoryBible, DEFAULT_NOVEL_ID
from agents.text_quality import normalize_chapter_text, clean_ai_format_artifacts
from state import NovelState


# ═══════════════════════════════════════════════════════
# 模糊匹配引擎
# ═══════════════════════════════════════════════════════

def _normalize_for_match(text: str) -> str:
    """规范化用于匹配：去空白差异 + 全角半角转换 + 标点归一。

    不改变字符串长度意义（保留字数），但让"看起来一致"的字符串能匹配上。
    """
    if not text:
        return ""
    # NFKC 归一：全角英数/标点 -> 半角
    text = unicodedata.normalize("NFKC", text)
    # 把常见中文标点也归一（全角逗号和半角逗号视为一致）
    text = text.replace("，", ",").replace("。", ".").replace("：", ":").replace("；", ";")
    text = text.replace("！", "!").replace("？", "?")
    text = text.replace("（", "(").replace("）", ")").replace("【", "[").replace("】", "]")
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    text = text.replace("、", ",")
    # 折叠空白
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _threshold_for_length(target_len: int) -> float:
    """根据 target 长度动态选择相似度阈值。

    阈值放宽（从 0.92/0.82/0.72/0.62/0.55 → 0.78/0.68/0.58/0.50/0.45）：
    实战中发现 27B 模型输出的 target 经常和原文差异较大（标点、同义词、微调），
    原阈值导致 patcher 频繁 "所有匹配策略未达标"。
    """
    if target_len < 8:
        return 0.78
    if target_len < 30:
        return 0.68
    if target_len < 100:
        return 0.58
    if target_len < 300:
        return 0.50
    return 0.45  # 长字符串：允许略低相似度


def _line_level_match(text: str, target: str) -> Tuple[Optional[int], int, float]:
    """行级 difflib 滑动窗口。

    Returns:
        (start_line_idx, num_lines, ratio) 或 (None, 0, 0.0)
    """
    target_lines = [l for l in target.split("\n") if l.strip()]
    text_lines = text.split("\n")
    n = len(target_lines)
    if n == 0 or n > len(text_lines):
        return None, 0, 0.0

    target_text = "\n".join(target_lines)
    best_ratio = 0.0
    best_idx = -1
    for i in range(len(text_lines) - n + 1):
        window_lines = text_lines[i:i + n]
        # 跳过空行窗口
        if not any(l.strip() for l in window_lines):
            continue
        window = "\n".join(window_lines)
        ratio = difflib.SequenceMatcher(None, target_text, window, autojunk=False).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = i

    if best_idx < 0:
        return None, 0, 0.0
    return best_idx, n, best_ratio


def _sentence_level_match(text: str, target: str) -> Tuple[Optional[int], Optional[int], float]:
    """句子级 difflib 匹配（target 是单句时使用）。

    Returns:
        (start_offset, end_offset, ratio) 或 (None, None, 0.0)
    """
    # 用中文/英文句号、问号、感叹号、分号切句
    sentences = re.split(r"(?<=[。！？!?；;\n])", text)
    sentences = [s for s in sentences if s.strip()]
    if not sentences:
        return None, None, 0.0

    target_clean = target.strip()
    if not target_clean:
        return None, None, 0.0

    best_ratio = 0.0
    best_sent = None
    for sent in sentences:
        ratio = difflib.SequenceMatcher(
            None, target_clean, sent.strip(), autojunk=False
        ).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_sent = sent

    if best_sent is None or best_sent not in text:
        return None, None, 0.0

    start = text.find(best_sent)
    end = start + len(best_sent)
    return start, end, best_ratio


def _extend_to_paragraph(text: str, offset: int) -> Tuple[int, int]:
    """把单句 offset 扩展到整段（按 \n\n 分段）。"""
    # 向前找段落开头
    para_start = text.rfind("\n\n", 0, offset)
    para_start = 0 if para_start < 0 else para_start + 2
    # 向后找段落结尾
    para_end = text.find("\n\n", offset)
    para_end = len(text) if para_end < 0 else para_end
    return para_start, para_end


def fuzzy_find_and_replace(
    text: str,
    target: str,
    replacement: str,
    min_ratio: Optional[float] = None,
) -> Tuple[str, bool, str]:
    """多级模糊匹配 + 替换。

    Args:
        text: 原始全文
        target: 要替换的原文片段
        replacement: 替换文本
        min_ratio: 强制最低相似度（不传则按 target 长度自动）

    Returns:
        (new_text, success, reason)
    """
    if not text or not target:
        return text, False, "text 或 target 为空"

    target_clean = target.strip()
    if not target_clean:
        return text, False, "target 仅含空白"

    replacement_clean = (replacement or "").strip()
    if not replacement_clean:
        return text, False, "replacement 为空"

    threshold = min_ratio if min_ratio is not None else _threshold_for_length(len(target_clean))

    # 1) 精确匹配
    if target in text:
        return text.replace(target, replacement), True, "精确匹配"

    # 2) strip 后精确匹配
    if target_clean in text and target_clean != target:
        return text.replace(target_clean, replacement), True, "去除前后空白后精确匹配"

    # 3) 标点/空白规范化后匹配
    norm_target = _normalize_for_match(target)
    norm_text = _normalize_for_match(text)
    if norm_target and norm_target in norm_text:
        # 找到 norm_target 在 norm_text 中的位置
        # 简化处理：在 text 中用滑动窗口找最相似的连续片段
        idx, n, ratio = _line_level_match(text, target)
        if idx is not None and ratio >= threshold:
            text_lines = text.split("\n")
            new_lines = text_lines[:idx] + replacement.split("\n") + text_lines[idx + n:]
            return "\n".join(new_lines), True, f"标点规范化后行级匹配 (ratio={ratio:.2f})"

    # 4) 行级 difflib 滑动窗口
    idx, n, ratio = _line_level_match(text, target)
    if idx is not None and ratio >= threshold:
        text_lines = text.split("\n")
        new_lines = text_lines[:idx] + replacement.split("\n") + text_lines[idx + n:]
        return "\n".join(new_lines), True, f"行级模糊匹配 (ratio={ratio:.2f}, threshold={threshold:.2f})"

    # 5) 句子级 difflib（target 较短时更有效）
    if len(target_clean) <= 200:
        sent_start, sent_end, sent_ratio = _sentence_level_match(text, target)
        if sent_start is not None and sent_ratio >= threshold:
            # 扩展到整段
            para_start, para_end = _extend_to_paragraph(text, sent_start)
            new_text = text[:para_start] + replacement.strip() + text[para_end:]
            return new_text, True, f"句子级模糊匹配 (ratio={sent_ratio:.2f}, 扩展为段落)"

    return text, False, f"所有匹配策略未达标 (best_line={ratio:.2f}, threshold={threshold:.2f})"


# ═══════════════════════════════════════════════════════
# 风险 patcher 解析（保持原有逻辑）
# ═══════════════════════════════════════════════════════

def strip_json_fence(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if text.startswith("```json"):
        text = text[7:].strip()
    elif text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def parse_risk_patch_json(raw_text: str) -> list[dict]:
    text = strip_json_fence(raw_text)
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

    if isinstance(data, list):
        patches = data
    elif isinstance(data, dict):
        patches = data.get("patches", [])
        if not patches and isinstance(data.get("patch"), dict):
            patches = [data.get("patch")]
    else:
        patches = []
    if not isinstance(patches, list):
        return []
    normalized = []
    for item in patches:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target", "")).strip()
        replacement = str(item.get("replacement", "")).strip()
        if not target or not replacement:
            continue
        normalized.append({
            "target": [target],
            "instruction": [str(item.get("reason", "专项一致性风险修补")).strip() or "专项一致性风险修补"],
            "replacement": replacement,
            "source": "risk_detector",
        })
    return normalized


def build_risk_patches(chapter_content: str, risk_patch_required: str) -> list[dict]:
    if not risk_patch_required.strip():
        return []
    report_progress("正在根据专项风险生成局部补丁...", "patch")
    prompt = RISK_PATCHER_PROMPT.format(
        risk_patch_required=risk_patch_required,
        chapter_content=chapter_content,
    )
    raw_result = call_llm(
        role="editor",
        system_prompt=RISK_PATCHER_SYSTEM,
        prompt=prompt,
        temperature=0.1,
        max_tokens=4096,
    )
    return parse_risk_patch_json(raw_result)


# ═══════════════════════════════════════════════════════
# 主节点
# ═══════════════════════════════════════════════════════

def patch_chapter(state: NovelState) -> Dict[str, Any]:
    """LangGraph 节点：执行局部修补。

    基于 Editor 输出的缺陷，调用 LLM 进行点对点级别的段落替换。
    支持多级模糊匹配：精确 → strip → 标点规范化 → 行级 difflib → 句子级 difflib。
    """
    chapter_content = state["chapter_content"]
    edit_required = state.get("edit_required", "")
    risk_patch_required = state.get("risk_patch_required", "")
    chapter_num = state["current_chapter"]
    edit_round = state.get("edit_count", 0)
    bible = StoryBible(state.get("novel_id", DEFAULT_NOVEL_ID))

    print(f"\n[打补丁机制] 启动局部修补...")
    report_progress("正在执行局部修补...", "patch")

    patches = []
    lines = edit_required.split('\n')
    current_target: list[str] = []
    current_instruction: list[str] = []
    mode = None

    for line in lines:
        stripped = line.strip()
        if (
            stripped.startswith("## 专项一致性风险")
            or stripped.startswith("专项一致性检测发现")
            or stripped.startswith("### 专项风险")
        ):
            mode = None
            continue
        if stripped.startswith("- 定位：") or stripped.startswith("定位："):
            mode = "target"
            val = stripped.split("定位：", 1)[1].strip()
            if val.startswith("[") and val.endswith("]"): val = val[1:-1]
            current_target = [val]
        elif stripped.startswith("- 指令：") or stripped.startswith("指令："):
            mode = "instruction"
            val = stripped.split("指令：", 1)[1].strip()
            if val.startswith("[") and val.endswith("]"): val = val[1:-1]
            current_instruction = [val]
            if current_target and current_instruction:
                patches.append({
                    "target": current_target.copy(),
                    "instruction": current_instruction.copy(),
                })
        else:
            if mode == "target":
                current_target.append(stripped)
            elif mode == "instruction":
                if patches:
                    patches[-1]["instruction"].append(stripped)
                else:
                    current_instruction.append(stripped)

    risk_patches = build_risk_patches(chapter_content, risk_patch_required)
    if risk_patches:
        print(f"  [风险] 专项风险生成 {len(risk_patches)} 个候选补丁")
        report_progress(f"专项风险生成 {len(risk_patches)} 个候选补丁", "patch")
        patches.extend(risk_patches)

    # ── 限制单轮 patcher 处理数量（避免 LLM 调用过多）──
    MAX_PATCHES_PER_ROUND = 8
    if len(patches) > MAX_PATCHES_PER_ROUND:
        print(f"  [打补丁] 候选补丁 {len(patches)} 个超过上限 {MAX_PATCHES_PER_ROUND}，只处理前 {MAX_PATCHES_PER_ROUND} 个")
        patches = patches[:MAX_PATCHES_PER_ROUND]

    if not patches:
        print("  [打补丁] 未能解析出明确的补丁定位和指令，跳过修补...")
        report_progress("未解析到可执行补丁，跳过局部修补", "patch")
        return {"chapter_content": chapter_content}

    new_content = chapter_content
    success_count = 0
    fuzzy_count = 0  # 模糊匹配成功数
    fail_reasons: list[str] = []

    for i, patch in enumerate(patches):
        t_text = "\n".join(patch["target"]).strip()
        i_text = "\n".join(patch["instruction"]).strip()

        if not t_text or not i_text:
            continue

        print(f"  [打补丁] {i+1}/{len(patches)}: target={t_text[:30]!r}...")
        report_progress(f"正在执行补丁 {i+1}/{len(patches)}", "patch")

        if patch.get("replacement"):
            replacement = patch["replacement"]
        else:
            prompt = PATCHER_PROMPT.format(target_text=t_text, instruction=i_text)
            replacement = call_llm(
                role="editor",
                system_prompt=PATCHER_SYSTEM,
                prompt=prompt,
                temperature=0.3,
            )

        # 清理 markdown 包裹
        replacement = (replacement or "").strip()
        if replacement.startswith("```"):
            replacement = "\n".join(replacement.split("\n")[1:])
            if replacement.endswith("```"):
                replacement = "\n".join(replacement.split("\n")[:-1])

        # 多级模糊匹配 + 替换
        new_content, patch_success, reason = fuzzy_find_and_replace(
            new_content, t_text, replacement
        )

        if patch_success:
            success_count += 1
            if reason != "精确匹配" and reason != "去除前后空白后精确匹配":
                fuzzy_count += 1
            print(f"     [OK] {reason}")
        else:
            fail_reasons.append(reason)
            print(f"     [FAIL] {reason}")

        bible.add_patch_record(
            chapter_num=chapter_num,
            edit_round=edit_round,
            patch_index=i + 1,
            target_text=t_text,
            instruction=i_text,
            replacement_text=replacement,
            success=patch_success,
            reason=reason,
        )

    if success_count > 0:
        precision_msg = (
            f"，其中模糊匹配 {fuzzy_count} 处"
            if fuzzy_count > 0 else "（全部精确匹配）"
        )
        print(f"  [打补丁] 完成：成功 {success_count}/{len(patches)}{precision_msg}")
        report_progress(
            f"局部修补完成，成功 {success_count} 处" + precision_msg, "patch"
        )
        bible.add_chapter_version(
            chapter_num,
            "after_patch",
            new_content,
            f"局部修补后版本，成功 {success_count} 处（模糊匹配 {fuzzy_count} 处）",
        )
    else:
        print(f"  [打补丁] 全部 {len(patches)} 个补丁均未成功匹配")
        report_progress(
            f"局部修补失败：{len(patches)} 个补丁均未匹配", "patch"
        )

    return {"chapter_content": normalize_chapter_text(clean_ai_format_artifacts(new_content), dedupe=True)}
