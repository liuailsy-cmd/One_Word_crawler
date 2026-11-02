# -*- coding: utf-8 -*-
"""动态网页渲染与人工接管（Playwright）。

职责（对应设计文档 5.5 / 技术选型）：
1. headless 渲染 JavaScript 动态页面；
2. 人工接管：打开可见浏览器，供用户登录或完成验证码（不做任何验证码破解）；
3. 会话保存在持久化用户目录中，人工完成后复用同一个已授权浏览器会话
   输出页面快照继续采集，绝不假设独立的 HTTP 请求自动拥有登录态。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

PROFILES_DIR = Path(__file__).resolve().parent.parent / "storage" / ".browser_profiles"

# Chromium 及其驱动会在系统临时目录创建运行产物；为让环境完全自包含
# （并规避个别机器上系统 Temp 目录的权限问题），统一使用项目内 storage/.tmp。
_PROJECT_TMP = Path(__file__).resolve().parent.parent / "storage" / ".tmp"
_PROJECT_TMP.mkdir(parents=True, exist_ok=True)
os.environ["TMP"] = str(_PROJECT_TMP)
os.environ["TEMP"] = str(_PROJECT_TMP)

# 若未显式指定浏览器路径，且项目目录内已有本地安装的 Chromium（.venv/ms-playwright），
# 则自动使用它 —— 保证环境随项目目录走、可移植。
_DEFAULT_BROWSERS = Path(__file__).resolve().parent.parent / ".venv" / "ms-playwright"
if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and _DEFAULT_BROWSERS.exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_DEFAULT_BROWSERS)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class BrowserUnavailableError(RuntimeError):
    """Playwright 或 Chromium 未安装。"""


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _pw():
    """延迟导入 playwright，未安装时给出清晰提示。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BrowserUnavailableError(
            "未安装 playwright，请执行：pip install playwright && playwright install chromium"
        ) from e
    return sync_playwright()


def render_html(url: str, timeout_ms: int = 30000, wait_ms: int = 2500) -> str:
    """用无头 Chromium 渲染 JavaScript 页面，返回渲染后的 HTML。"""
    with _pw() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            raise BrowserUnavailableError(
                f"Chromium 启动失败：{e}。请执行 playwright install chromium"
            ) from e
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(wait_ms)
            return page.content()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# 人工接管会话（可见浏览器 + 持久化登录态）
# ---------------------------------------------------------------------------

class UserSession:
    """一个任务对应一个可见浏览器会话，登录态持久化到本地用户目录。"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.profile_dir = PROFILES_DIR / task_id
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = _pw()
        self._playwright = self._pw.__enter__()  # 上下文管理器进入后才是 Playwright 实例
        self.context = self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=False,
            user_agent=USER_AGENT,
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def open(self, url: str) -> str:
        """导航到目标网址（浏览器已被用户关闭时重新开一个页面）。"""
        if self.page.is_closed():
            self.page = self.context.new_page()
        self.page.goto(url, timeout=45000, wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)
        return self.page.url

    def snapshot(self) -> dict[str, Any]:
        """返回当前会话页面快照（使用已授权会话，登录态在其中）。"""
        return {
            "url": self.page.url,
            "title": self.page.title(),
            "html": self.page.content(),
        }

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._pw.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


_sessions: dict[str, UserSession] = {}
_lock = threading.Lock()


def open_for_user(task_id: str, url: str) -> str:
    """为任务打开可见浏览器，返回提示信息。"""
    with _lock:
        session = _sessions.get(task_id)
        if session is None:
            session = UserSession(task_id)
            _sessions[task_id] = session
    try:
        current = session.open(url)
        return f"已在浏览器中打开 {current}，请完成登录或验证后点击“继续采集”"
    except Exception:
        with _lock:
            _sessions.pop(task_id, None)
        session.close()
        raise


def snapshot_for_task(task_id: str) -> dict[str, Any] | None:
    """获取任务已授权会话的当前页面快照；无会话或失败返回 None。"""
    with _lock:
        session = _sessions.get(task_id)
    if session is None:
        return None
    try:
        return session.snapshot()
    except Exception:  # noqa: BLE001
        return None


def close_session(task_id: str) -> None:
    """关闭并清理任务的人工会话。"""
    with _lock:
        session = _sessions.pop(task_id, None)
    if session is not None:
        session.close()


def close_all() -> None:
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for s in sessions:
        s.close()
