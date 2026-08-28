# Security Policy

## Supported versions

| Version | Supported          |
|---------|--------------------|
| v1.x    | ✅ Active support   |
| < 1.0   | ❌ Not supported   |

We follow [semantic versioning](https://semver.org/). Patch releases (1.0.x) get security fixes; minor releases (1.x) get new features; major releases (x.0) may have breaking changes.

## Reporting a vulnerability

**Please do NOT report security issues via public GitHub issues.**

Instead, please report security issues privately:

- **Email**: security@your-domain.com (or open a [GitHub Security Advisory](https://github.com/<your-org>/dream-weaver/security/advisories/new))
- **What to include**:
  - Description of the vulnerability
  - Steps to reproduce
  - Affected versions (if known)
  - Your assessment of severity (Low / Medium / High / Critical)
  - Any known mitigations

We aim to:
- Acknowledge within **3 business days**
- Provide a fix timeline within **7 business days** of confirmation
- Credit the reporter in the release notes (unless anonymity is requested)

## Threat model

Dream Weaver runs **locally on your machine** by default. The threat model is:

- ✅ **In scope**: local file disclosure, XSS in the web UI, dependency CVEs, `MOCK_MODE` bypass, RCE via malicious prompts
- ⚠️ **Out of scope (by design)**: the LLM provider itself (trust your provider's security)
- ⚠️ **Out of scope**: novels stored in `novels/` (you control the directory; back it up however you like)

## Security best practices for users

1. **Never commit `.env`** — it contains API keys. The `.gitignore` already excludes it, but double-check before pushing.
2. **Run behind a reverse proxy** if exposing to the network. The FastAPI server is bound to localhost by default.
3. **Use a dedicated user account** if you share the host. Dream Weaver has multi-user auth (see USER_MANUAL).
4. **Keep `MOCK_MODE=0` only in trusted environments.** Mock mode intentionally returns preset data and is safe; real mode sends your prompts to the configured LLM provider.
5. **Review novel content for prompt injection** if you import drafts from untrusted sources — the writer agent may incorporate adversarial text into later chapters.
6. **Pin dependency versions** in `requirements.txt` for production. The current file uses `>=` for flexibility; consider switching to `==` for stricter installs.

## Known issues

| Issue | Severity | Status | Mitigation |
|---|---|---|---|
| None reported | — | — | — |

## Acknowledgments

We thank the following people for responsibly disclosing security issues:

*(none yet — be the first!)*
