import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { fileAgentApi } from '../fileAgentApi'
import type { FileAgentTask, FileDiff } from '../fileAgentTypes'
import { FileAgent } from './FileAgent'

vi.mock('../fileAgentApi', () => ({ fileAgentApi: {
  history: vi.fn(), task: vi.fn(), create: vi.fn(), diff: vi.fn(), file: vi.fn(),
  rerun: vi.fn(), replay: vi.fn(), apply: vi.fn(), discard: vi.fn(),
} }))

const task: FileAgentTask = {
  id: 'task-1', parent_id: null, prompt: 'Обнови инструкцию запуска', model: 'model-a',
  status: 'completed', base_sha: '1234567890abcdef',
  workspace: { id: 'workspace-1', state: 'exported', artifact: { sha256: 'a'.repeat(64) } },
  operations: [{ id: 1, timestamp: '2026-10-15T10:00:01Z', phase: 'tools', tool: 'file_list', status: 'success', result: { files: ['README.md', 'src/Main.java'] } }],
  read_files: ['README.md', 'src/Main.java'],
  changes: [{ path: 'README.md', status: 'modified', additions: 2, deletions: 1 }],
  usage: { input_tokens: 100, output_tokens: 20, total_tokens: 120, llm_calls: 2 },
  validation: { complete: true, ok: true }, parameters: { max_iterations: 10 },
  result: '**Инструкция обновлена.** <script>bad()</script>', error: null,
  created_at: '2026-10-15T10:00:00Z', updated_at: '2026-10-15T10:00:10Z',
  started_at: '2026-10-15T10:00:01Z', finished_at: '2026-10-15T10:00:10Z',
  can_apply: false, source_apply_enabled: false, correctness_guaranteed: false,
  patch_export_notice: 'Redacted preview is not an exact replayable patch.',
}
const diff: FileDiff = { diff: '--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+<script>alert(1)</script>', changes: task.changes, bytes: 100, redacted: true, truncated: true }
const props = { models: [{ id: 'model-a', name: 'Model A' }, { id: 'model-b', name: 'Model B' }], initialModel: 'model-a', onMenu: vi.fn() }

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
})

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(fileAgentApi.history).mockResolvedValue([task])
  vi.mocked(fileAgentApi.task).mockImplementation(async (id) => ({ ...task, id }))
  vi.mocked(fileAgentApi.diff).mockResolvedValue(diff)
  vi.mocked(fileAgentApi.file).mockResolvedValue({ path: 'README.md', content: '1: # Real file\n2: <img src=x onerror=bad()>', sha256: 'b'.repeat(64), total_lines: 2, start_line: 1, end_line: 2, redacted: true, truncated: false })
  vi.mocked(fileAgentApi.create).mockResolvedValue({ ...task, id: 'created-task' })
  vi.mocked(fileAgentApi.rerun).mockResolvedValue({ ...task, id: 'rerun-task', parent_id: task.id })
  vi.mocked(fileAgentApi.replay).mockResolvedValue({ ...task, id: 'replay-task', parent_id: 'rerun-task' })
  vi.mocked(fileAgentApi.apply).mockRejectedValue(new Error('Source apply is disabled (read-only Docker checkout).'))
  vi.mocked(fileAgentApi.discard).mockResolvedValue({ ...task, status: 'discarded', updated_at: '2026-10-15T10:00:20Z' })
})

afterEach(() => { vi.useRealTimers() })

describe('FileAgent', () => {
  it('runs a suggested task with the chosen model, then re-runs and replays through separate APIs', async () => {
    vi.mocked(fileAgentApi.history).mockResolvedValue([])
    render(<FileAgent {...props} />)
    await screen.findByText(/История пуста/)
    await userEvent.click(screen.getByText('Примеры задач'))
    const suggestion = screen.getByRole('button', { name: /Обнови API-документацию/ })
    await userEvent.click(suggestion)
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Модель File Agent' }), 'model-b')
    await userEvent.click(screen.getByRole('button', { name: 'Run Task' }))
    expect(fileAgentApi.create).toHaveBeenCalledWith(suggestion.textContent, 'model-b', expect.any(AbortSignal))
    await userEvent.click(await screen.findByRole('button', { name: 'Re-run Task' }))
    expect(fileAgentApi.rerun).toHaveBeenCalledWith('created-task', expect.any(AbortSignal))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Replay patch' })).toBeEnabled())
    await userEvent.click(screen.getByRole('button', { name: 'Replay patch' }))
    expect(fileAgentApi.replay).toHaveBeenCalledWith('rerun-task', expect.any(AbortSignal))
  })

  it('inspects actual file paths, escapes diff/source text, and ignores an obsolete file response', async () => {
    const { container } = render(<FileAgent {...props} />)
    const diffPanel = await screen.findByLabelText('Diff README.md')
    expect(diffPanel).toHaveTextContent('+<script>alert(1)</script>')
    expect(diffPanel.querySelector('.fa-diff__added')).toHaveTextContent('+<script>')
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getByText(/Усечено: показано не всё содержимое/)).toBeInTheDocument()
    expect(screen.getByText('120')).toBeInTheDocument()

    let resolveOld: (value: Awaited<ReturnType<typeof fileAgentApi.file>>) => void = () => undefined
    vi.mocked(fileAgentApi.file).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
    await userEvent.click(screen.getByRole('button', { name: 'Open File' }))
    const oldSignal = vi.mocked(fileAgentApi.file).mock.calls[0][2]
    await userEvent.click(screen.getByRole('button', { name: /src\/Main.java прочитан/ }))
    expect(oldSignal?.aborted).toBe(true)
    expect(await screen.findByLabelText('Содержимое src/Main.java')).toHaveTextContent('<img src=x onerror=bad()>')
    expect(container.querySelector('img')).toBeNull()
    await act(async () => resolveOld({ path: 'README.md', content: 'STALE CONTENT', sha256: '', total_lines: 1, start_line: 1, end_line: 1, truncated: false, redacted: true }))
    expect(screen.queryByText('STALE CONTENT')).not.toBeInTheDocument()
  })

  it('requires explicit confirmation, sends the reviewed artifact SHA, and never claims a rejected apply succeeded', async () => {
    render(<FileAgent {...props} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Apply Changes' }))
    let dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(task.base_sha)).toBeInTheDocument()
    expect(within(dialog).getByText('README.md')).toBeInTheDocument()
    expect(fileAgentApi.apply).not.toHaveBeenCalled()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Отмена' }))
    expect(fileAgentApi.apply).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Apply Changes' }))
    dialog = screen.getByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Confirm Apply Changes' }))
    expect(fileAgentApi.apply).toHaveBeenCalledWith(task.id, task.workspace.artifact?.sha256, expect.any(AbortSignal))
    expect(await screen.findByRole('alert')).toHaveTextContent('Изменения не применены: Source apply is disabled')
    expect(screen.queryByText('Сервер подтвердил применение изменений к исходному репозиторию.')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Discard Changes' }))
    expect(fileAgentApi.discard).not.toHaveBeenCalled()
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm Discard Changes' }))
    expect(fileAgentApi.discard).toHaveBeenCalledWith(task.id, expect.any(AbortSignal))
    expect(await screen.findByText('Рабочая копия удалена. Исходники не изменены.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Apply Changes' })).toBeDisabled()
  })

  it('polls active tasks sequentially and aborts pending requests and timers on unmount', async () => {
    vi.useFakeTimers()
    const running = { ...task, status: 'running', finished_at: null }
    vi.mocked(fileAgentApi.history).mockResolvedValue([running])
    vi.mocked(fileAgentApi.task).mockResolvedValue(running)
    const { unmount } = render(<FileAgent {...props} />)
    await act(async () => undefined)
    const callsAfterLoad = vi.mocked(fileAgentApi.history).mock.calls.length
    vi.mocked(fileAgentApi.history).mockImplementation(() => new Promise(() => undefined))
    await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
    expect(fileAgentApi.history).toHaveBeenCalledTimes(callsAfterLoad + 1)
    const signal = vi.mocked(fileAgentApi.history).mock.calls.at(-1)?.[0]
    await act(async () => { await vi.advanceTimersByTimeAsync(7500) })
    expect(fileAgentApi.history).toHaveBeenCalledTimes(callsAfterLoad + 1)
    unmount()
    expect(signal?.aborted).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('shows history failures and offers a real retry', async () => {
    vi.mocked(fileAgentApi.history).mockRejectedValueOnce(new Error('Network offline'))
    render(<FileAgent {...props} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Network offline')
    await userEvent.click(screen.getByRole('button', { name: 'Повторить' }))
    await screen.findByLabelText('Diff README.md')
    expect(fileAgentApi.history).toHaveBeenCalledTimes(2)
    expect(screen.queryByText(/Network offline/)).not.toBeInTheDocument()
  })

  it('labels fallback token usage as estimated instead of provider-reported', async () => {
    const estimated = { ...task, usage: { ...task.usage, estimated: true } }
    vi.mocked(fileAgentApi.history).mockResolvedValue([estimated])
    vi.mocked(fileAgentApi.task).mockResolvedValue(estimated)
    render(<FileAgent {...props} />)
    expect(await screen.findByText('Токены · оценка')).toBeInTheDocument()
    expect(screen.queryByText('Токены · по данным API')).not.toBeInTheDocument()
    expect(screen.getByText('120')).toBeInTheDocument()
  })
})
