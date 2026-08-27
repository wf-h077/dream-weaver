"""Optional browser automation for platform draft upload.

The uploader deliberately stops at draft preparation. It does not bypass login,
CAPTCHA, platform checks, or final publishing.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


DEFAULT_FANQIE_WRITER_URL = os.getenv(
    "FANQIE_WRITER_URL",
    "https://fanqienovel.com/writer/zone",
)


class PublishUploaderError(RuntimeError):
    pass


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


@dataclass
class FanqieUploadSession:
    session_id: str
    created_at: float
    writer_url: str = DEFAULT_FANQIE_WRITER_URL
    status: str = "created"
    message: str = ""
    last_action_at: float = field(default_factory=time.time)
    playwright: Any = None
    context: Any = None
    page: Any = None

    def to_public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "message": self.message,
            "writer_url": self.writer_url,
            "created_at": self.created_at,
            "last_action_at": self.last_action_at,
            "current_url": self.safe_current_url(),
        }

    def safe_current_url(self) -> str:
        try:
            return self.page.url if self.page else ""
        except Exception:
            return ""


class FanqieDraftUploader:
    def __init__(self, profile_dir: str):
        self.profile_dir = profile_dir
        self.sessions: dict[str, FanqieUploadSession] = {}

    def capability(self) -> dict[str, Any]:
        return {
            "available": playwright_available(),
            "dependency": "playwright",
            "install_hint": "pip install playwright && python -m playwright install chromium",
            "mode": "semi_auto_draft",
            "supports": [
                "打开番茄作家后台",
                "用户自行登录",
                "在新增章节页尝试填入标题和正文",
                "可选尝试点击保存草稿",
            ],
            "limits": [
                "不保存账号密码",
                "不处理验证码",
                "不点击最终发布",
                "页面改版时可能需要更新选择器",
            ],
        }

    def start(self, writer_url: str | None = None) -> FanqieUploadSession:
        if not playwright_available():
            raise PublishUploaderError("未安装 Playwright，无法启动半自动上传浏览器")

        from playwright.sync_api import sync_playwright

        os.makedirs(self.profile_dir, exist_ok=True)
        # 清理可能残留的 SingletonLock / DevToolsActivePort 文件
        # （上次 Chrome 异常退出时可能留下，导致 exitCode=21）
        profile_path = os.path.join(self.profile_dir, "fanqie")
        os.makedirs(profile_path, exist_ok=True)
        for stale in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile", "DevToolsActivePort"):
            stale_path = os.path.join(profile_path, stale)
            if os.path.exists(stale_path):
                try:
                    os.remove(stale_path)
                except OSError:
                    pass

        session_id = uuid.uuid4().hex
        session = FanqieUploadSession(
            session_id=session_id,
            created_at=time.time(),
            writer_url=(writer_url or DEFAULT_FANQIE_WRITER_URL).strip() or DEFAULT_FANQIE_WRITER_URL,
        )
        playwright = sync_playwright().start()
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=profile_path,
                headless=False,
                viewport={"width": 1440, "height": 900},
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as e:
            playwright.stop()
            raise PublishUploaderError(
                f"启动浏览器失败：{e}\n\n"
                f"💡 排查建议：\n"
                f"  1) 关闭所有 chrome.exe 进程（PowerShell: Get-Process chrome | Stop-Process -Force）\n"
                f"  2) 手动删除 lockfile：Remove-Item '{profile_path}\\lockfile' -Force\n"
                f"  3) 重新启动 app.py"
            )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(session.writer_url, wait_until="domcontentloaded")

        session.playwright = playwright
        session.context = context
        session.page = page
        session.status = "waiting_user"
        session.message = "浏览器已打开。请在番茄作家后台登录，并进入目标作品的新增章节/编辑草稿页面。"
        session.last_action_at = time.time()
        self.sessions[session_id] = session
        return session

    def get(self, session_id: str) -> FanqieUploadSession:
        session = self.sessions.get(session_id)
        if not session:
            raise PublishUploaderError("上传会话不存在或已关闭")
        return session

    def close(self, session_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        try:
            if session.context:
                session.context.close()
            if session.playwright:
                session.playwright.stop()
        finally:
            self.sessions.pop(session_id, None)
        return {"session_id": session_id, "status": "closed"}

    def fill_chapter(self, session_id: str, title: str, body: str) -> dict[str, Any]:
        session = self.get(session_id)
        if not session.page:
            raise PublishUploaderError("浏览器页面未就绪")
        page = session.page
        filled_title = self._fill_first(page, self._title_selectors(), title)
        filled_body = self._fill_first(page, self._body_selectors(), body)
        session.last_action_at = time.time()

        if filled_title and filled_body:
            session.status = "filled"
            session.message = "已尝试填入章节标题和正文，请在番茄后台人工确认后保存草稿。"
        else:
            session.status = "needs_manual"
            missing = []
            if not filled_title:
                missing.append("标题输入框")
            if not filled_body:
                missing.append("正文编辑器")
            # 增强诊断：dump 页面上所有候选 input/textarea/contenteditable，
            # 让用户 / 开发者一眼看到真实 DOM 特征
            try:
                diag = page.evaluate(
                    """() => {
                        const pick = (el) => {
                            if (!el) return null;
                            return {
                                tag: el.tagName,
                                type: el.type || '',
                                maxLength: el.maxLength >= 0 ? el.maxLength : -1,
                                placeholder: el.placeholder || '',
                                className: (el.className || '').toString().slice(0, 80),
                                name: el.name || '',
                                id: el.id || '',
                                ariaLabel: el.getAttribute('aria-label') || '',
                                dataPlaceholder: el.getAttribute('data-placeholder') || '',
                            };
                        };
                        const inputs = Array.from(document.querySelectorAll('input'))
                            .filter(i => i.type !== 'hidden' && i.type !== 'checkbox' && i.type !== 'radio')
                            .slice(0, 15)
                            .map(pick);
                        const tas = Array.from(document.querySelectorAll('textarea')).slice(0, 5).map(pick);
                        const edits = Array.from(document.querySelectorAll('[contenteditable=\"true\"]')).slice(0, 5)
                            .map(el => ({tag: el.tagName, className: (el.className || '').toString().slice(0, 80), dataPlaceholder: el.getAttribute('data-placeholder') || '', ariaLabel: el.getAttribute('aria-label') || ''}));
                        const buttons = Array.from(document.querySelectorAll('button'))
                            .filter(b => /存草稿|保存草稿|保存/.test(b.innerText))
                            .slice(0, 5)
                            .map(b => ({text: b.innerText.slice(0, 20), className: (b.className || '').toString().slice(0, 60)}));
                        return {url: location.href, inputs, textareas: tas, contenteditable: edits, save_buttons: buttons};
                    }"""
                )
                session.message = (
                    f"未能自动定位{ '、'.join(missing) }。\n\n"
                    f"📋 页面 DOM 诊断（番茄 URL={diag.get('url', '')[:80]}）：\n"
                    f"  • input 元素 ({len(diag['inputs'])} 个):\n"
                    + "\n".join(
                        f"      placeholder='{i['placeholder'][:30]}' maxLength={i['maxLength']} class='{i['className'][:40]}'"
                        for i in diag['inputs'][:5]
                    )
                    + f"\n  • contenteditable 元素 ({len(diag['contenteditable'])} 个):\n"
                    + "\n".join(
                        f"      class='{e['className'][:60]}' data-placeholder='{e['dataPlaceholder']}' aria-label='{e['ariaLabel']}'"
                        for e in diag['contenteditable'][:3]
                    )
                    + f"\n  • 保存按钮 ({len(diag['save_buttons'])} 个):\n"
                    + "\n".join(
                        f"      text='{b['text']}' class='{b['className'][:40]}'"
                        for b in diag['save_buttons'][:3]
                    )
                    + "\n\n💡 请把这些信息告诉开发者以更新 selector。"
                )
                # 也把诊断信息存到 session 让前端能拿到
                session.diagnose = diag
            except Exception as e:
                session.message += f"（诊断获取失败：{e}）"

        result = {
            **session.to_public(),
            "filled_title": filled_title,
            "filled_body": filled_body,
        }
        if hasattr(session, "diagnose"):
            result["diagnose"] = session.diagnose
        return result

    def try_save_draft(self, session_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        if not session.page:
            raise PublishUploaderError("浏览器页面未就绪")
        page = session.page
        clicked = self._click_first(page, self._save_button_selectors())
        session.last_action_at = time.time()
        if clicked:
            session.status = "save_clicked"
            session.message = "已尝试点击保存草稿。请在番茄后台确认是否保存成功。"
        else:
            session.status = "needs_manual"
            session.message = "未能自动定位保存草稿按钮，请在番茄后台手动保存。"
        return {**session.to_public(), "clicked": clicked}

    def _fill_first(self, page: Any, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() < 1:
                    continue
                target = locator.first
                if not target.is_visible(timeout=800):
                    continue
                try:
                    target.fill(value, timeout=1500)
                except Exception:
                    target.click(timeout=1500)
                    page.keyboard.press("Control+A")
                    page.keyboard.type(value, delay=0)
                return True
            except Exception:
                continue
        return False

    def _click_first(self, page: Any, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() < 1:
                    continue
                target = locator.first
                if not target.is_visible(timeout=800):
                    continue
                target.click(timeout=1500)
                return True
            except Exception:
                continue
        return False

    def _title_selectors(self) -> list[str]:
        # 番茄作家后台 + 其他平台的标题输入框 selector
        return [
            # 番茄后台实测："第 __ 章" 后 input + placeholder="请输入标题" + maxLength=30
            "input[maxlength='30']",
            "input[maxLength='30']",
            "input[placeholder='请输入标题']",
            "input[placeholder*='请输入标题']",
            "input[placeholder*='章节名']",
            "input[placeholder*='章节名称']",
            "input[placeholder*='章节标题']",
            "input[placeholder*='标题']",
            "textarea[placeholder*='章节名']",
            "textarea[placeholder*='标题']",
            "[contenteditable='true'][aria-label*='标题']",
        ]

    def _body_selectors(self) -> list[str]:
        return [
            # 番茄后台实测：大块 [contenteditable='true'] 区域
            "textarea[placeholder*='正文']",
            "textarea[placeholder*='内容']",
            "[contenteditable='true'][aria-label*='正文']",
            "[contenteditable='true'][data-placeholder*='正文']",
            # 富文本编辑器常见 class
            ".ProseMirror[contenteditable='true']",
            ".ql-editor[contenteditable='true']",
            ".editor-content[contenteditable='true']",
            ".write-content[contenteditable='true']",
            ".writer-content[contenteditable='true']",
            # 番茄实际 DOM 上大块的 contenteditable（不带任何 class）
            "div[contenteditable='true']",
            "section[contenteditable='true']",
            # 兜底
            "[contenteditable='true']",
        ]

    def _save_button_selectors(self) -> list[str]:
        # 番茄后台实测：右上"存草稿"按钮
        return [
            "button:has-text('存草稿')",
            "button:has-text('保存草稿')",
            "button:has-text('保存为草稿')",
            "button:has-text('保存')",
            "[aria-label*='保存草稿']",
            "[aria-label*='存草稿']",
            "[aria-label*='保存']",
            "text=存草稿",
            "text=保存草稿",
            "text=保存为草稿",
            "text=保存",
        ]
