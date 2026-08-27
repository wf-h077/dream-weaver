"""全局配置模块"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── API 配置（多 provider 路由）──
# 本地 GPUStack / OpenAI 兼容接口（Qwen 系列）
LOCAL_API_KEY = os.getenv("API_KEY")
LOCAL_BASE_URL = os.getenv("BASE_URL", "http://localhost:8000/v1")

# MiniMax 开放 API（writer 角色使用，强文笔 + 512k 长上下文）
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY")
MINIMAX_BASE_URL = os.getenv(
    "MINIMAX_BASE_URL",
    "https://api.minimaxi.com/v1",
)
# 写正文时建议关闭 thinking 模式，输出更稳定；如需强推理可设 false
MINIMAX_THINKING_DISABLED = os.getenv("MINIMAX_THINKING_DISABLED", "true").lower() in {
    "1", "true", "yes", "on",
}

# ── 旧字段，向后兼容（直接被 models.py / 其它模块 import）──
API_KEY = LOCAL_API_KEY
BASE_URL = LOCAL_BASE_URL

# ── 模型名称映射 ──
# 角色分配策略：
#   planner/extractor  → 本地 Qwen3.5-9B   短文本规划/抽取，零成本、速度快
#   writer             → MiniMax-M3        强文笔 + 512k 上下文，专做长篇正文
#   editor / patcher   → 本地 Qwen3.5-27B  中大杯模型，逻辑分析强，本地零成本
#
# 修改说明：
#   1) 全部用 OpenAI 兼容 chat completions 接口
#   2) "provider" 字段决定走 LOCAL 还是 MINIMAX
#   3) "thinking" 字段控制是否启用思考模式
#   4) 调用方只需 call_llm(role=...) 即可，无需关心 provider
MODELS = {
    "planner":   {"model": "qwen3.5-9b",   "provider": "local",   "thinking": False, "top_k": 20},
    "writer":    {"model": "MiniMax-M3",   "provider": "minimax", "thinking": not MINIMAX_THINKING_DISABLED,
                  "temperature": 0.85, "top_p": 0.95},
    "editor":    {"model": "qwen3.5-27b",  "provider": "local",   "thinking": False},
    "patcher":   {"model": "qwen3.5-27b",  "provider": "local",   "thinking": False},
    "extractor": {"model": "qwen3.5-9b",   "provider": "local",   "thinking": False, "top_k": 20},
    "summarizer":{"model": "qwen3.5-9b",   "provider": "local",   "thinking": False, "top_k": 20},
}

# ── 数据库配置 ──
SQLITE_DB_PATH = os.path.join(os.path.dirname(__file__), "story_bible.db")

# ── 输出配置 ──
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output2")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── 创作参数 ──
MAX_EDIT_ROUNDS = 2          # 最大审核-重写循环次数（从 3 降到 2，节省 5 分钟）
CHAPTER_MIN_WORDS = 2000     # 每章最少字数
CHAPTER_MAX_WORDS = 3000     # 每章最多字数


def get_chapter_word_range(target_words: int | str | None) -> tuple[int, int]:
    """Return the enforceable word-count range for a chapter target.

    用户填写"2000 字"时，预期通常是接近 2000，而不是 1500 也算达标。
    这里采用较紧的下限和略宽的上限，兼顾平台字数稳定性与模型自然波动。
    """
    try:
        target = int(target_words or CHAPTER_MIN_WORDS)
    except (TypeError, ValueError):
        target = CHAPTER_MIN_WORDS
    target = max(500, target)
    upper_slack = min(500, max(250, int(target * 0.20)))
    return target, target + upper_slack


def get_model_config(role: str) -> dict:
    """获取指定角色的模型配置，找不到则回退到 planner。"""
    return MODELS.get(role, MODELS["planner"])


def get_provider_for(role: str) -> str:
    """获取指定角色的 provider（local / minimax）。"""
    return get_model_config(role).get("provider", "local")
