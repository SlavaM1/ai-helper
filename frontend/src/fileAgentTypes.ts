export interface FileChange {
  path: string
  status: string
  additions: number
  deletions: number
}

export interface FileOperation {
  id: number
  timestamp: string
  tool?: string
  phase: string
  status: string
  arguments?: { path?: string; paths?: string[]; iteration?: number }
  result?: unknown
  error?: string | null
  elapsed_ms?: number
}

export interface FileAgentTask {
  id: string
  parent_id: string | null
  prompt: string
  model: string
  status: string
  base_sha: string
  workspace: {
    id?: string
    base_sha?: string
    created_at?: string
    state?: string
    artifact?: { sha256: string; [key: string]: unknown }
  }
  operations: FileOperation[]
  read_files: string[]
  changes: FileChange[]
  usage: {
    input_tokens?: number
    output_tokens?: number
    total_tokens?: number
    llm_calls?: number
    context_bytes?: number
    estimated?: boolean
  }
  validation: { ok?: boolean; complete?: boolean; limitations?: string; [key: string]: unknown }
  parameters: Record<string, unknown>
  result: unknown
  error: string | null
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
  can_apply: boolean
  source_apply_enabled: boolean
  correctness_guaranteed: false
  patch_export_notice: string
}

export interface FileDiff {
  diff: string
  changes: FileChange[]
  bytes: number
  truncated: boolean
  redacted: boolean
}

export interface FileSource {
  path: string
  content: string
  sha256: string
  total_lines: number
  start_line: number
  end_line: number
  truncated: boolean
  redacted: boolean
}
