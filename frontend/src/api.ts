import type {
  Conversation,
  HealthStatus,
  JsonRecord,
  McpTool,
  Message,
  MessageMetrics,
  ModelInfo,
  RagStatus,
  RepositoryInfo,
  Source,
  StreamEventHandlers,
  SummaryInfo,
  ToolCall,
  UsageStats,
} from './types'

const API_ROOT = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, '') ?? '/api'

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly details?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function text(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined
}

function number(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) return Number(value)
  return undefined
}

function arrayPayload(value: unknown, ...keys: string[]): unknown[] {
  if (Array.isArray(value)) return value
  if (!isRecord(value)) return []
  for (const key of keys) {
    if (Array.isArray(value[key])) return value[key]
  }
  if (isRecord(value.data)) return arrayPayload(value.data, ...keys)
  return []
}

async function responseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('json')) return response.json()
  const body = await response.text()
  if (!body) return undefined
  try {
    return JSON.parse(body)
  } catch {
    return body
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  const body = await responseBody(response)
  if (!response.ok) {
    const message = isRecord(body) ? text(body.detail) ?? text(body.message) : text(body)
    throw new ApiError(message ?? `Запрос завершился с кодом ${response.status}`, response.status, body)
  }
  return body as T
}

function normalizeConversation(value: unknown): Conversation {
  const item = isRecord(value) ? value : {}
  const settings = isRecord(item.settings) ? item.settings : {}
  return {
    id: String(item.id ?? item.conversation_id ?? ''),
    title: text(item.title) ?? text(item.name) ?? 'Новый диалог',
    model: text(item.model) ?? text(settings.model),
    contextWindow: number(item.context_window) ?? number(item.contextWindow) ?? number(settings.context_window),
    contextPreset: text(item.context_preset) ?? text(item.contextPreset) ?? text(settings.context_preset),
    customContext: text(item.custom_context) ?? text(item.customContext) ?? text(settings.custom_context),
    createdAt: text(item.created_at) ?? text(item.createdAt),
    updatedAt: text(item.updated_at) ?? text(item.updatedAt),
    raw: item,
  }
}

function normalizeSource(value: unknown): Source {
  const item = isRecord(value) ? value : {}
  const metadata = isRecord(item.metadata) ? item.metadata : {}
  return {
    path: text(item.path) ?? text(item.file_path) ?? text(item.source) ?? text(metadata.path) ?? 'Источник',
    lineStart: number(item.line_start) ?? number(item.start_line) ?? number(metadata.line_start),
    lineEnd: number(item.line_end) ?? number(item.end_line) ?? number(metadata.line_end),
    url: text(item.url) ?? text(item.github_url),
    score: number(item.score) ?? number(item.similarity),
    label: text(item.label) ?? text(item.title),
    raw: item,
  }
}

function normalizeMetrics(value: unknown): MessageMetrics {
  const item = isRecord(value) ? value : {}
  return {
    ...item,
    model: text(item.model),
    promptTokens: number(item.prompt_tokens) ?? number(item.promptTokens) ?? number(item.input_tokens),
    completionTokens: number(item.completion_tokens) ?? number(item.completionTokens) ?? number(item.output_tokens),
    totalTokens: number(item.total_tokens) ?? number(item.totalTokens),
    latencyMs: number(item.latency_ms) ?? number(item.latencyMs),
    retrievalMs: number(item.retrieval_ms) ?? number(item.retrievalMs),
    firstTokenMs: number(item.first_token_ms) ?? number(item.firstTokenMs),
    cost: number(item.cost) ?? number(item.cost_usd),
  }
}

function normalizeTool(value: unknown): ToolCall {
  const item = isRecord(value) ? value : {}
  return {
    name: text(item.name) ?? text(item.tool) ?? text(item.tool_name) ?? 'tool',
    status: text(item.status),
    durationMs: number(item.duration_ms) ?? number(item.durationMs),
    input: item.input ?? item.arguments,
    output: item.output ?? item.result,
  }
}

function normalizeMessage(value: unknown): Message {
  const item = isRecord(value) ? value : {}
  const role = item.role === 'user' || item.role === 'system' || item.role === 'tool' ? item.role : 'assistant'
  return {
    id: String(item.id ?? item.message_id ?? crypto.randomUUID()),
    role,
    content: text(item.content) ?? text(item.text) ?? text(item.message) ?? '',
    createdAt: text(item.created_at) ?? text(item.createdAt),
    sources: arrayPayload(item.sources, 'sources').map(normalizeSource),
    metrics: isRecord(item.metrics) || isRecord(item.usage) ? normalizeMetrics(item.metrics ?? item.usage) : undefined,
    tools: arrayPayload(item.tools ?? item.tool_calls, 'tools', 'tool_calls').map(normalizeTool),
    raw: item,
  }
}

function normalizeUsage(value: unknown): UsageStats {
  const item = isRecord(value) ? value : {}
  const usage = isRecord(item.usage) ? item.usage : item
  const promptTokens = number(usage.prompt_tokens) ?? number(usage.promptTokens) ?? number(usage.input_tokens) ?? 0
  const completionTokens = number(usage.completion_tokens) ?? number(usage.completionTokens) ?? number(usage.output_tokens) ?? 0
  return {
    promptTokens,
    completionTokens,
    totalTokens: number(usage.total_tokens) ?? number(usage.totalTokens) ?? promptTokens + completionTokens,
    requests: number(usage.requests) ?? number(usage.request_count) ?? number(usage.user_message_count) ?? number(usage.messages) ?? 0,
    cost: number(usage.cost) ?? number(usage.cost_usd) ?? 0,
    raw: item,
  }
}

export function aggregateUsage(items: UsageStats[]): UsageStats {
  return items.reduce<UsageStats>(
    (total, item) => ({
      promptTokens: total.promptTokens + item.promptTokens,
      completionTokens: total.completionTokens + item.completionTokens,
      totalTokens: total.totalTokens + item.totalTokens,
      requests: total.requests + item.requests,
      cost: total.cost + item.cost,
    }),
    { promptTokens: 0, completionTokens: 0, totalTokens: 0, requests: 0, cost: 0 },
  )
}

export const api = {
  async getHealth(): Promise<HealthStatus> {
    const value = await request<unknown>('/health')
    const item = isRecord(value) ? value : {}
    return {
      status: text(item.status) ?? text(item.state) ?? (item.ok === true ? 'ok' : 'unknown'),
      version: text(item.version),
      raw: item,
    }
  },

  async getModels(): Promise<ModelInfo[]> {
    const value = await request<unknown>('/models')
    return arrayPayload(value, 'models', 'data', 'items').map((entry) => {
      if (typeof entry === 'string') return { id: entry, name: entry }
      const item = isRecord(entry) ? entry : {}
      const id = text(item.id) ?? text(item.model) ?? text(item.name) ?? ''
      return { id, name: text(item.name) ?? id, description: text(item.description) }
    }).filter((model) => model.id)
  },

  async getConversations(): Promise<Conversation[]> {
    const value = await request<unknown>('/conversations')
    return arrayPayload(value, 'conversations', 'items', 'data').map(normalizeConversation).filter((item) => item.id)
  },

  async createConversation(title = 'Новый диалог'): Promise<Conversation> {
    return normalizeConversation(await request<unknown>('/conversations', { method: 'POST', body: JSON.stringify({ title }) }))
  },

  async getConversation(id: string): Promise<Conversation> {
    return normalizeConversation(await request<unknown>(`/conversations/${encodeURIComponent(id)}`))
  },

  async updateConversation(id: string, update: JsonRecord): Promise<Conversation> {
    return normalizeConversation(await request<unknown>(`/conversations/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(update),
    }))
  },

  async deleteConversation(id: string): Promise<void> {
    await request<unknown>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' })
  },

  async getMessages(id: string): Promise<Message[]> {
    const value = await request<unknown>(`/conversations/${encodeURIComponent(id)}/messages`)
    return arrayPayload(value, 'messages', 'items', 'data').map(normalizeMessage)
  },

  async getSummary(id: string): Promise<SummaryInfo> {
    const value = await request<unknown>(`/conversations/${encodeURIComponent(id)}/summary`)
    const item = isRecord(value) ? value : {}
    return {
      content: text(value) ?? text(item.summary) ?? text(item.content) ?? '',
      messageCount: number(item.message_count) ?? number(item.messageCount),
      updatedAt: text(item.updated_at) ?? text(item.updatedAt),
      raw: item,
    }
  },

  async getUsage(id: string): Promise<UsageStats> {
    return normalizeUsage(await request<unknown>(`/conversations/${encodeURIComponent(id)}/usage`))
  },

  async getRepository(): Promise<RepositoryInfo> {
    const value = await request<unknown>('/repository')
    const item = isRecord(value) ? value : {}
    const repository = isRecord(item.repository) ? item.repository : item
    return {
      name: text(repository.name),
      path: text(repository.path) ?? text(repository.root),
      branch: text(repository.branch) ?? text(repository.current_branch),
      commit: text(repository.commit) ?? text(repository.head) ?? text(repository.sha),
      remoteUrl: text(repository.remote_url) ?? text(repository.remoteUrl) ?? text(repository.url),
      dirty: typeof repository.dirty === 'boolean' ? repository.dirty : undefined,
      raw: item,
    }
  },

  async getTools(): Promise<McpTool[]> {
    const value = await request<unknown>('/mcp/tools')
    return arrayPayload(value, 'tools', 'items', 'data').map((entry) => {
      const item = isRecord(entry) ? entry : {}
      return {
        name: text(item.name) ?? 'tool',
        description: text(item.description),
        inputSchema: item.input_schema ?? item.inputSchema,
      }
    })
  },

  async getRagStatus(): Promise<RagStatus> {
    const value = await request<unknown>('/rag/status')
    const item = isRecord(value) ? value : {}
    return {
      status: text(item.status) ?? text(item.state) ?? 'unknown',
      indexedFiles: number(item.indexed_files) ?? number(item.files),
      totalChunks: number(item.total_chunks) ?? number(item.chunks),
      lastIndexedAt: text(item.last_indexed_at) ?? text(item.updated_at),
      progress: number(item.progress),
      message: text(item.message),
      raw: item,
    }
  },

  async reindex(): Promise<RagStatus> {
    const value = await request<unknown>('/rag/reindex', { method: 'POST' })
    const item = isRecord(value) ? value : {}
    return {
      status: text(item.status) ?? text(item.state) ?? 'indexing',
      indexedFiles: number(item.indexed_files),
      totalChunks: number(item.total_chunks),
      progress: number(item.progress),
      message: text(item.message),
      raw: item,
    }
  },
}

function streamData(payload: unknown, eventName: string, handlers: StreamEventHandlers): boolean {
  if (payload === '[DONE]') {
    handlers.onDone?.()
    return true
  }

  if (!isRecord(payload)) {
    if (typeof payload === 'string' && payload) handlers.onDelta(payload)
    return false
  }

  const type = text(payload.type) ?? eventName
  if (type === 'error') {
    handlers.onError?.(new Error(text(payload.message) ?? text(payload.detail) ?? 'Ошибка потока'))
    return true
  }
  if (type === 'sources' || Array.isArray(payload.sources)) {
    handlers.onSources?.(arrayPayload(payload.sources ?? payload, 'sources').map(normalizeSource))
  }
  if (type === 'metrics' || type === 'usage' || isRecord(payload.metrics) || isRecord(payload.usage)) {
    handlers.onMetrics?.(normalizeMetrics(payload.metrics ?? payload.usage ?? payload))
  }
  if (type.startsWith('tool') || isRecord(payload.tool)) {
    handlers.onTool?.(normalizeTool(payload.tool ?? payload))
  }

  const choices = arrayPayload(payload.choices, 'choices')
  const choice = isRecord(choices[0]) ? choices[0] : {}
  const choiceDelta = isRecord(choice.delta) ? choice.delta : {}
  const delta = text(payload.delta) ?? text(payload.token) ?? (type === 'message' ? text(payload.content) : undefined) ?? text(choiceDelta.content)
  if (delta) handlers.onDelta(delta)

  if (type === 'done' || type === 'complete' || payload.done === true) {
    handlers.onDone?.(isRecord(payload.message) ? normalizeMessage(payload.message) : undefined)
    return true
  }
  return false
}

export async function streamChat(
  conversationId: string,
  message: string,
  handlers: StreamEventHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_ROOT}/conversations/${encodeURIComponent(conversationId)}/chat`, {
    method: 'POST',
    headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
    signal,
  })
  if (!response.ok) {
    const body = await responseBody(response)
    const reason = isRecord(body) ? text(body.detail) ?? text(body.message) : text(body)
    throw new ApiError(reason ?? `Не удалось отправить сообщение (${response.status})`, response.status, body)
  }
  if (!response.body) throw new ApiError('Сервер вернул пустой поток', response.status)

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finished = false

  const processBlock = (block: string) => {
    let eventName = 'message'
    const data: string[] = []
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith('event:')) eventName = line.slice(6).trim()
      if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
    }
    if (!data.length) return
    const raw = data.join('\n')
    let payload: unknown = raw
    try {
      payload = JSON.parse(raw)
    } catch {
      // Plain text data is a valid SSE payload.
    }
    finished = streamData(payload, eventName, handlers) || finished
  }

  while (!finished) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const blocks = buffer.split(/\r?\n\r?\n/)
    buffer = blocks.pop() ?? ''
    blocks.forEach(processBlock)
    if (done) break
  }
  if (buffer.trim()) processBlock(buffer)
  if (!finished) handlers.onDone?.()
}
