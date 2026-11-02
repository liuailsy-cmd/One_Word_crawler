import { useCallback, useEffect, useRef, useState } from 'react'
import { api, Task, ResultPreview, ScriptInfo } from './api'

// ---------------- 阶段 / 状态展示映射 ----------------
const STAGE_LABELS: Record<string, string> = {
  creating: '创建任务',
  analyzing: '分析网页',
  generating: '生成爬虫',
  scraping: '执行采集',
  repairing: '自动修复',
  waiting_manual: '等待人工操作',
  completed: '已完成',
  failed: '失败',
}

const STATUS_META: Record<Task['status'], { label: string; color: string }> = {
  running: { label: '运行中', color: '#2563eb' },
  waiting_for_user: { label: '等待人工操作', color: '#d97706' },
  completed: { label: '完成', color: '#16a34a' },
  failed: { label: '失败', color: '#dc2626' },
}

const ALL_STAGES = ['analyzing', 'generating', 'scraping', 'repairing', 'waiting_manual']

const POLL_INTERVAL = 2000 // 设计文档 7：前端每 2 秒轮询一次

export default function App() {
  const [url, setUrl] = useState('')
  const [requirement, setRequirement] = useState('')
  const [maxItems, setMaxItems] = useState(100)
  const [creating, setCreating] = useState(false)
  const [formError, setFormError] = useState('')

  const [tasks, setTasks] = useState<Task[]>([])
  const [task, setTask] = useState<Task | null>(null)
  const [result, setResult] = useState<ResultPreview | null>(null)
  const [scriptInfo, setScriptInfo] = useState<ScriptInfo | null>(null)
  const [scriptOpen, setScriptOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [actionMsg, setActionMsg] = useState('')

  const selectedIdRef = useRef<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const list = await api.listTasks()
      setTasks(list)
      const id = selectedIdRef.current ?? list[0]?.id ?? null
      if (!id) return
      const [t, r] = await Promise.all([api.getTask(id), api.getResult(id)])
      setTask(t)
      setResult(r)
    } catch (e) {
      console.error('轮询失败：', e)
    }
  }, [])

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, POLL_INTERVAL)
    return () => clearInterval(timer)
  }, [refresh])

  const selectTask = (id: string) => {
    selectedIdRef.current = id
    setScriptOpen(false)
    setScriptInfo(null)
    setActionMsg('')
    refresh()
  }

  // ---------------- A. 创建任务 ----------------
  const handleCreate = async () => {
    setFormError('')
    if (!/^https?:\/\//i.test(url.trim())) {
      setFormError('请输入以 http:// 或 https:// 开头的网址')
      return
    }
    if (!requirement.trim()) {
      setFormError('请填写数据需求')
      return
    }
    setCreating(true)
    try {
      const t = await api.createTask(url.trim(), requirement.trim(), maxItems)
      selectedIdRef.current = t.id
      setScriptOpen(false)
      setScriptInfo(null)
      setActionMsg('')
      await refresh()
    } catch (e) {
      setFormError(e instanceof Error ? e.message : String(e))
    } finally {
      setCreating(false)
    }
  }

  // ---------------- C. 结果操作 ----------------
  const handleOpenSite = async () => {
    if (!task) return
    setBusy(true)
    setActionMsg('')
    try {
      const r = await api.openSite(task.id)
      setActionMsg(r.message)
    } catch (e) {
      setActionMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const handleResume = async () => {
    if (!task) return
    setBusy(true)
    setActionMsg('')
    try {
      await api.resume(task.id)
      setActionMsg('已提交继续采集请求，正在检查网页是否恢复正常…')
    } catch (e) {
      setActionMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const handleToggleScript = async () => {
    if (!task) return
    if (scriptOpen) {
      setScriptOpen(false)
      return
    }
    try {
      const info = await api.getScript(task.id)
      setScriptInfo(info)
      setScriptOpen(true)
    } catch (e) {
      setActionMsg(e instanceof Error ? e.message : String(e))
    }
  }

  const stage = task ? task.stage : ''
  const statusMeta = task ? STATUS_META[task.status] : null
  const isWaiting = task?.status === 'waiting_for_user'

  return (
    <div className="page">
      <style>{css}</style>
      <header className="header">
        <h1>🤖 AI 自动爬虫 Demo</h1>
        <p className="subtitle">输入任务 → 分析网页 → AI 生成代码 → 执行采集 → 检查结果 → 失败时自动修复</p>
      </header>

      {/* A. 创建任务 */}
      <section className="card">
        <h2>① 创建任务</h2>
        <div className="form-row">
          <label>网页 URL</label>
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://books.toscrape.com/"
          />
        </div>
        <div className="form-row">
          <label>数据需求</label>
          <input
            value={requirement}
            onChange={(e) => setRequirement(e.target.value)}
            placeholder="例如：提取商品名称、价格和链接"
          />
        </div>
        <div className="form-row">
          <label>最大采集条数</label>
          <input
            type="number"
            min={1}
            max={1000}
            value={maxItems}
            onChange={(e) => setMaxItems(Number(e.target.value) || 100)}
            style={{ width: 120 }}
          />
          <button className="primary" onClick={handleCreate} disabled={creating}>
            {creating ? '创建中…' : '开始爬取'}
          </button>
        </div>
        {formError && <div className="error">{formError}</div>}
      </section>

      {/* B. 任务运行状态 */}
      <section className="card">
        <h2>② 任务运行状态</h2>
        {!task ? (
          <p className="muted">暂无任务，请先创建。</p>
        ) : (
          <>
            <div className="task-head">
              <span className="task-id">{task.id}</span>
              {statusMeta && (
                <span className="badge" style={{ background: statusMeta.color }}>
                  {statusMeta.label}
                </span>
              )}
              <span className="muted">已采集 {task.items_count} 条</span>
            </div>
            <div className="stages">
              {ALL_STAGES.map((s) => (
                <span
                  key={s}
                  className={
                    'stage-chip ' +
                    (stage === s ? 'active' : stage === 'completed' || stage === 'failed' ? 'done' : '')
                  }
                >
                  {STAGE_LABELS[s]}
                </span>
              ))}
            </div>
            <div className="message">
              <strong>当前阶段：{STAGE_LABELS[stage] ?? stage}</strong>
              <span> —— {task.message || '…'}</span>
            </div>
            {task.error && <div className="error">失败原因：{task.error}</div>}
            <div className="history">
              {[...(task.history ?? [])].slice(-6).map((h, i) => (
                <div key={i} className="history-line">
                  <span className="muted">{h.time}</span> {h.message}
                </div>
              ))}
            </div>
            {actionMsg && <div className="notice">{actionMsg}</div>}
          </>
        )}
      </section>

      {/* C. 结果展示 */}
      <section className="card">
        <h2>③ 结果展示</h2>
        {isWaiting && task && (
          <div className="waiting-box">
            <p>⚠️ 需要人工操作：请在浏览器中完成登录或验证码，然后继续采集。</p>
            <div className="btn-row">
              <button className="primary" onClick={handleOpenSite} disabled={busy}>
                打开网站
              </button>
              <button className="secondary" onClick={handleResume} disabled={busy}>
                继续采集
              </button>
            </div>
          </div>
        )}
        {result && (result.columns.length > 0 || result.total > 0) ? (
          <>
            <div className="result-meta">
              <span>共 {result.total} 条数据（预览前 {result.rows.length} 条）</span>
              <span className="btn-row">
                <a className="button-link" href={api.downloadUrl(task!.id)} download>
                  下载 CSV
                </a>
                <button className="secondary" onClick={handleToggleScript}>
                  {scriptOpen ? '收起脚本' : '查看生成的 Python 脚本'}
                </button>
              </span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    {result.columns.map((c, i) => (
                      <th key={i}>{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.map((row, i) => (
                    <tr key={i}>
                      {result.columns.map((_, j) => (
                        <td key={j}>{row[j] ?? ''}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <p className="muted">暂无采集结果。</p>
        )}
        {scriptOpen && (
          <div className="script-box">
            {scriptInfo?.script ? (
              <pre>{scriptInfo.script}</pre>
            ) : (
              <p className="muted">{scriptInfo?.note ?? '暂无脚本'}</p>
            )}
          </div>
        )}
      </section>

      {/* 历史任务 */}
      <section className="card">
        <h2>历史任务</h2>
        {tasks.length === 0 ? (
          <p className="muted">暂无历史任务。</p>
        ) : (
          <ul className="task-list">
            {tasks.map((t) => (
              <li
                key={t.id}
                className={t.id === task?.id ? 'selected' : ''}
                onClick={() => selectTask(t.id)}
              >
                <span className="task-id">{t.id}</span>
                <span className="badge" style={{ background: STATUS_META[t.status].color }}>
                  {STATUS_META[t.status].label}
                </span>
                <span className="ellipsis">{t.url}</span>
                <span className="muted">{t.items_count} 条</span>
                <span className="muted">{t.created_at}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

const css = `
* { box-sizing: border-box; }
body { margin: 0; background: #f1f5f9; font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; color: #1e293b; }
.page { max-width: 1100px; margin: 0 auto; padding: 24px 16px 60px; }
.header { margin-bottom: 20px; }
.header h1 { margin: 0 0 6px; }
.subtitle { margin: 0; color: #64748b; font-size: 14px; }
.card { background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px 20px; margin-bottom: 16px; }
.card h2 { margin: 0 0 14px; font-size: 17px; }
.form-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.form-row label { width: 110px; color: #475569; font-size: 14px; }
.form-row input { flex: 1; min-width: 260px; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 14px; }
button, .button-link { display: inline-block; border: none; border-radius: 6px; padding: 8px 16px; font-size: 14px; cursor: pointer; text-decoration: none; }
button.primary, .button-link { background: #2563eb; color: #fff; }
button.primary:disabled { background: #93c5fd; cursor: not-allowed; }
button.secondary { background: #e2e8f0; color: #1e293b; }
button.secondary:disabled { opacity: 0.5; cursor: not-allowed; }
.task-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.task-id { font-family: Consolas, monospace; background: #eff6ff; color: #1d4ed8; padding: 2px 8px; border-radius: 4px; font-size: 13px; }
.badge { color: #fff; padding: 2px 10px; border-radius: 999px; font-size: 12px; }
.stages { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
.stage-chip { padding: 4px 10px; border-radius: 999px; background: #f1f5f9; color: #94a3b8; font-size: 12px; }
.stage-chip.active { background: #dbeafe; color: #1d4ed8; font-weight: 600; }
.stage-chip.done { background: #dcfce7; color: #15803d; }
.message { font-size: 14px; margin-bottom: 10px; }
.error { color: #dc2626; background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 8px 12px; font-size: 13px; margin: 8px 0; }
.notice { color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 8px 12px; font-size: 13px; margin: 8px 0; }
.history { background: #f8fafc; border-radius: 6px; padding: 8px 12px; font-size: 13px; }
.history-line { padding: 2px 0; color: #334155; }
.muted { color: #94a3b8; font-size: 13px; }
.waiting-box { background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 12px 14px; margin-bottom: 12px; }
.waiting-box p { margin: 0 0 10px; color: #92400e; font-size: 14px; }
.btn-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.result-meta { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; font-size: 14px; flex-wrap: wrap; gap: 8px; }
.table-wrap { overflow-x: auto; border: 1px solid #e2e8f0; border-radius: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { border-bottom: 1px solid #e2e8f0; padding: 8px 10px; text-align: left; max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
th { background: #f8fafc; }
.script-box { margin-top: 14px; }
.script-box pre { background: #0f172a; color: #e2e8f0; border-radius: 8px; padding: 14px; overflow-x: auto; font-size: 12px; max-height: 420px; }
.task-list { list-style: none; margin: 0; padding: 0; }
.task-list li { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-bottom: 1px solid #f1f5f9; cursor: pointer; font-size: 13px; }
.task-list li:hover, .task-list li.selected { background: #f8fafc; }
.ellipsis { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #334155; }
`
