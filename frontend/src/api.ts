// 与后端 FastAPI 交互的接口封装（设计文档 6：最小 API）
// 开发模式下 Vite 把 /api 代理到 http://127.0.0.1:8000

export interface TaskHistoryItem {
  time: string
  stage: string
  message: string
}

export interface Task {
  id: string
  url: string
  requirement: string
  max_items: number
  status: 'running' | 'waiting_for_user' | 'completed' | 'failed'
  stage: string
  message: string
  items_count: number
  attempts: number
  error: string | null
  script_path: string | null
  result_path: string | null
  analysis: Record<string, unknown> | null
  browser_open: boolean
  history: TaskHistoryItem[]
  created_at: string
  updated_at: string
}

export interface ResultPreview {
  columns: string[]
  rows: string[][]
  total: number
  path: string | null
}

export interface ScriptInfo {
  script: string | null
  note?: string
  path?: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`
    try {
      const data = await resp.json()
      if (data && data.detail) detail = String(data.detail)
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new Error(detail)
  }
  return (await resp.json()) as T
}

export const api = {
  createTask: (url: string, requirement: string, max_items: number) =>
    request<Task>('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({ url, requirement, max_items }),
    }),
  listTasks: () => request<Task[]>('/api/tasks'),
  getTask: (id: string) => request<Task>(`/api/tasks/${id}`),
  getResult: (id: string) => request<ResultPreview>(`/api/tasks/${id}/result`),
  getScript: (id: string) => request<ScriptInfo>(`/api/tasks/${id}/script`),
  downloadUrl: (id: string) => `/api/tasks/${id}/download`,
  openSite: (id: string) =>
    request<{ ok: boolean; message: string }>(`/api/tasks/${id}/open-site`, { method: 'POST' }),
  resume: (id: string) => request<Task>(`/api/tasks/${id}/resume`, { method: 'POST' }),
}
