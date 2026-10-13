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

export type ReviewStatus = 'pending' | 'running' | 'completed' | 'failed' | string

export interface ReviewSummary {
  id: string
  pullRequestNumber: number
  status: ReviewStatus
  headSha?: string
  summary?: string
  findingsCount?: number
  severityCounts?: Record<string, number>
  model?: string
  elapsedMs?: number
  createdAt?: string
  updatedAt?: string
  completedAt?: string
  raw?: JsonRecord
}

export interface PullRequest {
  number: number
  title: string
  author: string
  headBranch: string
  baseBranch: string
  state: string
  updatedAt?: string
  htmlUrl?: string
  headSha?: string
  reviewStatus?: ReviewStatus
  lastReview?: ReviewSummary
  raw?: JsonRecord
}

export interface ReviewFinding {
  id: string
  title: string
  description: string
  severity: string
  category: string
  filePath?: string
  lineStart?: number
  lineEnd?: number
  suggestion?: string
  impact?: string
  evidence?: string
  permalink?: string
  codeSnippet?: string
  raw?: JsonRecord
}

export interface CodeReview extends ReviewSummary {
  findings: ReviewFinding[]
  sources: Source[]
}

export interface ReviewSyncResult {
  synced: number | boolean
  message?: string
  raw?: JsonRecord
}

export interface ReviewDiscussion {
  conversationId: string
  reviewId: string
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
