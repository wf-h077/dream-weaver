---
name: ⚡ Performance issue
about: Report slow generation, high memory, or token cost
title: "[Perf] "
labels: ["performance", "needs-triage"]
assignees: []
---

## 🐢 What's slow / heavy?

<!-- E.g. "Chapter generation takes 5 min", "Token cost is 50k per chapter" -->

## 📊 Numbers

- **Single chapter generation**: [e.g. 240s]
- **Token cost per chapter**: [e.g. 12k prompt + 3k completion]
- **Total token budget (so far)**: [from /api/usage]
- **Chapter length**: [e.g. 2500 chars]
- **Number of agents / roles involved**: [e.g. 7 — planner/writer/editor/...]

## 🔬 Reproduction conditions

- **LLM provider**: [local GPUStack / MiniMax]
- **Model per role**:
  - planner: [...]
  - writer: [...]
  - editor: [...]
  - patcher: [...]
- **Novel size**: [e.g. 50 chapters, 120k words]
- **MCP server status**: [running / not running]
- **Cache stats**: [from /api/admin/cache_stats]

## 📋 Logs

```
[paste generation logs here — especially the per-phase timing]
```

## 💡 My guess at the bottleneck

<!--
- LLM round-trip latency
- Repeated cross-chapter retrieval
- Patcher re-running after editor
- Story bible growing past context window
- ...
-->

## 🛠️ What I've already tried

- [ ] Switched to faster model for planner
- [ ] Increased `MAX_INPUT_TOKENS_BUDGET`
- [ ] Disabled quality_gate / editor pass
- [ ] Used `MOCK_MODE=1` to confirm slowness is LLM-related, not UI
