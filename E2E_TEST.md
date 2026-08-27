# 端到端测试：跑一章 M3 写出来的实际质量

这个脚本会**真实**调用你的 LLM 配置（本地 Qwen + 云端 M3）跑一章完整流程，
看实际生成效果，对比之前 14B 模型的质量差距。

## 使用前提

1. **环境配置**：`.env` 至少包含
   ```env
   API_KEY=your_local_gpustack_key
   BASE_URL=http://localhost:8000/v1
   MINIMAX_API_KEY=your_minimax_key
   MINIMAX_BASE_URL=https://api.minimaxi.com/v1
   MINIMAX_THINKING_DISABLED=true
   ```

2. **本地模型服务在跑**（GPUStack 上有 qwen3.5-9b 和 qwen3.5-27b）

3. **MCP 服务在跑**：另开终端
   ```powershell
   cd E:\Program\minimax-code\网文
   python mcp_server.py
   ```

## 跑一次

```powershell
# 跑第 1 章
python e2e_run_chapter.py "被退婚后我成了绝顶高手"

# 跑第 5 章
python e2e_run_chapter.py "末日医生" --chapter 5

# 指定目标字数
python e2e_run_chapter.py "被退婚后我成了绝顶高手" --target-words 3000

# 跳过全书大纲生成（用预设大纲，更快）
python e2e_run_chapter.py "被退婚后我成了绝顶高手" --no-full
```

## 脚本会输出

1. **环境检查**：确认 .env 和 MCP 服务
2. **角色分配**：显示每个角色用什么模型
3. **第 1 章生成进度**：从大纲到正文到审稿
4. **Token 消耗**：总调用数、token、按角色 / provider 拆分
5. **正文预览**：前 800 字
6. **保存文件**：`output/e2e_chapter_<时间戳>.txt`

## 预期性能

| 阶段 | 时长 |
|---|---|
| 全文大纲 | 30-60s（Qwen 9B） |
| 章节大纲 | 5-10s |
| brief_composer | 10-20s（Qwen 9B） |
| 写作正文 | 30-90s（M3 写 2500 字） |
| 审稿 + 修补 | 20-60s（Qwen 27B） |
| 记忆 + 摘要 | 20-40s（3 个 LLM 并行） |
| **总计** | **约 3-6 分钟** |

## 性能对比

| 项目 | 之前（Qwen 14B） | 现在（多 provider） |
|---|---|---|
| Writer 文笔 | AI 味重、长篇飘 | M3 强文笔 + 512k 上下文 |
| Editor 逻辑 | 弱（14B 容易漏检） | 27B 逻辑强 + 硬规则 5 类校验 |
| Planner/Extractor | 14B | 9B 更快 |
| 摘要阶段 | 串行 3 次 LLM = ~30s | 并行 3 次 = ~10s |

## 跑完看什么

1. **正文质量**：
   - 有没有"金光""混沌"等空泛堆砌？
   - 人物对话有没有区分度？
   - 情节推进是否清晰？
2. **Token 成本**：
   - 单章大概多少 token？
   - 50 章连写的成本预估（×50）
3. **耗时**：
   - 本地 9B/27B 有没有成为瓶颈？
   - M3 是不是够快？

## 反馈

跑完后告诉我：
1. 文字质量对比 14B 时期如何？
2. 有没有任何报错？
3. Token 成本能接受吗？

如果某角色表现不好，可以改 `config.py` 的 `MODELS` 字典：
```python
MODELS = {
    "writer": {"model": "claude-sonnet-4-20250514", "provider": "anthropic", ...},
    "editor": {"model": "deepseek-v3", "provider": "deepseek", ...},
}
```
（需要先实现对应 provider）

## 故障排查

| 错误 | 原因 | 解决 |
|---|---|---|
| MCP 服务不可达 | mcp_server.py 没启动 | 另开终端运行 `python mcp_server.py` |
| qwen3.5-27b not found | GPUStack 没这个模型 | 部署 27b 或临时改 config.py 用 14b |
| MINIMAX_API_KEY 未配置 | .env 缺 key | 补上 MINIMAX_API_KEY |
| chapter_content < 100 字 | M3 thinking 没关 | 设 MINIMAX_THINKING_DISABLED=true |
| 反复"扩写" | 目标字数设太大 | 降低 --target-words 到 1500-2000 |
