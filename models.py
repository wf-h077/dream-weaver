"""多 provider LLM 调用层

设计要点：
1. 支持多 provider 路由（local GPUStack + MiniMax 开放 API）
2. 按角色 (role) 自动选择 provider，无需调用方关心
3. 全局累积 token 消耗统计（call_llm 用同一个 process 内共享的 UsageTracker）
4. 向后兼容：call_llm 默认返回 str；如需拿到 usage/finish_reason/工具调用
   等结构化结果，调用 call_llm(...) 并捕获返回值即可（见返回类型说明）。
5. M3 默认开启 thinking 会拖慢且消耗大量 token 写小说；建议关闭。
6. Qwen3 / Qwen3.5 通过 chat_template_kwargs.enable_thinking=False 关闭思考。
"""
from __future__ import annotations

import json
import re
import threading
import time
import os
import json
import contextvars
from dataclasses import dataclass, field, asdict
from typing import Any

# 当前 call_llm 调用的 novel_id（由 app 层在 graph node 入口设置）
CURRENT_NOVEL_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_novel_id", default=""
)

# ── MOCK 模式：跳过真实 LLM 调用，返回预设数据 ──
# 用法：在 .env 设 MOCK_MODE=1，重启服务即可。所有 call_llm() 会从 mock_data.py 返回内容，
# 让 GitHub 访客无需配置 LLM key 也能完整体验 UI 流程。
MOCK_MODE = os.getenv("MOCK_MODE", "").lower() in {"1", "true", "yes", "on"}

from openai import OpenAI

from config import (
    LOCAL_API_KEY,
    LOCAL_BASE_URL,
    MINIMAX_API_KEY,
    MINIMAX_BASE_URL,
    MODELS,
    get_model_config,
)


# ── Provider 客户端池（按 provider 懒加载，线程安全）──

_clients: dict[str, OpenAI] = {}
_clients_lock = threading.Lock()


def _get_client(provider: str) -> OpenAI:
    """根据 provider 名称获取（按需创建）OpenAI 客户端。"""
    with _clients_lock:
        if provider in _clients:
            return _clients[provider]

        if provider == "minimax":
            if not MINIMAX_API_KEY:
                raise RuntimeError(
                    "MINIMAX_API_KEY 未配置，但有模型角色被分配到 minimax provider。\n"
                    "请在 .env 中设置 MINIMAX_API_KEY，或把该角色的 provider 改回 local。"
                )
            client = OpenAI(base_url=MINIMAX_BASE_URL, api_key=MINIMAX_API_KEY)
        else:  # local / 其它统一走 local
            if not LOCAL_API_KEY:
                raise RuntimeError(
                    "API_KEY 未配置，本地 provider 无法启动。请在 .env 中设置 API_KEY 和 BASE_URL。"
                )
            client = OpenAI(base_url=LOCAL_BASE_URL, api_key=LOCAL_API_KEY)

        _clients[provider] = client
        return client


# ── Usage 统计 ──

@dataclass
class UsageRecord:
    """单次 LLM 调用的消耗记录。"""
    role: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_seconds: float = 0.0
    success: bool = True
    error: str = ""


@dataclass
class UsageTracker:
    """全局 token 消耗跟踪器（线程安全 + 持久化）。

    - `records`: 当前进程内的所有记录（一次性，重启清空）
    - `per_novel`: 按 novel_id 累加的 {calls, prompt, completion, total, duration, by_role, by_provider}
      **跨进程持久**，写到 data/usage_per_novel.json
    - `add(record)` 时根据 `CURRENT_NOVEL_ID` contextvar 自动归类
    """
    records: list[UsageRecord] = field(default_factory=list)
    per_novel: dict[str, dict] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # 持久化路径
    PERSIST_PATH: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "usage_per_novel.json",
    )

    def _empty_novel_bucket(self) -> dict:
        return {
            "total_calls": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "total_duration_seconds": 0.0,
            "by_role": {},
            "by_provider": {},
            "last_updated": 0.0,
        }

    def load_from_disk(self) -> None:
        """从磁盘恢复 per_novel 累加。"""
        if not os.path.exists(self.PERSIST_PATH):
            return
        try:
            with open(self.PERSIST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self.per_novel = data or {}
        except Exception as e:
            print(f"[usage] 加载持久化失败: {e}")

    def save_to_disk(self) -> None:
        """写盘。"""
        try:
            os.makedirs(os.path.dirname(self.PERSIST_PATH), exist_ok=True)
            tmp = self.PERSIST_PATH + ".tmp"
            with self._lock:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.per_novel, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, self.PERSIST_PATH)
        except Exception as e:
            print(f"[usage] 写盘失败: {e}")

    def add(self, record: UsageRecord) -> None:
        novel_id = (CURRENT_NOVEL_ID.get() or "").strip() or "__unknown__"
        with self._lock:
            self.records.append(record)
            bucket = self.per_novel.setdefault(novel_id, self._empty_novel_bucket())
            bucket["total_calls"] += 1
            bucket["total_prompt_tokens"] += record.prompt_tokens
            bucket["total_completion_tokens"] += record.completion_tokens
            bucket["total_tokens"] += record.total_tokens
            bucket["total_duration_seconds"] += record.duration_seconds
            bucket["last_updated"] = time.time()
            bucket["by_role"].setdefault(record.role, {"calls": 0, "tokens": 0})
            bucket["by_role"][record.role]["calls"] += 1
            bucket["by_role"][record.role]["tokens"] += record.total_tokens
            bucket["by_provider"].setdefault(record.provider, {"calls": 0, "tokens": 0})
            bucket["by_provider"][record.provider]["calls"] += 1
            bucket["by_provider"][record.provider]["tokens"] += record.total_tokens
        # 异步持久化（不阻塞 call_llm）
        try:
            self.save_to_disk()
        except Exception:
            pass

    def summary(self, novel_id: str | None = None) -> dict:
        """获取统计汇总。

        - novel_id=None: 当前进程内的 records 汇总（不跨重启）
        - novel_id="<具体 id>": per_novel 中该书的累计（跨重启）
        - novel_id="__all__": 全部书的累计
        """
        with self._lock:
            if novel_id == "__all__":
                # 全部书的累加
                agg = self._empty_novel_bucket()
                for nid, b in self.per_novel.items():
                    agg["total_calls"] += b["total_calls"]
                    agg["total_prompt_tokens"] += b["total_prompt_tokens"]
                    agg["total_completion_tokens"] += b["total_completion_tokens"]
                    agg["total_tokens"] += b["total_tokens"]
                    agg["total_duration_seconds"] += b["total_duration_seconds"]
                    for r, v in b["by_role"].items():
                        agg["by_role"].setdefault(r, {"calls": 0, "tokens": 0})
                        agg["by_role"][r]["calls"] += v["calls"]
                        agg["by_role"][r]["tokens"] += v["tokens"]
                    for p, v in b["by_provider"].items():
                        agg["by_provider"].setdefault(p, {"calls": 0, "tokens": 0})
                        agg["by_provider"][p]["calls"] += v["calls"]
                        agg["by_provider"][p]["tokens"] += v["tokens"]
                agg["by_novel"] = {
                    nid: {
                        "calls": b["total_calls"],
                        "tokens": b["total_tokens"],
                        "last_updated": b.get("last_updated", 0),
                    }
                    for nid, b in self.per_novel.items()
                }
                return agg

            if novel_id:
                bucket = self.per_novel.get(novel_id) or self._empty_novel_bucket()
                bucket = dict(bucket)  # copy
                bucket.pop("last_updated", None)
                return bucket

            # 默认：当前进程 records 汇总
            total_calls = len(self.records)
            total_prompt = sum(r.prompt_tokens for r in self.records)
            total_completion = sum(r.completion_tokens for r in self.records)
            total_tokens = sum(r.total_tokens for r in self.records)
            total_duration = sum(r.duration_seconds for r in self.records)
            by_role: dict[str, dict] = {}
            by_provider: dict[str, dict] = {}
            for r in self.records:
                by_role.setdefault(r.role, {"calls": 0, "tokens": 0})
                by_role[r.role]["calls"] += 1
                by_role[r.role]["tokens"] += r.total_tokens
                by_provider.setdefault(r.provider, {"calls": 0, "tokens": 0})
                by_provider[r.provider]["calls"] += 1
                by_provider[r.provider]["tokens"] += r.total_tokens
            return {
                "total_calls": total_calls,
                "total_prompt_tokens": total_prompt,
                "total_completion_tokens": total_completion,
                "total_tokens": total_tokens,
                "total_duration_seconds": round(total_duration, 2),
                "by_role": by_role,
                "by_provider": by_provider,
            }

    def reset(self, novel_id: str | None = None) -> None:
        with self._lock:
            self.records.clear()


USAGE = UsageTracker()
# 启动时从磁盘恢复 per_novel 累计
USAGE.load_from_disk()


def get_usage_summary(novel_id: str | None = None) -> dict:
    """获取 token 消耗汇总。

    - novel_id=None: 当前进程内 records 汇总（不跨重启）
    - novel_id="<id>": 该书跨重启的累计
    - novel_id="__all__": 全部书的累计
    """
    return USAGE.summary(novel_id=novel_id)


def set_current_novel_id(novel_id: str) -> None:
    """在调用 call_llm 之前设置当前 novel_id（让 UsageTracker 自动归类到这本书）。

    用法：在 graph node 入口、batch/invoke 前后调用。
    也可以通过 contextvars 的 CURRENT_NOVEL_ID 直接设置。
    """
    try:
        CURRENT_NOVEL_ID.set(novel_id or "")
    except Exception:
        pass


def reset_usage(novel_id: str | None = None) -> None:
    """重置统计。

    - novel_id=None: 清空当前进程 records + 全部 per_novel 累计
    - novel_id="<id>": 只清空该书的 per_novel 累计
    """
    USAGE.reset(novel_id=novel_id)
    USAGE.save_to_disk()


# ── 思考模式 / 模板控制 ──

NO_THINKING_INSTRUCTION = "\n【重要要求】请直接输出符合指令格式的最终内容，严禁输出任何分析、推理、思考过程或 <think> 内容。"


def is_qwen_model(model: str) -> bool:
    return "qwen" in (model or "").lower()


def is_minimax_model(model: str) -> bool:
    name = (model or "").lower()
    return "minimax" in name or "abab" in name


def build_extra_body(model: str, thinking_enabled: bool, model_config: dict) -> dict | None:
    """根据 provider/模型生成正确的 extra_body。

    - Qwen3/Qwen3.5：通过 chat_template_kwargs.enable_thinking 关闭
    - MiniMax M3：通过 chat_template_kwargs.thinking.type=disabled 关闭
    - 其它：直接透传 model_config 中的 extra_body
    """
    extra_body = dict(model_config.get("extra_body") or {})

    if thinking_enabled:
        return extra_body or None

    chat_template_kwargs = dict(extra_body.get("chat_template_kwargs") or {})

    if is_qwen_model(model):
        chat_template_kwargs["enable_thinking"] = False
    elif is_minimax_model(model):
        chat_template_kwargs["thinking"] = {"type": "disabled"}

    if chat_template_kwargs:
        extra_body["chat_template_kwargs"] = chat_template_kwargs

    # Qwen3.5 常见需要 top_k
    if "qwen3.5" in model.lower() and "top_k" not in extra_body:
        extra_body.setdefault("top_k", model_config.get("top_k", 20))

    return extra_body or None


def inject_no_thinking_system_message(messages: list, instruction: str) -> list:
    """向 system 消息末尾追加“不要思考”的强约束。"""
    msgs = [dict(msg) for msg in messages]
    for msg in msgs:
        if msg.get("role") == "system":
            msg["content"] = (msg.get("content") or "").strip() + "\n" + instruction
            return msgs
    return [{"role": "system", "content": instruction}, *msgs]


def extract_json_from_text(text: str) -> str | None:
    """从推理链或混合输出中尝试提取最完整的 JSON object。"""
    if not text:
        return None
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1].strip()
    return None


def _strip_think_blocks(text: str) -> str:
    """清除 LLM 输出中的 <think>...</think> 块（thinking 模型防护）。

    即使 thinking 没成功关闭，输出层兜底，避免推理草稿污染最终正文。
    """
    if not text or "<think>" not in text:
        return text
    # 清除 <think>...</think> 块（含多行）
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _safe_usage(usage_obj: Any) -> tuple[int, int, int]:
    """从 OpenAI 兼容的 usage 对象中安全提取 token 数字。"""
    if usage_obj is None:
        return 0, 0, 0
    prompt = getattr(usage_obj, "prompt_tokens", 0) or 0
    completion = getattr(usage_obj, "completion_tokens", 0) or 0
    total = getattr(usage_obj, "total_tokens", 0) or 0
    if not total and (prompt or completion):
        total = prompt + completion
    return int(prompt), int(completion), int(total)


def call_llm(
    role: str,
    prompt: str = None,
    system_prompt: str = "",
    temperature: float = 0.7,
    max_tokens: int = 4096,
    messages: list = None,
    tools: list = None,
    return_structured: bool = False,
) -> str | Any:
    """统一 LLM 调用入口。

    Args:
        role: 模型角色（决定 provider / model / thinking 等）
        prompt: 单轮 prompt（与 messages 二选一）
        system_prompt: 系统提示词
        temperature: 采样温度
        max_tokens: 单次最大输出 token
        messages: 多轮 messages（与 prompt 二选一）
        tools: OpenAI function calling tools
        return_structured: True 时返回 LLMResponse（包含 content/usage/tool_calls/finish_reason），
                           False 时保持向后兼容返回 str。

    Returns:
        默认返回 str（content），便于现有大量调用方无改动。
        return_structured=True 时返回 LLMResponse dataclass。
    """
    # ── MOCK 模式：跳过真实 LLM 调用，返回预设数据 ──
    if MOCK_MODE:
        try:
            from mock_data import call_llm_mock
            call_type, mock_text = call_llm_mock(system_prompt or "", prompt or "", messages)
            # 记录到 usage 跟踪器（mock 标记）
            mock_record = UsageRecord(
                role=role,
                model=f"[MOCK] {call_type}",
                provider="mock",
                prompt_tokens=len(prompt or "") // 2,
                completion_tokens=len(mock_text) // 2,
                total_tokens=(len(prompt or "") + len(mock_text)) // 2,
                duration_seconds=0.1,
                success=True,
            )
            USAGE.add(mock_record)
            if return_structured:
                return LLMResponse(
                    content=mock_text,
                    role=role,
                    model=f"[MOCK] {call_type}",
                    provider="mock",
                    duration_seconds=0.1,
                    usage={"prompt_tokens": mock_record.prompt_tokens, "completion_tokens": mock_record.completion_tokens, "total_tokens": mock_record.total_tokens},
                )
            return mock_text
        except Exception as e:
            print(f"  ⚠️ MOCK 模式异常: {e}，回退到真实调用")
            # 失败则继续走真实调用路径

    max_retries = 10
    retry_delay = 30

    for attempt in range(max_retries):
        start_time = time.time()
        record = UsageRecord(role=role, model="", provider="")
        try:
            model_config = get_model_config(role)
            model = model_config["model"]
            provider = model_config.get("provider", "local")
            thinking_enabled = model_config.get("thinking", True)

            record.model = model
            record.provider = provider

            current_temp = model_config.get("temperature", temperature)
            current_top_p = model_config.get("top_p", None)
            current_freq_pen = model_config.get("frequency_penalty", None)
            current_pres_pen = model_config.get("presence_penalty", None)

            current_system_prompt = system_prompt
            if not thinking_enabled:
                if current_system_prompt:
                    current_system_prompt = current_system_prompt.strip() + "\n" + NO_THINKING_INSTRUCTION
                else:
                    current_system_prompt = NO_THINKING_INSTRUCTION

            if messages is None:
                msgs = []
                if current_system_prompt:
                    msgs.append({"role": "system", "content": current_system_prompt})
                if prompt:
                    msgs.append({"role": "user", "content": prompt})
            else:
                msgs = messages.copy()
                if not thinking_enabled:
                    msgs = inject_no_thinking_system_message(msgs, NO_THINKING_INSTRUCTION)

            kwargs = {
                "model": model,
                "messages": msgs,
                "temperature": current_temp,
                "max_tokens": max_tokens,
            }
            if current_top_p is not None:
                kwargs["top_p"] = current_top_p
            if current_freq_pen is not None:
                kwargs["frequency_penalty"] = current_freq_pen
            if current_pres_pen is not None:
                kwargs["presence_penalty"] = current_pres_pen
            if tools:
                kwargs["tools"] = tools

            extra_body = build_extra_body(model, thinking_enabled, model_config)
            if extra_body:
                kwargs["extra_body"] = extra_body

            client = _get_client(provider)

            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as _e:
                error_str = str(_e).lower()
                if (
                    "auto\" tool choice" in error_str
                    or "tool_choice" in error_str
                    or "enable-auto-tool-choice" in error_str
                ):
                    print(f"  ⚠️ {provider} provider 不支持自动工具调用，已降级（移除 tools）...")
                    if "tools" in kwargs:
                        del kwargs["tools"]
                    if "tool_choice" in kwargs:
                        del kwargs["tool_choice"]
                    response = client.chat.completions.create(**kwargs)
                else:
                    raise _e

            if not hasattr(response, "choices") or not response.choices:
                raise RuntimeError(f"API 返回了非法响应 (no choices): {response}")

            message = response.choices[0].message
            content = message.content
            # 输出层兜底：清除 thinking 块污染（即使 disable 没生效）
            if content:
                content = _strip_think_blocks(content)
            reasoning = getattr(message, "reasoning", None) or getattr(message, "reasoning_content", None)

            if content:
                print(f"  DEBUG: [LLM Response] provider={provider} role={role} model={model} content_length={len(content)}")
            elif reasoning:
                print(f"  DEBUG: [LLM Response] provider={provider} role={role} model={model} content=None, reasoning_length={len(reasoning)}")
                json_text = extract_json_from_text(reasoning)
                if json_text:
                    content = json_text
                    print(f"  ✅ 从推理链中提取到 JSON 结构 ({len(content)} chars)")
                else:
                    content = reasoning[-2000:] if len(reasoning) > 2000 else reasoning
                    print(f"  ⚠️ 未找到明显 JSON 结构，取推理链末尾块作为兜底")
            else:
                print(f"  DEBUG: [LLM Response] provider={provider} role={role} model={model} content=None, reasoning=None")

            # 累计 token
            usage_obj = getattr(response, "usage", None)
            p, c, t = _safe_usage(usage_obj)
            record.prompt_tokens = p
            record.completion_tokens = c
            record.total_tokens = t
            record.duration_seconds = round(time.time() - start_time, 3)
            record.success = True
            USAGE.add(record)

            if return_structured:
                return LLMResponse(
                    content=content or "",
                    reasoning=reasoning,
                    tool_calls=message.tool_calls,
                    finish_reason=response.choices[0].finish_reason,
                    usage={"prompt_tokens": p, "completion_tokens": c, "total_tokens": t},
                    role=role,
                    model=model,
                    provider=provider,
                    duration_seconds=record.duration_seconds,
                )

            # 默认行为：tool_calls 仍返回原始 message 对象
            if message.tool_calls:
                return message

            return content if content else ""

        except Exception as e:
            record.duration_seconds = round(time.time() - start_time, 3)
            record.success = False
            record.error = str(e)[:200]
            USAGE.add(record)

            error_str = str(e).lower()
            is_rate_limit = any(x in error_str for x in [
                "429", "rate limit", "too many requests", "速率限制", "非法响应", "no choices",
            ])

            if is_rate_limit and attempt < max_retries - 1:
                print(f"  ⏳ [第{attempt+1}次重试] 触发模型频率限制({role}/{record.model})... 等待 {retry_delay} 秒")
                time.sleep(retry_delay)
                retry_delay = min(120, int(retry_delay * 1.5))
                continue
            else:
                print(f"  ❌ 发生不可恢复错误或达到最大重试次数: {e}")
                if return_structured:
                    return LLMResponse(content="", error=str(e), role=role, model=record.model, provider=record.provider)
                raise e


@dataclass
class LLMResponse:
    """call_llm(return_structured=True) 时的结构化返回。"""
    content: str = ""
    reasoning: str | None = None
    tool_calls: Any = None
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)
    role: str = ""
    model: str = ""
    provider: str = ""
    duration_seconds: float = 0.0
    error: str = ""


# ── 上下文截断（避免 prompt 超 32K token 限制）──

# 不同模型的上下文限制（按 M3/Qwen 32768 设定）
MODEL_MAX_CONTEXT_TOKENS = 32768
# 留 8000 tokens 给输出 (用户可设 4096) + 系统 prompt (~2000) + 业务 prompt (~2000) + 业务扩展
# 实际经验：M3/Qwen tokenizer 1 中文字符 ≈ 1 token
# 用户 max_tokens 默认 2400-4096，所以输入预算 = 32768 - 4096 - 4000 = ~24000，但留 buffer 用 20000
MAX_INPUT_TOKENS_BUDGET = 20000
# 中文字符 → token 经验比（按 1 字符 = 1 token）
CHARS_PER_TOKEN_CN = 1.0


def estimate_tokens(text: str) -> int:
    """粗估 token 数（中文 1 字符 ≈ 1 token，英文按 4 字符 ≈ 1 token）。

    按 M3/Qwen tokenizer 实测：1 中文字符 ≈ 1 token（含标点/换行）。
    """
    if not text:
        return 0
    cn = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cn
    return int(cn * CHARS_PER_TOKEN_CN + other / 4)


def truncate_to_chars(text: str, max_chars: int, head: int = 0) -> str:
    """截断文本到 max_chars 字符。优先保留末尾（最新内容），可选保留前 head 字。

    中文小说场景：前情摘要末尾 = 最近剧情，保留；开头 = 早期背景可丢。
    """
    if not text or len(text) <= max_chars:
        return text
    if head > 0 and head < max_chars:
        return text[:head] + "\n...（中段省略 " + str(len(text) - max_chars) + " 字）...\n" + text[-(max_chars - head - 60):]
    return "...（前段省略 " + str(len(text) - max_chars) + " 字）...\n" + text[-max_chars:]


def truncate_state_context(state: dict, max_input_tokens: int = MAX_INPUT_TOKENS_BUDGET) -> dict:
    """根据总 token 预算，截断 state['story_so_far'] / ['bible_context'] / ['chapter_outline']。

    返回**新 dict**（不修改原 state），把截断后的字段替换好。
    """
    if not state:
        return state
    new_state = dict(state)
    ssf = new_state.get("story_so_far", "") or ""
    bbc = new_state.get("bible_context", "") or ""
    co = new_state.get("chapter_outline", "") or ""

    # 单字段最大预算（按 1 token ≈ 1 字符估算）
    ssf_budget = int(max_input_tokens * 0.35)  # 约 9000 字（story_so_far 占比最大）
    bbc_budget = int(max_input_tokens * 0.20)  # 约 5200 字
    co_budget = int(max_input_tokens * 0.10)   # 约 2600 字

    ssf_tok = estimate_tokens(ssf)
    bbc_tok = estimate_tokens(bbc)
    co_tok = estimate_tokens(co)
    total = ssf_tok + bbc_tok + co_tok

    # 总超预算才截断
    if total > max_input_tokens:
        if ssf_tok > ssf_budget:
            new_state["story_so_far"] = truncate_to_chars(ssf, ssf_budget, head=200)
        if bbc_tok > bbc_budget:
            new_state["bible_context"] = truncate_to_chars(bbc, bbc_budget, head=200)
        if co_tok > co_budget:
            new_state["chapter_outline"] = truncate_to_chars(co, co_budget, head=0)
        # 再次估算
        ssf_tok = estimate_tokens(new_state.get("story_so_far", ""))
        bbc_tok = estimate_tokens(new_state.get("bible_context", ""))
        co_tok = estimate_tokens(new_state.get("chapter_outline", ""))
        # 仍然超，强制各砍一半
        if ssf_tok + bbc_tok + co_tok > max_input_tokens:
            for k, b in (("story_so_far", ssf_budget // 2), ("bible_context", bbc_budget // 2), ("chapter_outline", co_budget // 2)):
                new_state[k] = truncate_to_chars(new_state.get(k, ""), b, head=100)
        return new_state
    return new_state


def is_input_within_budget(prompt: str, system_prompt: str = "", max_input_tokens: int = MAX_INPUT_TOKENS_BUDGET) -> bool:
    """判断 prompt + system_prompt 是否在 token 预算内。"""
    return estimate_tokens(prompt) + estimate_tokens(system_prompt or "") <= max_input_tokens

