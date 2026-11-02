# -*- coding: utf-8 -*-
"""FastAPI 入口：HTTP 接口与爬虫任务流程。

运行方式（在项目根目录执行）:
    uvicorn backend.main:app --host 127.0.0.1 --port 8000

任务流程（对应设计文档 5.x）:
    分析网页 -> AI 生成脚本 -> 受限子进程执行 -> 检查结果 -> 失败时自动修复(最多 3 次)
    遇到登录/验证码 -> 打开可见浏览器 -> 人工操作 -> 检查恢复 -> 用同一授权会话继续采集

所有数据直接保存在本地 storage/ 目录（tasks/JSON、scripts/Python、results/CSV），不使用数据库。
"""

from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai  # noqa: E402
import browser  # noqa: E402
import scraper  # noqa: E402

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

try:  # 加载项目根目录 .env（可选）
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # noqa: BLE001
    pass

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_DIR = PROJECT_ROOT / "storage"
TASKS_DIR = STORAGE_DIR / "tasks"
SCRIPTS_DIR = STORAGE_DIR / "scripts"
RESULTS_DIR = STORAGE_DIR / "results"

MAX_REPAIRS = 3                # 设计文档 5.4：最多自动修复 3 次
MAX_RUN_SECONDS = 120          # 单次脚本执行超时
WAIT_USER_SECONDS = 30 * 60    # 人工操作最长等待 30 分钟
MAX_LOGIN_CYCLES = 3           # 人工接管最多循环次数

# 受限子进程的最小环境变量（不含任何密钥类变量）
_ALLOWED_ENV = {
    "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT", "COMSPEC",
    "HOMEDRIVE", "HOMEPATH", "USERPROFILE", "ALLUSERSPROFILE", "APPDATA",
    "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "NUMBER_OF_PROCESSORS",
    "PLAYWRIGHT_BROWSERS_PATH",  # 项目内 Chromium 路径（动态页面脚本需要）
}

for d in (STORAGE_DIR, TASKS_DIR, SCRIPTS_DIR, RESULTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="AI 自动爬虫 Demo")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本地演示：允许 Vite dev server 等本地来源
    allow_methods=["*"],
    allow_headers=["*"],
)

atexit.register(browser.close_all)


# ---------------------------------------------------------------------------
# 任务文件读写
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_user_events: dict[str, threading.Event] = {}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _task_file(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def _next_task_id() -> str:
    with _lock:
        nums = [
            int(m.group(1))
            for f in TASKS_DIR.glob("task_*.json")
            if (m := re.match(r"task_(\d+)\.json", f.name))
        ]
        return f"task_{max(nums, default=0) + 1:03d}"


def _load_task(task_id: str) -> dict:
    f = _task_file(task_id)
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return json.loads(f.read_text(encoding="utf-8"))


def _save_task(task: dict) -> None:
    task["updated_at"] = _now()
    with _lock:
        _task_file(task["id"]).write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _log(task: dict, stage: str, message: str) -> None:
    """记录运行信息到任务历史，并更新当前阶段。"""
    task.setdefault("history", []).append({"time": _now(), "stage": stage, "message": message})
    if len(task["history"]) > 50:
        task["history"] = task["history"][-50:]
    task["stage"] = stage
    task["message"] = message


# ---------------------------------------------------------------------------
# 受限子进程执行（设计文档 5.3）
# ---------------------------------------------------------------------------

def _run_script(task: dict, snapshot: str | None = None) -> dict:
    """在受限子进程中执行生成的脚本：最小环境变量 + 独立工作目录 + 超时。"""
    script_path = SCRIPTS_DIR / f'{task["id"]}.py'
    csv_path = RESULTS_DIR / f'{task["id"]}.csv'
    cmd = [sys.executable, str(script_path), task["url"], str(csv_path), str(task["max_items"])]
    if snapshot and Path(snapshot).exists():
        cmd += ["--html", str(snapshot)]

    env = {k: v for k, v in os.environ.items() if k in _ALLOWED_ENV}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(RESULTS_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=MAX_RUN_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "执行超时（超过 120 秒）"}
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-4000:],
    }


def _save_script(task_id: str, code: str) -> Path:
    path = SCRIPTS_DIR / f"{task_id}.py"
    path.write_text(code, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 人工接管（设计文档 5.5）
# ---------------------------------------------------------------------------

_LOGIN_HINT = ("login", "登录", "验证码", "captcha", "recaptcha", "hcaptcha")


def _wait_for_user(task: dict, reason: str) -> str | None:
    """暂停任务，打开可见浏览器等待人工操作。

    返回恢复后的页面快照文件路径；超时或失败返回 None（任务已置为 failed）。
    """
    browser_opened = False
    while True:
        event = threading.Event()
        with _lock:
            _user_events[task["id"]] = event
        task["status"] = "waiting_for_user"
        task["stage"] = "waiting_manual"
        task["message"] = reason
        _save_task(task)

        if not browser_opened:
            try:
                note = browser.open_for_user(task["id"], task["url"])
                task["browser_open"] = True
                task["message"] = reason + "。 " + note
                _save_task(task)
                browser_opened = True
            except Exception as e:  # noqa: BLE001
                task["browser_open"] = False
                task["message"] = (
                    f"{reason}（自动打开浏览器失败：{e}，请点击“打开网站”手动打开）"
                )
                _save_task(task)

        event.wait(WAIT_USER_SECONDS)
        if not event.is_set():
            # 等待超时
            task["status"] = "failed"
            task["stage"] = "failed"
            task["error"] = "等待人工操作超时（30 分钟），任务已终止"
            _log(task, "failed", task["error"])
            _save_task(task)
            browser.close_session(task["id"])
            with _lock:
                _user_events.pop(task["id"], None)
            return None

        # 用户点击了“继续采集”：检查网页是否恢复正常（设计文档 5.5 第 5 步）
        snap = browser.snapshot_for_task(task["id"])
        if snap is None:
            _log(task, "waiting_manual", "未检测到浏览器页面，请点击“打开网站”重新打开后继续")
            _save_task(task)
            continue
        html = snap["html"] or ""
        if scraper.looks_like_login(html) or scraper.looks_like_captcha(html):
            _log(task, "waiting_manual", "网页尚未恢复正常（仍有登录/验证），请完成后再次点击“继续采集”")
            _save_task(task)
            continue

        # 网页已恢复：保存授权会话的页面快照，用同一会话继续采集
        snapshot_path = RESULTS_DIR / f'{task["id"]}_snapshot.html'
        snapshot_path.write_text(html, encoding="utf-8")
        browser.close_session(task["id"])
        with _lock:
            _user_events.pop(task["id"], None)
        task["status"] = "running"
        _log(task, "analyzing", "人工操作完成，网页已恢复，使用已授权会话继续采集")
        _save_task(task)
        return str(snapshot_path)


# ---------------------------------------------------------------------------
# 任务主流程
# ---------------------------------------------------------------------------

def _finish_generic(task: dict, snapshot: str | None, csv_path: Path) -> None:
    """用内置静态提取器完成采集（AI 未配置或调用失败时的降级，数据仍来自真实页面）。

    优先使用已渲染/已授权的页面快照，否则重新抓取。
    """
    try:
        html = (
            Path(snapshot).read_text(encoding="utf-8")
            if snapshot and Path(snapshot).exists()
            else scraper.fetch_html(task["url"])[0]
        )
    except Exception as e:  # noqa: BLE001
        task["status"] = "failed"
        task["stage"] = "failed"
        task["error"] = f"内置提取器获取页面失败：{e}"
        _log(task, "failed", task["error"])
        _save_task(task)
        return
    rows = scraper.extract_generic(html, task["requirement"], task["max_items"])
    count = scraper.write_csv(rows, csv_path)
    if count == 0:
        task["status"] = "failed"
        task["stage"] = "failed"
        task["error"] = "AI 与内置提取器都未能从页面提取到数据"
        _log(task, "failed", task["error"])
        _save_task(task)
        return
    task["status"] = "completed"
    task["stage"] = "completed"
    task["items_count"] = count
    task["result_path"] = str(csv_path.relative_to(PROJECT_ROOT))
    _log(task, "completed", f"采集完成，共 {count} 条数据（内置静态提取器）")
    _save_task(task)


def _run_task(task_id: str) -> None:
    task = _load_task(task_id)
    task["status"] = "running"
    snapshot: str | None = None
    last_error = ""

    try:
        # ---- 1. 分析网页（设计文档 5.2） ----
        _log(task, "analyzing", "正在获取并分析网页…")
        _save_task(task)
        analysis = scraper.analyze_page(task["url"])
        task["analysis"] = {
            k: analysis.get(k)
            for k in ("title", "final_url", "text_length", "login_page", "captcha", "is_dynamic")
        }
        _save_task(task)

        if analysis.get("login_page") or analysis.get("captcha"):
            _log(task, "analyzing", "检测到登录页/验证码，需要人工操作")
            _save_task(task)
            snapshot = _wait_for_user(
                task, "检测到登录页或验证码，请在弹出的浏览器中完成登录/验证，然后点击“继续采集”"
            )
            if snapshot is None:
                return
            html = Path(snapshot).read_text(encoding="utf-8")
            analysis = scraper.analyze_html(html, task["url"])
            analysis["render_required"] = False
        elif analysis.get("is_dynamic"):
            _log(task, "analyzing", "页面可能由 JavaScript 渲染，切换到 Playwright 获取内容…")
            _save_task(task)
            if not browser.playwright_available():
                raise RuntimeError(
                    "目标页面疑似由 JavaScript 动态渲染，但未安装 Playwright。"
                    "请执行：pip install playwright && playwright install chromium 后重试"
                )
            html = browser.render_html(task["url"])
            wrapper = scraper.analyze_html(html, task["url"])
            merged = html
            iframe_srcs = wrapper.get("iframes") or []
            # 页面是 iframe 壳（主体内容在 iframe 中）：渲染内页并合并，供分析与兜底提取使用
            if wrapper.get("uses_iframe") or (wrapper["text_length"] < 200 and iframe_srcs):
                _log(task, "analyzing", f"页面主体在 iframe 中（{len(iframe_srcs)} 个），渲染内页内容…")
                _save_task(task)
                for src in iframe_srcs[:2]:
                    try:
                        inner = browser.render_html(src)
                        if scraper.analyze_html(inner, src)["text_length"] > 300:
                            merged = html + f"\n<!-- IFRAME CONTENT src={src} -->\n" + inner
                            break
                    except Exception as e:  # noqa: BLE001
                        _log(task, "analyzing", f"iframe {src} 渲染失败：{e}")
            snapshot_path = RESULTS_DIR / f'{task["id"]}_snapshot.html'
            snapshot_path.write_text(merged, encoding="utf-8")
            analysis = scraper.analyze_html(merged, task["url"])
            analysis["render_required"] = True  # 生成的脚本需用 Playwright 渲染
            analysis["iframe_srcs"] = iframe_srcs  # 供 AI 知晓内容在 iframe 中
        else:
            analysis["render_required"] = False

        csv_path = RESULTS_DIR / f'{task["id"]}.csv'

        # ---- 2. 生成爬虫（设计文档 5.3） ----
        if not ai.ai_available():
            # AI 未配置：退化为内置静态提取器（数据仍全部来自真实页面）
            _log(task, "generating", "AI 未配置（缺少 OPENAI_API_KEY），使用内置静态提取器")
            _save_task(task)
            _finish_generic(task, snapshot, csv_path)
            return

        _log(task, "generating", "AI 正在生成专用爬虫脚本…")
        _save_task(task)
        try:
            code = ai.generate_script(task["requirement"], task["url"], task["max_items"], analysis)
        except ai.AIError as e:
            # AI 生成失败（接口异常/空回复等）：降级为内置静态提取器，任务不直接失败
            _log(task, "generating", f"AI 生成失败（{e}），降级为内置静态提取器")
            _save_task(task)
            _finish_generic(task, snapshot, csv_path)
            return
        _save_script(task["id"], code)
        task["script_path"] = str(
            (SCRIPTS_DIR / f'{task["id"]}.py').relative_to(PROJECT_ROOT)
        )
        _save_task(task)

        # ---- 3/4. 执行 + 自动修复循环（设计文档 5.4） ----
        attempt = 0
        login_cycles = 0
        last_run: dict | None = None
        while attempt <= MAX_REPAIRS:
            last_run = None
            ok, why = ai.check_code_safety(code)
            if not ok:
                last_error = f"代码安全检查未通过：{why}"
            else:
                _log(task, "scraping", f"正在执行采集脚本（第 {attempt + 1} 次运行）…")
                _save_task(task)
                run = _run_script(task, snapshot)
                last_run = run
                count = scraper.count_csv(csv_path)
                task["items_count"] = count
                _save_task(task)

                # 脚本报告需要登录/验证：进入人工接管
                if run["exit_code"] == 3 or any(
                    k in (run["stdout"] + " " + run["stderr"]).lower() for k in _LOGIN_HINT
                ):
                    login_cycles += 1
                    if login_cycles > MAX_LOGIN_CYCLES:
                        last_error = "多次人工处理后仍要求登录/验证，已停止任务"
                        break
                    _log(task, "waiting_manual", "采集过程中遇到登录页/验证码，需要人工操作")
                    _save_task(task)
                    snapshot = _wait_for_user(
                        task, "采集过程中遇到登录或验证码，请在浏览器中完成操作后点击“继续采集”"
                    )
                    if snapshot is None:
                        return
                    continue  # 人工接管不消耗自动修复次数

                if run["ok"] and count > 0:
                    # 成功
                    task["status"] = "completed"
                    task["stage"] = "completed"
                    task["items_count"] = count
                    task["attempts"] = attempt
                    task["result_path"] = str(csv_path.relative_to(PROJECT_ROOT))
                    _log(task, "completed", f"采集完成，共 {count} 条数据")
                    _save_task(task)
                    browser.close_session(task["id"])
                    return

                if run["ok"] and count == 0:
                    last_error = "脚本成功退出，但未提取到任何数据（空结果）"
                else:
                    last_error = (
                        f"退出码 {run['exit_code']}；"
                        f"stderr：{(run['stderr'] or '')[-800:] or '（空）'}；"
                        f"stdout：{(run['stdout'] or '')[-300:] or '（空）'}"
                    )

            attempt += 1
            if attempt > MAX_REPAIRS:
                break
            _log(task, "repairing", f"第 {attempt} 次自动修复：AI 根据错误重新生成脚本…")
            _save_task(task)
            err_info = (
                {
                    "exit_code": last_run.get("exit_code", "?"),
                    "stdout": last_run.get("stdout", ""),
                    "stderr": (last_run.get("stderr", "") or last_error),
                }
                if last_run
                else {"exit_code": "?", "stdout": "", "stderr": last_error}
            )
            try:
                code = ai.repair_script(
                    task["requirement"], task["url"], task["max_items"],
                    analysis, code, err_info, attempt,
                )
            except ai.AIError as e:
                # AI 修复接口异常：降级为内置静态提取器
                _log(task, "repairing", f"AI 修复失败（{e}），降级为内置静态提取器")
                _save_task(task)
                _finish_generic(task, snapshot, csv_path)
                return
            _save_script(task["id"], code)
            _save_task(task)

        # ---- 失败收尾 ----
        task["status"] = "failed"
        task["stage"] = "failed"
        task["error"] = last_error or "未知错误"
        task["attempts"] = min(attempt, MAX_REPAIRS + 1)
        _log(task, "failed", f"自动修复 {MAX_REPAIRS} 次后仍失败，任务终止")
        _save_task(task)
        browser.close_session(task["id"])

    except ai.AIError as e:
        task["status"] = "failed"
        task["stage"] = "failed"
        task["error"] = str(e)
        _log(task, "failed", f"AI 调用失败：{e}")
        _save_task(task)
        browser.close_session(task["id"])
    except Exception as e:  # noqa: BLE001
        task["status"] = "failed"
        task["stage"] = "failed"
        task["error"] = str(e)
        _log(task, "failed", f"任务失败：{e}")
        _save_task(task)
        browser.close_session(task["id"])


# ---------------------------------------------------------------------------
# API（设计文档 6）
# ---------------------------------------------------------------------------

class TaskRequest(BaseModel):
    url: str
    requirement: str = Field(..., min_length=1)
    max_items: int = Field(100, ge=1, le=1000)


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "ai_available": ai.ai_available(),
        "playwright_available": browser.playwright_available(),
    }


@app.post("/api/tasks")
def create_task(req: TaskRequest) -> dict:
    """创建并启动爬虫任务。"""
    url = req.url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise HTTPException(status_code=400, detail="URL 必须以 http:// 或 https:// 开头")
    requirement = req.requirement.strip()
    if not requirement:
        raise HTTPException(status_code=400, detail="数据需求不能为空")

    task_id = _next_task_id()
    task = {
        "id": task_id,
        "url": url,
        "requirement": requirement,
        "max_items": req.max_items,
        "status": "running",
        "stage": "creating",
        "message": "任务已创建",
        "items_count": 0,
        "attempts": 0,
        "error": None,
        "script_path": None,
        "result_path": None,
        "analysis": None,
        "browser_open": False,
        "history": [{"time": _now(), "stage": "creating", "message": "任务已创建"}],
        "created_at": _now(),
        "updated_at": _now(),
    }
    _save_task(task)
    thread = threading.Thread(target=_run_task, args=(task_id,), daemon=True)
    with _lock:
        _threads[task_id] = thread
    thread.start()
    return task


@app.get("/api/tasks")
def list_tasks() -> list[dict]:
    """任务列表（扫描本地 storage/tasks 文件夹）。"""
    tasks = []
    for f in TASKS_DIR.glob("task_*.json"):
        try:
            tasks.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return tasks


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    """任务状态。"""
    return _load_task(task_id)


@app.get("/api/tasks/{task_id}/result")
def get_result(task_id: str, limit: int = 100) -> dict:
    """结果预览（CSV 前 limit 行）。"""
    _load_task(task_id)
    path = RESULTS_DIR / f"{task_id}.csv"
    if not path.exists():
        return {"columns": [], "rows": [], "total": 0, "path": None}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            import csv

            reader = csv.reader(f)
            header = next(reader, [])
            rows = []
            for r in reader:
                rows.append(r)
                if len(rows) >= max(1, min(limit, 1000)):
                    break
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"读取结果失败：{e}")
    return {
        "columns": header,
        "rows": rows,
        "total": scraper.count_csv(str(path)),
        "path": str(path.relative_to(PROJECT_ROOT)),
    }


@app.get("/api/tasks/{task_id}/script")
def get_script(task_id: str) -> dict:
    """查看生成的爬虫脚本。"""
    _load_task(task_id)
    path = SCRIPTS_DIR / f"{task_id}.py"
    if not path.exists():
        return {"script": None, "note": "该任务没有生成脚本（可能使用了内置静态提取器）"}
    return {
        "script": path.read_text(encoding="utf-8"),
        "path": str(path.relative_to(PROJECT_ROOT)),
    }


@app.get("/api/tasks/{task_id}/download")
def download_result(task_id: str) -> FileResponse:
    """下载 CSV 结果。"""
    _load_task(task_id)
    path = RESULTS_DIR / f"{task_id}.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="结果文件不存在")
    return FileResponse(str(path), media_type="text/csv", filename=f"{task_id}.csv")


@app.post("/api/tasks/{task_id}/open-site")
def open_site(task_id: str) -> dict:
    """为人工接管打开可见浏览器（设计文档 5.5 第 2 步）。"""
    task = _load_task(task_id)
    try:
        msg = browser.open_for_user(task_id, task["url"])
    except browser.BrowserUnavailableError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打开浏览器失败：{e}")
    return {"ok": True, "message": msg}


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str) -> dict:
    """人工处理完成后继续任务（设计文档 5.5 第 4/5 步）。

    后端会检查网页是否恢复正常，未恢复则继续保持等待状态。
    """
    task = _load_task(task_id)
    if task["status"] != "waiting_for_user":
        raise HTTPException(status_code=400, detail="任务当前不在等待人工操作状态")
    with _lock:
        event = _user_events.get(task_id)
    if event is None:
        raise HTTPException(status_code=409, detail="任务等待事件不存在，请重新创建任务")
    event.set()
    return _load_task(task_id)


# ---------------------------------------------------------------------------
# 可选：构建后的前端由后端直接托管（frontend/dist 存在时）
# ---------------------------------------------------------------------------

dist_dir = PROJECT_ROOT / "frontend" / "dist"
if dist_dir.exists():
    app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="frontend")
