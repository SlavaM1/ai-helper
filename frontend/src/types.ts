export type JsonRecord = Record<string, unknown>

export interface Conversation {
  id: string
  title: string
  model?: string
  contextWindow?: number
  contextPreset?: string
  customContext?: string
  createdAt?: string
  updatedAt?: string
  raw?: JsonRecord
}

export interface ModelInfo {
  id: string
  name: string
  description?: string
}

export interface HealthStatus {
  status: string
  version?: string
  raw?: JsonRecord
}

export interface Source {
  path: string
  lineStart?: number
  lineEnd?: number
  url?: string
  score?: number
  label?: string
  raw?: JsonRecord
}

export interface ToolCall {
  name: string
  status?: string
  durationMs?: number
  input?: unknown
  output?: unknown
}

export interface MessageMetrics {
  model?: string
  promptTokens?: number
  completionTokens?: number
  totalTokens?: number
  latencyMs?: number
  retrievalMs?: number
  firstTokenMs?: number
  cost?: number
  [key: string]: unknown
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  createdAt?: string
  sources?: Source[]
  metrics?: MessageMetrics
  tools?: ToolCall[]
  pending?: boolean
  failed?: boolean
  raw?: JsonRecord
}

export interface RepositoryInfo {
  name?: string
  path?: string
  branch?: string
  commit?: string
  remoteUrl?: string
  dirty?: boolean
  raw?: JsonRecord
}

export interface RagStatus {
  status: string
  indexedFiles?: number
  totalChunks?: number
  lastIndexedAt?: string
  progress?: number
  message?: string
  raw?: JsonRecord
}

export interface McpTool {
  name: string
  description?: string
  inputSchema?: unknown
}

export interface UsageStats {
  promptTokens: number
  completionTokens: number
  totalTokens: number
  requests: number
  cost: number
  raw?: JsonRecord
}

export interface SummaryInfo {
  content: string
  messageCount?: number
  updatedAt?: string
  raw?: JsonRecord
}

export type ContextPreset = '8192' | '16384' | '32768' | '65536' | 'custom'

export interface StreamEventHandlers {
  onDelta: (delta: string) => void
  onSources?: (sources: Source[]) => void
  onMetrics?: (metrics: MessageMetrics) => void
  onTool?: (tool: ToolCall) => void
  onDone?: (message?: Message) => void
  onError?: (error: Error) => void
}
