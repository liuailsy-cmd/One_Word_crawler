# -*- coding: utf-8 -*-
"""AI 生成与修复爬虫脚本（OpenAI 兼容接口）。

职责（对应设计文档 5.3 / 5.4）：
1. 根据网页结构和用户需求生成完整专用 Python 爬虫脚本；
2. 执行失败时根据错误信息修复脚本（由主流程控制最多 3 次）；
3. 脚本执行前做语法与安全约束检查，禁止未经检查直接执行模型生成的代码。
"""

from __future__ import annotations

import ast
import json
import os
import re
from typing import Any

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def ai_available() -> bool:
    """是否配置了 AI（OPENAI_API_KEY 非空且不是占位符）。

    未配置时主流程退化为内置静态提取器。
    """
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        return False
    placeholders = ("sk-你的密钥", "sk-你的key", "sk-xxxxxxxx", "sk-your-key", "your-key")
    return key.lower() not in {p.lower() for p in placeholders}


def _client():
    from openai import OpenAI
    return OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
    )


def _model() -> str:
    return os.getenv("OPENAI_MODEL") or "gpt-4o-mini"


class AIError(RuntimeError):
    """AI 调用失败。"""


# ---------------------------------------------------------------------------
# 系统提示词：脚本命令行契约
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一名资深的网页爬虫工程师。根据给定的网页结构分析和用户需求，生成一个完整、可直接运行的 Python 爬虫脚本。

【脚本命令行契约】（必须严格遵守）
用法：python script.py <url> <output_csv> <max_items> [--html <本地html路径>]
- 正常模式：用 httpx 或 requests 请求网页，BeautifulSoup（lxml 解析器）解析 HTML。
- --html 模式：从指定的本地 HTML 文件读取页面内容再解析（这是人工登录后的页面快照），此模式下禁止发起任何网络请求。
- 提取用户需要的字段，把结果写入 CSV 文件（路径必须是 output_csv 参数的值），第一行为表头，使用 utf-8-sig 编码，用 csv 模块写入。
- 需要翻页时自动翻页（分析“下一页”链接或分页参数），直到达到 max_items 条或没有更多数据为止。
- 每条数据都必须来自真实网页内容，禁止编造、伪造数据。
- 进度信息用 print(..., file=sys.stderr) 输出。
- 结束时向 stdout 输出一行 JSON：{"count": <实际条数>, "fields": [<字段名列表>]}。stdout 除这一行外不得输出任何内容。
- 退出码约定：0=成功；2=提取失败（选择器错误、无数据、异常等）；3=页面需要登录或人工验证（确认无法继续时）。
- 整体运行不超过 100 秒；每个请求设置超时并最多重试 2 次。
- 使用真实浏览器 User-Agent；请求间隔 0.5~1 秒，礼貌采集。
- 对可能变化的选择器做降级处理：优先精确选择器，失败后回退到宽松选择器。

【允许使用的库】httpx, requests, bs4(BeautifulSoup), csv, json, re, sys, time, urllib, html, pathlib, dataclasses, typing, random, math
【动态页面】如果页面分析信息中 render_required 为 true，则使用 playwright（from playwright.sync_api import sync_playwright，headless=True 模式）渲染页面后再解析，并保持上面的输出契约。
【iframe 页面】如果页面分析信息中 iframes 非空（页面主体在 iframe 里）：
- 使用 playwright 时：先 page.goto 主站，再遍历 page.frames，选择文本内容最多的那个 frame 来解析；
- 使用 httpx 时：直接请求 iframes 里给出的 src 地址再解析。
【严格禁止】
- 禁止导入 subprocess、os、socket、shutil、importlib、ctypes、pickle 等危险模块；
- 禁止 eval/exec/__import__/compile；
- 除 output_csv 与 --html 指定的文件外，禁止读写任何本地文件；
- 禁止打印约定 JSON 之外的任何内容到 stdout。

只输出 Python 代码本身，代码放在一个 ```python 代码块中，不要输出任何其他解释。"""


# ---------------------------------------------------------------------------
# 生成 / 修复
# ---------------------------------------------------------------------------

# 输出预算上限。推理模型（如 deepseek-flash）会先输出思维链，需要预留足够空间；
# 触发 finish_reason=length 时会自动扩容重试。
MAX_TOKENS = 8000
MAX_TOKEN_CAP = 32000


def generate_script(requirement: str, url: str, max_items: int, analysis: dict[str, Any]) -> str:
    """根据网页分析与需求生成完整爬虫脚本。失败时抛出 AIError。"""
    user = _build_generation_prompt(requirement, url, max_items, analysis)
    return _generate_with_retry(SYSTEM_PROMPT, user)


def repair_script(
    requirement: str,
    url: str,
    max_items: int,
    analysis: dict[str, Any],
    previous_code: str,
    error_info: dict[str, Any],
    attempt: int,
) -> str:
    """根据执行错误修复脚本，返回完整的新脚本。失败时抛出 AIError。"""
    user = (
        f"上一版爬虫脚本执行失败，请修复。这是第 {attempt} 次修复。\n\n"
        f"【目标网址】{url}\n"
        f"【数据需求】{requirement}\n"
        f"【最大采集条数】{max_items}\n"
        f"【网页结构分析】\n{_json_analysis(analysis)}\n\n"
        f"【上一版脚本】\n```python\n{previous_code}\n```\n\n"
        f"【执行错误信息】\n退出码：{error_info.get('exit_code')}\n"
        f"stdout 尾部：\n{error_info.get('stdout', '')}\n"
        f"stderr 尾部：\n{error_info.get('stderr', '')}\n\n"
        f"请分析失败原因（选择器变化、结构误判、翻页逻辑、编码问题等），"
        f"输出修复后的完整 Python 脚本，保持命令行契约不变，"
        f"代码放在一个 ```python 代码块中，不要输出其他解释。"
    )
    return _generate_with_retry(SYSTEM_PROMPT, user)


def _generate_with_retry(system: str, user: str) -> str:
    """调用 AI 并提取代码。

    针对三种常见异常自动处理：
    1. 推理模型思维链耗尽输出预算（finish_reason=length，content 为空）→ 扩容重试；
    2. 偶发空回复 → 原样重试；
    3. 代码写在 reasoning_content 里 → 从中提取。
    失败时抛出的 AIError 携带 finish_reason / 模型 / 思考 token 数等诊断信息。
    """
    max_tokens = MAX_TOKENS
    for attempt in range(1, 4):
        info = _chat(system, user, max_tokens=max_tokens)

        if info["content"]:
            code = _extract_code(info["content"])
            if code:
                return code
            # 有内容但没代码块：要求只输出代码后重试一次
            if attempt == 1:
                user = user + "\n\n（注意：只输出一个 ```python 代码块，不要输出任何其他内容。）"
                continue
            raise AIError(f"AI 输出中未找到 Python 代码（模型 {info['model']}，finish_reason={info['finish_reason']}）")

        # 内容为空
        if info["finish_reason"] == "length":
            if max_tokens < MAX_TOKEN_CAP:
                max_tokens *= 2  # 思维链耗尽预算，扩容重试
                continue
            raise AIError(
                f"AI 返回内容为空：思维链耗尽了输出预算（reasoning_tokens={info['reasoning_tokens']}，"
                f"max_tokens={max_tokens}，finish_reason=length，模型 {info['model']}）"
            )
        if info["reasoning"]:
            code = _extract_code(info["reasoning"])
            if code:
                return code  # 模型把代码写进了思维链
        if attempt == 1:
            continue  # 偶发空回复，原样重试一次
        raise AIError(
            f"AI 返回内容为空（finish_reason={info['finish_reason']}，模型 {info['model']}，已重试 {attempt} 次）"
        )
    raise AIError("AI 返回内容为空（多次重试后仍无有效内容）")


def _build_generation_prompt(
    requirement: str, url: str, max_items: int, analysis: dict[str, Any]
) -> str:
    return (
        f"【目标网址】{url}\n"
        f"【数据需求】{requirement}\n"
        f"【最大采集条数】{max_items}\n"
        f"【网页结构分析】\n{_json_analysis(analysis)}\n\n"
        f"请生成满足需求的爬虫脚本。"
    )


def _json_analysis(analysis: dict[str, Any]) -> str:
    try:
        text = json.dumps(analysis, ensure_ascii=False, indent=2)
    except Exception:
        text = str(analysis)
    return text[:6000]


def _chat(system: str, user: str, max_tokens: int = MAX_TOKENS) -> dict[str, Any]:
    """调用一次 AI，返回结构化信息（content / reasoning / finish_reason / 模型 / 用量）。"""
    try:
        client = _client()
        resp = client.chat.completions.create(
            model=_model(),
            temperature=0.2,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception as e:  # noqa: BLE001
        raise AIError(f"AI 接口调用失败：{e}") from e

    choice = resp.choices[0] if getattr(resp, "choices", None) else None
    if choice is None:
        raise AIError("AI 返回的响应中没有 choices")
    msg = choice.message

    reasoning_tokens = 0
    try:
        details = getattr(getattr(resp, "usage", None), "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None) or 0
    except Exception:  # noqa: BLE001
        pass

    return {
        "content": (msg.content or "").strip(),
        "reasoning": (getattr(msg, "reasoning_content", None) or "").strip(),
        "finish_reason": getattr(choice, "finish_reason", None),
        "model": getattr(resp, "model", None) or _model(),
        "reasoning_tokens": reasoning_tokens,
    }


def _extract_code(text: str) -> str:
    """从模型输出中提取 Python 代码块。"""
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("python"):
            text = text[6:].strip()
        return text
    return text


# ---------------------------------------------------------------------------
# 语法与安全约束检查（设计文档 5.3：禁止未经检查直接执行）
# ---------------------------------------------------------------------------

BANNED_IMPORTS = {
    "subprocess", "socket", "shutil", "importlib", "ctypes", "pickle",
    "os", "builtins", "multiprocessing", "pty", "functools", "signal",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "input", "globals", "locals", "vars"}
MAX_CODE_LEN = 20000


def check_code_safety(code: str) -> tuple[bool, str]:
    """检查生成代码的语法与安全约束。

    返回 (是否通过, 说明)。通过说明为 "ok"。
    """
    code = _extract_code(code)
    if not code:
        return False, "未能从 AI 输出中提取到代码"
    if len(code) > MAX_CODE_LEN:
        return False, f"代码过长（{len(code)} 字符，超过 {MAX_CODE_LEN} 上限）"

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误：{e.msg}（第 {e.lineno} 行）"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_IMPORTS:
                    return False, f"禁止导入模块：{alias.name}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_IMPORTS:
                return False, f"禁止导入模块：{node.module}"
        elif isinstance(node, ast.Call):
            # 只禁止裸名调用（如 compile()/eval()）；re.compile 等模块方法调用放行
            if isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
                return False, f"禁止调用：{node.func.id}()"
        elif isinstance(node, ast.Attribute):
            if node.attr in {"system", "popen", "remove", "unlink", "rmdir", "chmod"}:
                root = _attr_root_name(node)
                if root in BANNED_IMPORTS:
                    return False, f"禁止调用：{root}.{node.attr}"
    return True, "ok"


def _attr_root_name(node: ast.Attribute) -> str:
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    return cur.id if isinstance(cur, ast.Name) else ""
