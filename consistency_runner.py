"""跑 analyze_consistency.py，把结果存到 utf-8 文件"""
import sys
import subprocess
from pathlib import Path

# 设置 PYTHONIOENCODING=utf-8
env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

result = subprocess.run(
    [sys.executable, "-X", "utf8", "analyze_consistency.py", "output/batch_1787401442"],
    capture_output=True,
    env={**__import__("os").environ, **env},
)

# 解码为 UTF-8
out = result.stdout.decode("utf-8", errors="replace")
err = result.stderr.decode("utf-8", errors="replace")

# 保存
Path(".runlogs/consistency_clean.log").write_text(out, encoding="utf-8")
Path(".runlogs/consistency_err.log").write_text(err, encoding="utf-8")

print(f"stdout chars: {len(out)}")
print(f"stderr chars: {len(err)}")
print(f"returncode: {result.returncode}")

# 打印所有包含中文的行
for line in out.splitlines():
    if any(s in line for s in ["林", "铁", "稳定", "优秀", "改进", "评估", "所有", "漂移", "一致", "跨章", "出现"]):
        print("KEY:", line)
