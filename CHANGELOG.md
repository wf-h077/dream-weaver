# Changelog

All notable changes to Dream Weaver (造梦者) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Better `MOCK_MODE` coverage (some edge cases still fall through to real calls)
- Optional Docker compose file
- Translation of USER_MANUAL.md to English
- More issue / PR templates

---

## [1.0.0] — 2026-08-28

### 🎉 First public release

This is the initial public release of Dream Weaver. The project has been in private development and is now ready for community use.

### Added

#### Core features
- **Multi-agent pipeline**: `planner` / `writer` / `editor` / `patcher` / `reviewer` — each with its own prompt template and model role
- **Layered memory (MCP)**: separate `mcp_server.py` + SQLite, structured storage for lore / characters / foreshadowing / chapter versions
- **Cross-chapter retrieval**: when writing each chapter, auto-fetch relevant lore and previous chapters
- **Intelligent patching**: localized risk fixes + full-chapter feedback revision + one-click version rollback
- **Long-form companion tools**: chapter type detection / blueprint coverage / anti-AI-flavor check / character consistency / foreshadowing tracker
- **Real-time dashboard**: word count / token cost / character usage / time / type distribution / consistency scan
- **Dual LLM provider**: local GPUStack (Qwen3.5) + cloud MiniMax M3 (512K context)
- **Multi-user auth** with invite codes

#### New in this release
- 🆕 **Chapter constraint planning**: specify `num_chapters` (5–100) when refining a concept; AI generates one `chapter_constraints` entry per chapter
- 🆕 **Chapter navigation**: prev/next/jump-to buttons + `[` `]` keyboard shortcuts
- 🆕 **Chapter version history + rollback**: every save / revision auto-saves a version; restore any past version
- 🆕 **AI feedback revision**: reader gives feedback → AI applies targeted edits (preserves good parts)
- 🆕 **Friendly error messages**: rate limits / network / JSON parse / context-overflow all get a 💡 actionable hint
- 🆕 **MOCK mode** (`MOCK_MODE=1`): try the entire UI with preset data — no LLM key required
- 🆕 **Performance optimizations (P0-P2)**:
  - Batched word-count API (49× speedup)
  - In-memory TTL cache (50% hit rate)
  - Frontend esbuild minify + GZip (218KB → 50KB)
  - SQLite composite indexes (3874-row query: full scan → 3.9ms)
  - Skeleton screens + optimistic updates

#### Developer experience
- 🆕 **MCP server split**: data layer is now a separate process (`mcp_server.py` on port 8001) so you can run app + MCP independently
- 🆕 **Public API**: all endpoints documented and tested
- 🆕 **GitHub community files**: 4 issue templates, PR template, CONTRIBUTING, CI workflow, dependabot
- 🆕 **English README** + GIF demo (30s)
- 🆕 **Chinese docs** (`README.zh-CN.md`, `USER_MANUAL.md`, `WRITING_CHECKLIST.md`)

### Technical

- Python 3.10+ required
- FastAPI + SQLite + LangGraph-inspired custom multi-agent
- Vanilla HTML/CSS/JS frontend (no React/Vue) — keeps the bundle small
- LF line endings for all source files
- No new heavy dependencies added — the project intentionally has a small footprint

### Security

- 🆕 First public release — no prior vulnerabilities
- All sensitive config (`.env`, `novels/`, `*.db`, `.runlogs/`) is git-ignored
- See [SECURITY.md](SECURITY.md) for the vulnerability disclosure policy

### Migration notes

This is the first public release — no migration needed.

If you were running an earlier private build:
- The `mcp_server.py` is now a separate process. Run it before `app.py`.
- `.env` variables may have changed; copy from `.env.example`.
- The chapters table schema is unchanged; existing `novels/` directories should still load.

### Known limitations

- Frontend has minor font rendering issues on Safari (Chrome/Edge/Firefox are fine)
- Mobile UI is functional but not optimized for long-form reading
- `MOCK_MODE` doesn't cover all endpoints (some edge cases still fall through)
- No automatic backup scheduler (you must trigger `POST /api/admin/backup` manually or via cron)

---

## Release process

For maintainers:

1. Update this CHANGELOG with the version's changes
2. Bump version in `config.py` if applicable
3. Tag the commit: `git tag -a v1.x.y -m "v1.x.y"`
4. Push the tag: `git push origin v1.x.y`
5. Create a GitHub release with the CHANGELOG excerpt

[Unreleased]: https://github.com/<your-org>/dream-weaver/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/<your-org>/dream-weaver/releases/tag/v1.0.0
