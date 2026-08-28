---
name: 🐛 Bug report
about: Report a bug so we can fix it
title: "[Bug] "
labels: ["bug", "needs-triage"]
assignees: []
---

## 🐛 What happened?

<!-- A clear, concise description of the bug. -->

## 📋 Steps to reproduce

1. ...
2. ...
3. ...

## ✅ Expected behavior

<!-- What did you expect to happen? -->

## ❌ Actual behavior

<!-- What actually happened? Include any error messages. -->

## 🖼️ Screenshots / Logs

<!-- If applicable, add screenshots or paste logs here. -->

```
[paste logs here]
```

## 🛠️ Environment

- **OS**: [e.g. Windows 11 / macOS 14 / Ubuntu 22.04]
- **Python version**: [output of `python --version`]
- **MOCK_MODE**: [output of `grep MOCK_MODE .env` (or "not set")]
- **LLM provider**: [local GPUStack / MiniMax / mock / other]
- **Install method**: [pip / docker / source]

## 🔍 Self-diagnosis

<!-- Check what you've already tried. -->

- [ ] I read [WRITING_CHECKLIST.md](../../blob/main/WRITING_CHECKLIST.md) and [USER_MANUAL.md](../../blob/main/USER_MANUAL.md)
- [ ] I tried with `MOCK_MODE=1` to isolate LLM provider issues
- [ ] I checked the server log (`work/app_mock.log` or stdout)
- [ ] I searched existing issues for duplicates

## 📎 Additional context

<!-- Anything else relevant: novel size, recent changes, related issues. -->
