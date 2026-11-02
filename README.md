# 🤖 AI 自动爬虫 Demo

本地运行的 AI 爬虫演示程序：输入网页 URL 和数据需求，系统自动分析网页、**由 AI 生成专用 Python 爬虫**、在受限子进程中执行采集、检查结果，失败时自动修复（最多 3 次）；遇到登录、验证码等需要人工处理的情况，**打开可见浏览器供用户操作**，完成后用同一已授权会话继续采集。

**核心流程：**

```
输入任务 → 分析网页 → AI 生成代码 → 执行爬虫 → 检查结果 → 失败时修复 → 保存脚本和数据
```

技术栈：React + Vite（前端）、Python + FastAPI（后端）、HTTPX + BeautifulSoup（静态解析）、Playwright（动态网页 / 人工接管）、OpenAI 兼容 API（AI）、本地文件夹 + JSON（存储）、CSV / JSON（结果）。

## 目录结构

```
.
├── frontend/                 # React + Vite 单页前端
│   ├── src/
│   │   ├── App.tsx           # 唯一页面：创建任务 / 运行状态 / 结果展示
│   │   ├── api.ts            # 后端接口封装
│   │   └── main.tsx
│   ├── index.html
│   ├── package.json
│   ├── tsconfig.json
│   └── vite.config.ts        # 开发代理 /api → http://127.0.0.1:8000
│
├── backend/
│   ├── main.py               # API 接口和任务流程编排
│   ├── scraper.py            # HTTPX 抓取 + BeautifulSoup 页面分析 + 静态兜底提取 + CSV
│   ├── ai.py                 # AI 生成/修复脚本 + 语法与安全约束检查
│   └── browser.py            # Playwright 动态渲染 + 人工接管（登录态会话复用）
│
├── storage/                  # 本地存储（不使用数据库）
│   ├── tasks/                # 任务 JSON（task_001.json …）
│   ├── scripts/              # AI 生成的爬虫脚本（task_001.py …）
│   └── results/              # 采集结果（task_001.csv …）
│
├── .env.example              # AI 配置模板
├── requirements.txt          # 后端依赖
└── README.md
```

## 环境要求

- Python 3.9+
- Node.js 18+（前端）
- 推荐安装 Chromium 内核（用于动态网页与人工接管）

## 快速开始

### 1. 启动后端

```bash
# （可选）创建虚拟环境
python -m venv .venv
.venv\Scripts\activate        # Windows

# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器内核（动态网页 / 人工接管需要）
# 默认装到 %LOCALAPPDATA%\ms-playwright。
# 推荐随项目目录保存（环境自包含、可整体拷贝迁移）：
#   PowerShell:  $env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.venv\ms-playwright"
#   然后执行:     playwright install chromium
playwright install chromium

# 配置 AI
copy .env.example .env        # 然后编辑 .env，填入 OPENAI_API_KEY

# 启动（项目根目录执行）
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

> 后端会自动识别项目目录内 `.venv/ms-playwright` 中的 Chromium（无需每次手动设置环境变量）；若未在项目内安装，则使用系统默认位置。

后端接口：<http://127.0.0.1:8000>（`/docs` 可查看 Swagger 文档，`/api/health` 检查 AI 与 Playwright 是否就绪）。

### 2. 启动前端

```bash
cd frontend
npm install
npm run dev
```

浏览器打开 <http://localhost:5173>，前端每 2 秒轮询一次任务状态（无需 WebSocket）。

> 也可以构建后由后端直接托管：`npm run build`，然后直接访问 <http://127.0.0.1:8000>（无需启动 Vite）。

## AI 配置（OpenAI 兼容接口）

复制 `.env.example` 为 `.env` 并填写：

```ini
OPENAI_API_KEY=sk-你的密钥
OPENAI_BASE_URL=https://api.openai.com/v1    # 或 DeepSeek: https://api.deepseek.com/v1
OPENAI_MODEL=gpt-4o-mini
```

- 配置了密钥：走完整 AI 流程（AI 生成 → 执行 → 失败自动修复）。
- 未配置密钥：自动退化为内置静态提取器，仍采集**真实页面数据**（表格 / 列表块 / 链接），用于无网络或无密钥环境下的流程演示。
- 使用推理模型（如 `deepseek-flash` / `deepseek-reasoner`）时，系统会为其思维链自动预留输出预算，输出被截断（`finish_reason=length`）时自动扩容重试；AI 接口异常时任务自动降级为内置静态提取器，不会整单失败。
- 若页面主体在 iframe 中（外壳页无内容），后端会自动用 Playwright 渲染 iframe 内页并合并分析，AI 生成脚本时会知晓该结构。

## 使用演示

1. 输入一个公开商品列表网页，例如 `https://books.toscrape.com/`；
2. 数据需求填写「提取商品名称、价格和链接」；
3. 最大采集条数保持默认 100，点击「**开始爬取**」；
4. 前端显示执行阶段：分析网页 → 生成爬虫 → 执行采集 →（失败时自动修复）；
5. 完成后结果区展示真实采集数据表格，可「下载 CSV」或「查看生成的 Python 脚本」；
6. 本地 `storage/scripts/` 保存了脚本、`storage/results/` 保存了 CSV——**重新运行保存的脚本可以再次采集**：
   ```bash
   python storage/scripts/task_001.py <url> <输出csv> <最大条数>
   ```
7. 遇到登录 / 验证码页面时，任务进入「等待人工操作」，自动打开可见浏览器（也可点「打开网站」按钮），人工完成登录后点击「**继续采集**」，后端检查网页恢复后使用**同一个已授权浏览器会话**的页面快照继续采集。

> 不要求所有网站都能成功。对需要登录、验证码、授权或受限访问的网站，系统会清晰提示并支持人工接管；**不做任何验证码破解或访问控制绕过**。

## API 一览

| 接口                            | 功能                    |
| ------------------------------- | ----------------------- |
| `POST /api/tasks`               | 创建并启动爬虫任务      |
| `GET  /api/tasks`               | 获取任务列表            |
| `GET  /api/tasks/{id}`          | 获取任务状态            |
| `GET  /api/tasks/{id}/result`   | 获取结果预览            |
| `GET  /api/tasks/{id}/script`   | 查看生成的爬虫脚本      |
| `GET  /api/tasks/{id}/download` | 下载 CSV                |
| `POST /api/tasks/{id}/open-site`| 打开网站（人工接管）    |
| `POST /api/tasks/{id}/resume`   | 人工处理后继续任务      |
| `GET  /api/health`              | 健康检查（AI/Playwright 就绪状态） |

## 任务状态与阶段

状态只有四种：`running`（运行中）、`waiting_for_user`（等待人工操作）、`completed`（完成）、`failed`（失败）。

阶段作为任务 JSON 中的普通字段保存：`creating`、`analyzing`（分析网页）、`generating`（生成爬虫）、`scraping`（执行采集）、`repairing`（自动修复）、`waiting_manual`（等待人工操作）、`completed`、`failed`。

## 生成脚本的安全约束

AI 生成的代码在执行前会经过以下检查（**禁止未经检查直接执行模型生成的代码**）：

1. **语法检查**：`ast.parse` 校验；
2. **安全约束**：禁止导入 `subprocess / os / socket / shutil / importlib / ctypes` 等危险模块，禁止 `eval / exec / __import__` 等调用，限制代码长度；
3. **受限子进程执行**：独立工作目录、超时 120 秒、剥离密钥类环境变量（最小环境）；
4. 脚本契约固定：`python script.py <url> <output_csv> <max_items> [--html <本地快照>]`，stdout 只输出一行结果 JSON，退出码 `0=成功 / 2=提取失败 / 3=需要登录或验证`。

> 说明：这是演示级的安全约束。生成脚本在正式环境中仍建议放入容器 / 虚拟机运行。

## 自动修复

脚本执行出现选择器错误、空结果或格式错误时，后端把错误信息反馈给 AI 重新生成脚本再运行，**最多自动修复 3 次**，达到上限后停止并显示失败原因（脚本、错误信息均可在任务 JSON 与前端界面查看）。

## 常见问题

- **`pip install playwright` 后仍报浏览器缺失**：执行 `playwright install chromium`。
- **动态页面任务直接失败并提示安装 Playwright**：按提示安装后重试即可。
- **中文乱码**：CSV 使用 utf-8-sig 编码，Excel 直接双击打开即可；结果预览通过后端接口读取。
- **端口占用**：后端 `--port 8001` 启动后，需同步修改 `frontend/vite.config.ts` 中的代理地址。
