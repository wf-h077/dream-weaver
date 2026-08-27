# 🌙 造梦者 (Dream Weaver) | AI 长篇网文创作平台

[![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Framework](https://img.shields.io/badge/Framework-FastAPI-009688.svg)](https://fastapi.tiangolo.com/)
[![Multi--Agent](https://img.shields.io/badge/Architecture-Multi--Agent-orange.svg)](#)

> 一句话：**为长篇网文作者设计的"AI 副驾"**——从灵感立项到连载百章，从单章生成到全书一致性，从 AI 写稿到读者反馈修订，全流程覆盖。

造梦者是一个**多 Agent 协同的网文创作系统**。它不仅能写单章，还能管理 100+ 章的长篇连载，自动维护大纲、人物卡、势力卡、世界规则、伏笔库——保证 AI 写出来的内容"前后对得上、人物不崩、设定一致"。

---

## ✨ 它能做什么

### 🎯 全流程覆盖

```
灵感 → 立项方向 → 深度细化 → 全书章节蓝图 → 第 1 章 → 自动续写 → 反馈修订 → ...
       ↓                ↓              ↓                ↓
   3 个方向选择   100 章约束清单   100 章目标     自动按约束写
                   + 角色/势力     + 升级节奏
```

### 🛠️ 核心能力

| 能力 | 说明 |
|---|---|
| **🧠 多 Agent 协同** | planner（策划）/ writer（写作）/ editor（编辑）/ patcher（修补）/ reviewer（复盘）各司其职 |
| **📚 分层记忆（MCP）** | 独立的 `mcp_server.py` + SQLite，结构化存储 lore / 角色 / 伏笔 / 章节版本 |
| **🔍 跨章检索** | 写每章时自动从全书检索相关 lore/设定/前情，避免跑题 |
| **✂️ 智能修补** | 局部风险修复 + 整章反馈修订 + 一键回滚到任意历史版本 |
| **🎭 动态技能** | 内置 combat / emotion / dialogue 等专门技能，按需调度 |
| **💡 长篇陪跑工具** | 章节类型检测 / 大纲覆盖度 / 反 AI 味检测 / 角色一致性 / 伏笔追踪 |
| **📊 仪表盘** | 字数 / 成本 / 角色 / 仪表 / 类型分布 / 一致性扫描 实时展示 |
| **⏪ 版本回滚** | 每次保存/修订都自动留版本，一键恢复到任意历史版本（防误删） |
| **🌐 双 Provider** | 本地 GPUStack（Qwen 系列）+ 云端 MiniMax M3（512k 长上下文） |

### 🎨 写作流程

1. **立项**：3 个自动生成的方向选择
2. **细化**：填章节数目 → AI 生成 100 章约束清单 + 角色/势力/规则
3. **蓝图**：全书章节蓝图 → AI 自动拆解 N 章
4. **开始写**：第 1 章 AI 自动生成（含大纲 + 角色状态 + 前情）
5. **续写**：第 2/3/... 章自动按约束清单写
6. **反馈修订**：写完后读者给反馈，AI 按反馈局部修订
7. **修补**：检测到风险时 AI 自动修补
8. **复盘**：写完 30/50/100 章后做类型分布 / 一致性扫描

---

## 🚀 快速开始

### 前置要求

- Python 3.10+
- 一个 AI Provider：
  - **本地 GPUStack**（推荐）：[gpustack/gpustack](https://github.com/gpustack/gpustack) + Qwen3.5-9B/27B 模型
  - **或云端 MiniMax M3**：[MiniMax 开放平台](https://api.minimaxi.com) 注册获取 API key

### 安装

```bash
# 1. 克隆仓库
git clone https://github.com/<your-org>/dream-weaver.git
cd dream-weaver

# 2. 安装依赖
pip install -r requirements.txt

# 3. 复制环境变量
cp .env.example .env
# 编辑 .env 填入真实 API key
```

### 启动

```bash
# 启动顺序：先 mcp_server（数据层），再 app（API + 前端）
python mcp_server.py    # 端口 8001
python app.py          # 端口 8050
```

打开浏览器：[http://localhost:8050](http://localhost:8050)

### 第一次使用

1. 注册账号（首个账号自动是 admin）
2. 点"创作"标签 → 写一句话灵感 → 自动生成 3 个立项方向
3. 选 1 个方向 → 深度细化（**填章节数目**，如 100）→ AI 生成全章约束
4. 点"保存并生成全书章节蓝图"
5. 点"自动生成下一章"

> 详细的"避坑清单"请看 [WRITING_CHECKLIST.md](WRITING_CHECKLIST.md)

---

## 🏗️ 项目结构

```
dream-weaver/
├── app.py              # 🌐 FastAPI 主服务（端口 8050）
├── mcp_server.py       # 💾 MCP 数据服务（端口 8001）
├── memory.py           # 📚 StoryBible 封装
├── graph.py            # 🔀 LangGraph 工作流
├── models.py           # 🤖 多 provider LLM 调用
├── prompts.py          # 💬 所有 prompt 模板
├── state.py            # 📊 全局状态类型
├── config.py           # ⚙️ 配置加载
├── auth.py             # 🔐 鉴权
├── cache.py            # ⚡ TTL 缓存（P0-2 优化）
│
├── agents/             # 🤖 5 个 Agent
│   ├── planner.py      # 策划：立项、细化、章节蓝图
│   ├── writer.py       # 写作：写章节
│   ├── editor.py       # 编辑：风险检测、整章反馈修订
│   ├── patcher.py      # 修补：局部风险修补
│   └── reviewer.py     # 复盘：长篇陪跑工具
│
├── tools/              # 🛠️ Agent 自助工具
│   ├── search.py       # 跨章检索
│   ├── extract.py      # 实体抽取
│   └── ...
│
├── skills/             # 🎭 动态技能
│   ├── combat.json
│   ├── emotion.json
│   └── dialogue.json
│
├── static/             # 🎨 前端
│   ├── index.html
│   ├── app.js          # 主逻辑（minify 后 ~220KB）
│   └── style.css       # 暗色主题 + 移动端适配
│
├── prompts_style_presets.py  # 8 大网文风格预设（番茄/起点等）
├── publish_uploader.py       # 番茄小说发布
├── backup.py                  # 数据库备份
├── cloud_backup.py            # 云端备份（S3/OSS）
│
├── README.md
├── USER_MANUAL.md            # 详细用户操作手册
├── WRITING_CHECKLIST.md      # 写作避坑清单
├── E2E_TEST.md                # 端到端测试文档
├── .env.example               # 环境变量示例
└── LICENSE                    # MIT
```

---

## 🧬 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI + SQLite + MCP |
| Agent | 自研多 Agent（不用 LangGraph/LangChain） |
| 缓存 | Python 自研 TTL 缓存（无 Redis 依赖） |
| 前端 | 原生 HTML/CSS/JS（无 React/Vue 依赖） |
| AI | Qwen3.5 / MiniMax M3 等 OpenAI 兼容 API |
| 存储 | SQLite + 文件系统（章节 .txt） |

---

## 🎯 适用人群

- **长篇网文作者**：想用 AI 加速但又怕质量跑偏
- **工作室 / 编辑**：管理多个连载项目，需要标准化流程
- **AI 应用开发者**：参考多 Agent 系统的实现
- **网文运营**：番茄 / 起点 / 七猫 等平台的 AI 辅助写手

---

## 📊 性能优化（P0-P2）

- **P0 1**：批量字数 API（`/api/novels/{id}/chapters/stats`）— 1 次请求替代 50 次并发 fetch，**加速 49×**
- **P0 2**：内存 TTL 缓存（status 3s / novels 10s / stats 15s）— 命中率 50%
- **P1 1**：前端 esbuild minify + GZip 压缩 — 218KB / 50KB
- **P1 2**：SQLite 复合索引（5 个 novel_id 索引）— pending_extractions（3874 行）查询从全表扫描降至 3.9ms
- **P2**：骨架屏 + 乐观更新（归档/删除立即响应）

---

## 📚 文档

- [USER_MANUAL.md](USER_MANUAL.md) — 详细功能操作手册
- [WRITING_CHECKLIST.md](WRITING_CHECKLIST.md) — 写作避坑清单（必读）
- [E2E_TEST.md](E2E_TEST.md) — 端到端测试文档

---

## 🛡️ 隐私与安全

- **本地优先**：所有数据（章节、用户、版本历史）默认存本地 SQLite + 文件系统
- **不上传用户作品**：`novels/`、`story_bible.db` 已在 `.gitignore` 排除
- **API key 由你掌控**：`.env` 文件不上传，配置由你自己管理
- **可选云端备份**：AWS S3 / 阿里云 OSS / 任意兼容 S3 协议存储

---

## 🤝 贡献

欢迎 PR、Issue、Discussion。具体可看 [WRITING_CHECKLIST.md](WRITING_CHECKLIST.md) 了解项目痛点。

---

## 📜 License

[MIT](LICENSE) — 你可以自由使用、修改、商用，但保留版权声明。

---

## 🌟 致谢

- 感谢 [LangGraph](https://github.com/langchain-ai/langgraph) 启发的多 Agent 设计思路
- 感谢 [GPUStack](https://github.com/gpustack/gpustack) 让本地 GPU 推理变得简单
- 感谢所有网文作者——是你们的内容让 AI 有了学习方向

---

<p align="center">
  <b>造梦者 v1.0</b> · 让 AI 写完一整本小说不再是梦
</p>
