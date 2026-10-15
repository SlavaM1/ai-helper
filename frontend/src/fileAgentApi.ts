import { request } from './api'
import type { FileAgentTask, FileDiff, FileSource } from './fileAgentTypes'

const root = '/file-agent/tasks'
const taskPath = (id: string) => `${root}/${encodeURIComponent(id)}`
const post = (body: unknown, signal?: AbortSignal): RequestInit => ({ method: 'POST', body: JSON.stringify(body), signal })

export const fileAgentApi = {
  history: (signal?: AbortSignal) => request<FileAgentTask[]>(`${root}?limit=100`, { signal }),
  task: (id: string, signal?: AbortSignal) => request<FileAgentTask>(taskPath(id), { signal }),
  create: (prompt: string, model?: string, signal?: AbortSignal) => request<FileAgentTask>(root, post({ prompt, ...(model ? { model } : {}) }, signal)),
  diff: (id: string, path?: string, signal?: AbortSignal) => request<FileDiff>(`${taskPath(id)}/diff${path ? `?path=${encodeURIComponent(path)}` : ''}`, { signal }),
  file: (id: string, path: string, signal?: AbortSignal) => request<FileSource>(`${taskPath(id)}/file?path=${encodeURIComponent(path)}`, { signal }),
  rerun: (id: string, signal?: AbortSignal) => request<FileAgentTask>(`${taskPath(id)}/rerun`, post({}, signal)),
  replay: (id: string, signal?: AbortSignal) => request<FileAgentTask>(`${taskPath(id)}/replay`, post({}, signal)),
  apply: (id: string, artifactSha: string, signal?: AbortSignal) => request<FileAgentTask>(`${taskPath(id)}/apply`, post({ confirmed: true, artifact_sha256: artifactSha }, signal)),
  discard: (id: string, signal?: AbortSignal) => request<FileAgentTask>(`${taskPath(id)}/discard`, post({ confirmed: true }, signal)),
  stop: (id: string, signal?: AbortSignal) => request<FileAgentTask>(`${taskPath(id)}/stop`, post({}, signal)),
  deleteHistory: (id: string, signal?: AbortSignal) => request<void>(taskPath(id), { ...post({ confirmed: true }, signal), method: 'DELETE' }),
}
