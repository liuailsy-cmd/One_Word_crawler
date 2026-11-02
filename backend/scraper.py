# -*- coding: utf-8 -*-
"""网页抓取与静态分析。

职责（对应设计文档 5.2）：
1. 用 HTTPX 获取网页 HTML；
2. 用 BeautifulSoup 提取标题、正文、链接和相关结构，供 AI 生成爬虫；
3. 判断页面是否由 JavaScript 动态渲染（需要切换 Playwright）；
4. 判断页面是否为登录页 / 验证码页（触发人工接管）；
5. 提供 AI 未配置时的内置静态兜底提取器（只提取真实网页数据，非假数据）；
6. CSV 写出工具（结果格式之一）。
"""

from __future__ import annotations

import csv
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, UnicodeDammit

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------- 登录 / 验证码识别关键字 ----------
LOGIN_URL_HINTS = ("/login", "/signin", "/sign-in", "/sign_in", "/account/auth", "/user/login")
LOGIN_TEXT_HINTS = (
    "登录", "登陆", "会员登录", "账号登录", "帐号登录", "密码登录",
    "sign in", "log in", "member login", "please login",
)
CAPTCHA_MARKERS = ("g-recaptcha", "hcaptcha", "recaptcha", "geetest", "captcha", "cf-chl")
CAPTCHA_TEXT_HINTS = ("验证码", "人机验证", "机器人验证", "拖动滑块", "安全验证", "请完成验证")


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def fetch_html(url: str, timeout: float = 20.0) -> tuple[str, str]:
    """用 HTTPX 获取网页，返回 (html 文本, 最终跳转后的 URL)。

    失败时抛出 httpx.HTTPError，由调用方转换为任务失败原因。
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        resp = client.get(url)
        resp.raise_for_status()
        html = _decode(resp.content)
        return html, str(resp.url)


def _decode(content: bytes) -> str:
    """尽可能正确地解码网页（自动探测编码，兼容中文站点）。"""
    try:
        return UnicodeDammit(content).unicode_markup or content.decode("utf-8", errors="replace")
    except Exception:
        return content.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 页面分析
# ---------------------------------------------------------------------------

def analyze_html(html: str, url: str) -> dict[str, Any]:
    """分析 HTML 结构，返回结构化信息供 AI 参考。

    返回字段：url / title / meta_description / headings / links / paragraphs_sample /
    tables / lists / forms / input_types / text_length / script_count / images /
    login_page / captcha / is_dynamic
    """
    soup = BeautifulSoup(html, "lxml")

    title = soup.title.get_text(strip=True) if soup.title else ""
    meta_desc = ""
    md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if md and md.get("content"):
        meta_desc = md["content"][:300]

    headings = [h.get_text(" ", strip=True)[:100] for h in soup.find_all(["h1", "h2", "h3"])][:30]

    links: list[dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if not href.startswith(("http://", "https://")):
            continue
        text = a.get_text(" ", strip=True)
        if text:
            links.append({"text": text[:80], "href": href[:300]})
        if len(links) >= 40:
            break

    text = soup.get_text(" ", strip=True)
    paragraphs_sample = re.sub(r"\s+", " ", text)[:1500]

    tables: list[dict[str, Any]] = []
    for t in soup.find_all("table")[:1]:
        headers: list[str] = []
        first_tr = t.find("tr")
        if first_tr:
            headers = [c.get_text(" ", strip=True) for c in first_tr.find_all(["th", "td"])]
        sample_rows = [
            [c.get_text(" ", strip=True)[:60] for c in tr.find_all(["th", "td"])]
            for tr in t.find_all("tr")[1:3]
        ]
        if headers or sample_rows:
            tables.append({"headers": headers, "sample_rows": sample_rows})

    lists = [
        [li.get_text(" ", strip=True)[:80] for li in ul.find_all("li")[:10]]
        for ul in soup.find_all(["ul", "ol"])[:3]
    ]

    forms: list[dict[str, Any]] = []
    input_types: list[str] = []
    for f in soup.find_all("form")[:2]:
        types = [i.get("type", "text") for i in f.find_all("input")]
        forms.append({"action": f.get("action"), "method": f.get("method"), "input_types": types})
        input_types.extend(types)

    images = [
        (img.get("src") or img.get("data-src") or "")[:300]
        for img in soup.find_all("img")[:10]
    ]

    iframes = [
        urljoin(url, f.get("src") or "")[:300]
        for f in soup.find_all("iframe")
        if f.get("src")
    ][:5]

    info = {
        "url": url,
        "title": title,
        "meta_description": meta_desc,
        "headings": headings,
        "links": links,
        "paragraphs_sample": paragraphs_sample,
        "tables": tables,
        "lists": lists,
        "forms": forms,
        "input_types": input_types,
        "text_length": len(text),
        "script_count": len(soup.find_all("script")),
        "images": images,
        "iframes": iframes,
        "uses_iframe": bool(iframes) and len(text) < 500,
    }
    info["login_page"] = looks_like_login(html)
    info["captcha"] = looks_like_captcha(html)
    info["is_dynamic"] = _guess_dynamic(soup, len(text), info["script_count"])
    return info


def analyze_page(url: str) -> dict[str, Any]:
    """抓取并分析一个 URL（静态路径）。"""
    html, final_url = fetch_html(url)
    info = analyze_html(html, final_url)
    info["final_url"] = final_url
    return info


def _guess_dynamic(soup: BeautifulSoup, text_len: int, script_count: int) -> bool:
    """粗略判断主要内容是否由 JavaScript 渲染。"""
    app_root = bool(
        soup.find(id=re.compile(r"^(root|app)$", re.I))
        or soup.find("div", id="app")
        or soup.find(id="__next")
    )
    if app_root:
        return script_count > 0 and text_len < 2000
    return text_len < 300 and script_count > 0


def looks_like_login(html: str) -> bool:
    """页面是否看起来是登录页（有密码输入框 + 登录关键词）。"""
    soup = BeautifulSoup(html, "lxml")
    has_password = bool(soup.find("input", {"type": "password"}))
    if not has_password:
        return False
    text = soup.get_text(" ", strip=True).lower()
    return any(k in text for k in LOGIN_TEXT_HINTS)


def looks_like_captcha(html: str) -> bool:
    """页面是否包含验证码 / 人机验证。"""
    lower = html.lower()
    if any(m in lower for m in CAPTCHA_MARKERS):
        return True
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True).lower()
    return any(k in text for k in CAPTCHA_TEXT_HINTS)


# ---------------------------------------------------------------------------
# 内置静态兜底提取器（AI 未配置时使用，数据全部来自真实页面）
# ---------------------------------------------------------------------------

def extract_generic(html: str, requirement: str, max_items: int = 100) -> list[dict[str, str]]:
    """不依赖 AI 的通用提取：优先表格 → 重复块 → 链接文本。

    只返回页面里真实存在的内容，绝不编造数据。
    """
    soup = BeautifulSoup(html, "lxml")

    # 1) 页面上有规整表格时，直接按表头输出
    table = soup.find("table")
    if table:
        first_row = table.find("tr")
        headers = (
            [c.get_text(" ", strip=True) for c in first_row.find_all(["th", "td"])]
            if first_row else []
        )
        if headers:
            rows: list[dict[str, str]] = []
            for tr in table.find_all("tr")[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if not cells:
                    continue
                row = {
                    (headers[i] if i < len(headers) else f"列{i + 1}"):
                        (cells[i] if i < len(cells) else "")
                    for i in range(len(headers))
                }
                rows.append(row)
                if len(rows) >= max_items:
                    break
            if rows:
                return rows

    # 2) 重复块（商品 / 列表项 / 卡片）
    blocks = soup.select(
        "li, article, tr, [class*='item'], [class*='product'], [class*='goods'], [class*='card']"
    )
    seen: set[str] = set()
    rows = []
    for b in blocks:
        text = re.sub(r"\s+", " ", b.get_text(" ", strip=True)).strip()
        if len(text) < 2 or text in seen:
            continue
        seen.add(text)
        link = b.find("a", href=True)
        href = urljoin(b.get("baseurl", ""), link["href"]) if link is not None else ""
        rows.append({"内容": text[:200], "链接": href[:300]})
        if len(rows) >= max_items:
            break
    if rows:
        return rows

    # 3) 兜底：链接文本
    links: list[dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = urljoin("", a["href"])
        if text and href.startswith(("http://", "https://", "/")):
            links.append({"标题": text[:120], "链接": href[:300]})
        if len(links) >= max_items:
            break
    return links


# ---------------------------------------------------------------------------
# CSV 写出
# ---------------------------------------------------------------------------

def write_csv(rows: list[dict[str, Any]], path: str) -> int:
    """把行列表写入 CSV（utf-8-sig，兼容 Excel 中文），返回行数。"""
    path = str(path)
    if not rows:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            f.write("")
        return 0
    headers = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: str(v) for k, v in row.items()})
    return len(rows)


def count_csv(path: str) -> int:
    """统计 CSV 数据行数（不含表头）。"""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return max(0, sum(1 for _ in csv.reader(f)) - 1)
    except Exception:
        return 0
