import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { fileAgentApi } from '../fileAgentApi'
import type { FileAgentTask, FileDiff, FileSource } from '../fileAgentTypes'
import type { ModelInfo } from '../types'
import { AlertIcon, FileIcon, MenuIcon, RefreshIcon } from './Icons'
import './FileAgent.css'

const active = (task?: FileAgentTask) => task?.status === 'queued' || task?.status === 'running'
const errorText = (error: unknown) => error instanceof Error ? error.message : 'Неизвестная ошибка API'
const statusLabel = (status: string) => ({ queued: 'В очереди', running: 'Выполняется', completed: 'Готово к проверке', failed: 'Ошибка', applied: 'Применено к исходникам', discarded: 'Изменения отброшены' })[status] ?? status
const suggestions = [
  'Найди использования компонента: укажи имя сервиса. Прочитай связанные файлы и объясни связи со ссылками на строки.',
  'Обнови API-документацию: найди контроллеры, связанные service/DTO и создай docs/api-overview.md по реальному коду.',
  'Создай ADR: изучи архитектуру авторизации, используй RAG и создай docs/adr/0001-authentication-architecture.md. Отделяй факты от рекомендаций.',
  'Проверь архитектурные правила: контроллеры не должны обращаться к JPA repositories напрямую. Создай Markdown-отчёт с файлами и строками.',
]

function mergeTasks(current: FileAgentTask[], incoming: FileAgentTask[]) {
  const items = new Map(current.map((task) => [task.id, task]))
  for (const task of incoming) {
    const previous = items.get(task.id)
    if (!previous || task.updated_at >= previous.updated_at) items.set(task.id, task)
  }
  return [...items.values()].sort((a, b) => b.created_at.localeCompare(a.created_at))
}

function repositoryPaths(task: FileAgentTask) {
  const paths = new Set([...task.changes.map((change) => change.path), ...task.read_files])
  for (const operation of task.operations) {
    if (operation.tool !== 'file_list' || operation.status !== 'success') continue
    const result = operation.result
    if (result && typeof result === 'object' && 'files' in result && Array.isArray(result.files)) {
      for (const path of result.files) if (typeof path === 'string') paths.add(path)
    }
  }
  return [...paths]
}

function duration(task: FileAgentTask, now: number) {
  if (!task.started_at) return '—'
  const end = task.finished_at ? Date.parse(task.finished_at) : now
  const seconds = Math.max(0, Math.round((end - Date.parse(task.started_at)) / 1000))
  return Number.isFinite(seconds) ? `${Math.floor(seconds / 60)}м ${seconds % 60}с${task.finished_at ? '' : ' · идёт'}` : '—'
}

interface Confirmation { action: 'apply' | 'discard'; task: FileAgentTask }

function LifecycleConfirmation({ value, busy, onCancel, onConfirm }: { value?: Confirmation; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = dialogRef.current
    if (value && !dialog?.open) dialog?.showModal()
    if (!value && dialog?.open) dialog.close()
  }, [value])
  const applying = value?.action === 'apply'
  return <dialog ref={dialogRef} className="confirm-dialog fa-confirm" aria-labelledby="fa-confirm-title" onCancel={(event) => { event.preventDefault(); if (!busy) onCancel() }}>
    <h2 id="fa-confirm-title">{applying ? 'Apply Changes?' : 'Discard Changes?'}</h2>
    <p>{applying ? 'Применить проверенный артефакт к исходному репозиторию. Это изменит файлы на хосте, но не создаст коммит. При конфликте или read-only доступе операция будет отклонена.' : 'Удалить изолированную рабочую копию. Исходный репозиторий не изменится. Сохранённый приватный артефакт останется доступен для Replay patch.'}</p>
    <p>Base SHA: <code>{value?.task.base_sha || 'неизвестен'}</code></p>
    {applying && <p>Artifact SHA-256: <code>{value?.task.workspace.artifact?.sha256}</code></p>}
    <ul>{value?.task.changes.map((change) => <li key={change.path}><code>{change.path}</code> ({change.status})</li>)}</ul>
    {!value?.task.changes.length && <p>Нет зафиксированных изменений файлов.</p>}
    <div><button className="button-secondary" type="button" onClick={onCancel} disabled={busy} autoFocus>Отмена</button><button className={applying ? 'button-secondary' : 'button-danger'} type="button" onClick={onConfirm} disabled={busy}>{busy ? 'Выполняется…' : applying ? 'Confirm Apply Changes' : 'Confirm Discard Changes'}</button></div>
  </dialog>
}

interface Props { onMenu: () => void; models: ModelInfo[]; initialModel?: string; modelError?: boolean }

export function FileAgent({ onMenu, models, initialModel, modelError }: Props) {
  const [tasks, setTasks] = useState<FileAgentTask[]>([])
  const [selectedId, setSelectedId] = useState<string>()
  const [prompt, setPrompt] = useState('')
  const [model, setModel] = useState(initialModel ?? models[0]?.id ?? '')
  const [loading, setLoading] = useState(true)
  const [historyError, setHistoryError] = useState<string>()
  const [error, setError] = useState<string>()
  const [notice, setNotice] = useState<string>()
  const [refresh, setRefresh] = useState(0)
  const [busy, setBusy] = useState(false)
  const [confirmation, setConfirmation] = useState<Confirmation>()
  const [path, setPath] = useState('')
  const [mode, setMode] = useState<'diff' | 'file'>('diff')
  const [inspectionRefresh, setInspectionRefresh] = useState(0)
  const [preview, setPreview] = useState<FileDiff | FileSource>()
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string>()
  const [now, setNow] = useState(Date.now())
  const alive = useRef(false)
  const selectedRef = useRef<string | undefined>(undefined)
  const selectionVersion = useRef(0)
  const mutationBusy = useRef(false)
  const mutationControllers = useRef(new Set<AbortController>())
  const revision = useRef(0)
  const selected = tasks.find((task) => task.id === selectedId)
  const hasActive = tasks.some(active)
  const paths = selected ? repositoryPaths(selected) : []
  const selectedPath = path || selected?.changes[0]?.path || paths[0] || ''
  const workspaceAvailable = Boolean(selected?.workspace.id && selected.status !== 'discarded')

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      for (const controller of mutationControllers.current) controller.abort()
    }
  }, [])

  // Sequential polling avoids overlapping requests; tasks continue on the server after navigation.
  useEffect(() => {
    const controller = new AbortController()
    let timer: number | undefined
    const load = async () => {
      const requestRevision = revision.current
      try {
        const items = await fileAgentApi.history(controller.signal)
        if (controller.signal.aborted || requestRevision !== revision.current) return
        setTasks((current) => mergeTasks(current, items))
        if (!selectedRef.current && selectionVersion.current === 0 && items[0]) {
          selectedRef.current = items[0].id
          setSelectedId(items[0].id)
        }
        setHistoryError(undefined)
      } catch (reason) {
        if (!controller.signal.aborted) setHistoryError(`История не обновлена: ${errorText(reason)}`)
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false)
          if (hasActive) timer = window.setTimeout(() => void load(), 2500)
        }
      }
    }
    void load()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [refresh, hasActive])

  useEffect(() => {
    if (!selectedId) return
    const controller = new AbortController()
    fileAgentApi.task(selectedId, controller.signal).then((task) => {
      if (!controller.signal.aborted) setTasks((current) => mergeTasks(current, [task]))
    }).catch((reason) => { if (!controller.signal.aborted) setError(`Не удалось открыть задачу: ${errorText(reason)}`) })
    return () => controller.abort()
  }, [selectedId])

  useEffect(() => {
    if (!active(selected)) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [selected?.id, selected?.status])

  useEffect(() => {
    setPreview(undefined)
    setPreviewError(undefined)
    setPreviewLoading(false)
    if (!selectedId || !workspaceAvailable || !selectedPath) return
    const controller = new AbortController()
    setPreviewLoading(true)
    const request = mode === 'diff' ? fileAgentApi.diff(selectedId, selectedPath, controller.signal) : fileAgentApi.file(selectedId, selectedPath, controller.signal)
    request.then((value) => { if (!controller.signal.aborted) setPreview(value) })
      .catch((reason) => { if (!controller.signal.aborted) setPreviewError(errorText(reason)) })
      .finally(() => { if (!controller.signal.aborted) setPreviewLoading(false) })
    return () => controller.abort()
  }, [selectedId, selectedPath, mode, workspaceAvailable, inspectionRefresh, selected?.finished_at])

  const selectTask = (id?: string) => {
    selectionVersion.current += 1
    selectedRef.current = id
    setSelectedId(id)
    setPath('')
    setMode('diff')
    setError(undefined)
    setNotice(undefined)
  }

  const mutate = async (action: 'create' | 'rerun' | 'replay' | 'apply' | 'discard', target?: FileAgentTask) => {
    if (mutationBusy.current || (action === 'create' && !prompt.trim())) return
    mutationBusy.current = true
    setBusy(true)
    setError(undefined)
    setNotice(undefined)
    const version = selectionVersion.current
    const controller = new AbortController()
    mutationControllers.current.add(controller)
    revision.current += 1
    try {
      let task: FileAgentTask
      if (action === 'create') task = await fileAgentApi.create(prompt.trim(), model, controller.signal)
      else {
        if (!target) throw new Error('Выберите задачу')
        if (action === 'apply') {
          if (!target.workspace.artifact?.sha256) throw new Error('Нет сохранённого артефакта')
          task = await fileAgentApi.apply(target.id, target.workspace.artifact.sha256, controller.signal)
        } else task = await fileAgentApi[action](target.id, controller.signal)
      }
      if (!alive.current || controller.signal.aborted) return
      revision.current += 1
      setTasks((current) => mergeTasks(current, [task]))
      if (selectionVersion.current === version) {
        selectTask(task.id)
        setNotice(action === 'apply' ? 'Сервер подтвердил применение изменений к исходному репозиторию.' : action === 'discard' ? 'Рабочая копия удалена. Исходники не изменены.' : 'Задача сохранена и поставлена в очередь. Исходники не изменены.')
      }
    } catch (reason) {
      if (alive.current && !controller.signal.aborted) setError(`${action === 'apply' ? 'Изменения не применены' : 'Операция не выполнена'}: ${errorText(reason)}`)
    } finally {
      mutationControllers.current.delete(controller)
      mutationBusy.current = false
      if (alive.current) { setBusy(false); setConfirmation(undefined); setRefresh((value) => value + 1) }
    }
  }

  const result = selected?.result
  const summary = typeof result === 'string' ? result : result && typeof result === 'object' && 'summary' in result && typeof result.summary === 'string' ? result.summary : undefined
  const idle = selected && !active(selected)
  const applyEligible = idle && selected.status === 'completed' && selected.validation.ok && Boolean(selected.workspace.artifact?.sha256) && selected.changes.length > 0
  const latestOperation = selected?.operations.at(-1)

  return <div className="fa-workspace">
    <header className="fa-topbar">
      <button className="icon-button menu-button" type="button" onClick={onMenu} aria-label="Open navigation"><MenuIcon /></button>
      <FileIcon /><div><h1>File Agent</h1><span>Изолированная рабочая копия · исходники не меняются автоматически</span></div>
      <button className="fa-button fa-topbar__refresh" type="button" onClick={() => setRefresh((value) => value + 1)}><RefreshIcon />Обновить историю</button>
    </header>
    {historyError && <div className="error-banner" role="alert"><AlertIcon /><span>{historyError}</span><button type="button" onClick={() => setRefresh((value) => value + 1)}>Повторить</button></div>}
    {error && <div className="error-banner" role="alert"><AlertIcon /><span>{error}</span><button type="button" onClick={() => setError(undefined)}>Закрыть</button></div>}
    {notice && <div className="fa-notice" role="status">{notice}</div>}
    <div className="fa-layout">
      <aside className="fa-history" aria-label="История задач">
        <div className="fa-section-heading"><h2>Задачи</h2><button className="fa-button" type="button" onClick={() => selectTask()}>Новая задача</button></div>
        <p className="fa-caption">Последние 100 запусков · сохранены на сервере</p>
        {loading && <p className="fa-empty" role="status">Загрузка истории…</p>}
        {!loading && tasks.length === 0 && <p className="fa-empty">История пуста. Опишите изменение файлов и запустите задачу.</p>}
        <nav aria-label="Сохранённые задачи">{tasks.map((task) => <button className={`fa-task${task.id === selectedId ? ' is-active' : ''}`} type="button" key={task.id} onClick={() => selectTask(task.id)} aria-current={task.id === selectedId ? 'true' : undefined}>
          <span className={`fa-status fa-status--${task.status}`}>{statusLabel(task.status)}</span><strong>{task.prompt}</strong><time dateTime={task.created_at}>{new Date(task.created_at).toLocaleString('ru-RU')}</time><code>{task.base_sha.slice(0, 8)} · {task.model}</code>
        </button>)}</nav>
      </aside>
      <section className="fa-main" aria-label="Рабочая область файлов">
        <form className="fa-compose" onSubmit={(event) => { event.preventDefault(); void mutate('create') }}>
          <label htmlFor="fa-prompt">Задача для репозитория</label>
          <textarea id="fa-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} maxLength={20000} rows={3} placeholder="Какие файлы изучить и что изменить?" required />
          <div className="fa-compose__controls"><label>Модель<select aria-label="Модель File Agent" value={model} onChange={(event) => setModel(event.target.value)}><option value="">По умолчанию на сервере</option>{models.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><button className="fa-button fa-button--primary" type="submit" disabled={busy || !prompt.trim()}>{busy ? 'Запрос…' : 'Run Task'}</button></div>
          {modelError && <p className="fa-caption">Список моделей резервный: загрузка с сервера не удалась.</p>}
          <details className="fa-suggestions"><summary>Примеры задач</summary><div>{suggestions.map((suggestion) => <button type="button" key={suggestion} onClick={() => setPrompt(suggestion)}>{suggestion}</button>)}</div></details>
        </form>
        {selected ? <>
          <section className="fa-execution" aria-label="Выполнение задачи">
            <div className="fa-section-heading"><h2>Выполнение <code>{selected.id.slice(0, 8)}</code></h2><span className={`fa-status fa-status--${selected.status}`} role="status">{statusLabel(selected.status)}</span></div>
            <p className="fa-task-prompt">{selected.prompt}</p>
            <div className="fa-provenance"><span>Base SHA <code>{selected.base_sha}</code></span><span>Модель <code>{selected.model}</code></span>{selected.parent_id && <span>Родитель <code>{selected.parent_id}</code></span>}</div>
            <dl className="fa-metrics">
              <div><dt>Токены · {selected.usage.estimated ? 'оценка' : 'по данным API'}</dt><dd>{selected.usage.total_tokens?.toLocaleString('ru-RU') ?? '—'}</dd><small>вход {selected.usage.input_tokens ?? '—'} / выход {selected.usage.output_tokens ?? '—'}</small></div>
              <div><dt>Длительность</dt><dd>{duration(selected, now)}</dd></div>
              <div><dt>Вызовы LLM / tools</dt><dd>{selected.usage.llm_calls ?? 0} / {selected.operations.filter((operation) => operation.tool && operation.tool !== 'llm' && operation.status === 'started').length}</dd></div>
              <div><dt>Прочитано / изменено</dt><dd>{selected.read_files.length} / {selected.changes.length}</dd></div>
            </dl>
            {active(selected) && latestOperation && <p className="fa-current-operation" role="status"><code>{latestOperation.tool ?? latestOperation.phase}</code> · {latestOperation.status}</p>}
            {!selected.usage.estimated && selected.usage.total_tokens === 0 && <p className="fa-caption">Токены получены из usage API, не оценены. Нулевое значение также возможно, если провайдер не вернул usage.</p>}
            {selected.error && <p className="fa-error" role="alert">{selected.error}</p>}
            <div className="fa-actions">
              <button className="fa-button" type="button" disabled={!workspaceAvailable} onClick={() => { setMode('diff'); setInspectionRefresh((value) => value + 1); document.getElementById('fa-inspector')?.focus() }}>View Diff</button>
              <button className="fa-button" type="button" disabled={!idle || busy} onClick={() => void mutate('rerun', selected)}>Re-run Task</button>
              <button className="fa-button" type="button" disabled={!idle || busy || !selected.workspace.artifact} onClick={() => void mutate('replay', selected)}>Replay patch</button>
              <button className="fa-button fa-button--primary" type="button" disabled={!applyEligible || busy || (selected.source_apply_enabled && !selected.can_apply)} onClick={() => setConfirmation({ action: 'apply', task: selected })}>Apply Changes</button>
              <button className="fa-button fa-button--danger" type="button" disabled={!idle || !workspaceAvailable || busy} onClick={() => setConfirmation({ action: 'discard', task: selected })}>Discard Changes</button>
            </div>
            <p className="fa-caption">Re-run Task: новый запуск модели с сохранённой базой и параметрами. Replay patch: точный сохранённый патч в новой рабочей копии, без нового решения модели.</p>
            {!selected.source_apply_enabled && <div className="fa-host-notice"><strong>Применение к исходникам отключено (read-only Docker).</strong><p>Apply Changes запросит подтверждение и покажет ответ сервера. Для применения проверьте точный приватный артефакт на доверенном хосте; включать <code>AI_HELPER_FILE_AGENT_ALLOW_SOURCE_APPLY=true</code> можно только с writable checkout. Preview не подходит для точного применения.</p></div>}
            <details className="fa-details"><summary>Операции и прогресс ({selected.operations.length})</summary><p className="fa-caption">Только фактические вызовы инструментов и их статусы. Не внутренние рассуждения модели.</p><ol className="fa-operations">{selected.operations.map((operation) => <li key={operation.id}><time>{new Date(operation.timestamp).toLocaleTimeString('ru-RU')}</time><code>{operation.tool ?? operation.phase}</code><span>{operation.status}</span>{operation.arguments?.path && <code>{operation.arguments.path}</code>}{operation.elapsed_ms != null && <span>{operation.elapsed_ms} мс</span>}{operation.error && <span className="fa-error">{operation.error}</span>}</li>)}</ol></details>
            <details className="fa-details"><summary>Валидация и параметры · {selected.validation.complete ? selected.validation.ok ? 'проверки пройдены' : 'есть ошибки' : 'не завершена'}</summary><p>Проверки Markdown и лексические/архитектурные эвристики Java. Сборка, компиляция и тесты не запускаются; корректность не гарантируется.</p><pre>{JSON.stringify({ validation: selected.validation, parameters: selected.parameters }, null, 2)}</pre></details>
            {summary && <section className="fa-result"><h3>Результат</h3><div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{ a: ({ children, href }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>, img: () => null }}>{summary}</ReactMarkdown></div></section>}
            {result != null && !summary && <details className="fa-details"><summary>Результат</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>}
          </section>
          <section id="fa-inspector" className="fa-inspector" tabIndex={-1} aria-label="Файлы и diff">
            <div className="fa-files"><h2>Файлы репозитория</h2><p className="fa-caption">Из реальных list/read/diff операций</p>{paths.map((filePath) => {
              const change = selected.changes.find((item) => item.path === filePath)
              return <button type="button" className={selectedPath === filePath ? 'is-active' : ''} key={filePath} onClick={() => setPath(filePath)} aria-pressed={selectedPath === filePath}><code>{filePath}</code><span>{change ? <>{change.status} <b className="fa-added">+{change.additions}</b> <b className="fa-removed">−{change.deletions}</b></> : selected.read_files.includes(filePath) ? 'прочитан' : 'найден'}</span></button>
            })}{!paths.length && <p className="fa-empty">Файлы появятся после операций агента.</p>}</div>
            <div className="fa-preview">
              <div className="fa-preview__toolbar"><code>{selectedPath || 'Файл не выбран'}</code><button className="fa-button" type="button" aria-pressed={mode === 'diff'} disabled={!workspaceAvailable || !selectedPath} onClick={() => { setMode('diff'); setInspectionRefresh((value) => value + 1) }}>View Diff</button><button className="fa-button" type="button" aria-pressed={mode === 'file'} disabled={!workspaceAvailable || !selectedPath} onClick={() => { setMode('file'); setInspectionRefresh((value) => value + 1) }}>Open File</button></div>
              {!workspaceAvailable && <p className="fa-empty">{selected.status === 'discarded' ? 'Рабочая копия удалена. Используйте Replay patch для просмотра сохранённых изменений.' : 'Рабочая копия ещё не создана.'}</p>}
              {previewLoading && <p className="fa-empty" role="status">Загрузка {mode === 'diff' ? 'diff' : 'файла'}…</p>}
              {previewError && <div className="fa-empty fa-error" role="alert">{previewError}<button className="fa-button" type="button" onClick={() => setInspectionRefresh((value) => value + 1)}>Повторить просмотр</button></div>}
              {preview && <>
                <p className="fa-preview__meta">{preview.redacted ? 'Редактированный preview: секреты скрыты' : 'Preview'}{preview.truncated && ' · Усечено: показано не всё содержимое'}{'sha256' in preview && <><br />SHA-256 исходных байтов: <code>{preview.sha256}</code><br />Строки {preview.start_line}–{preview.end_line} из {preview.total_lines}</>}{'bytes' in preview && ` · ${preview.bytes} байт`}</p>
                {'diff' in preview ? preview.diff ? <pre className="fa-diff" tabIndex={0} aria-label={`Diff ${selectedPath}`}><code>{preview.diff.split('\n').map((line, index) => <span key={index} className={line.startsWith('+') && !line.startsWith('+++') ? 'fa-diff__added' : line.startsWith('-') && !line.startsWith('---') ? 'fa-diff__removed' : line.startsWith('@@') ? 'fa-diff__hunk' : ''}>{line || ' '}</span>)}</code></pre> : <p className="fa-empty">Нет изменений для этого файла.</p> : <pre className="fa-source" tabIndex={0} aria-label={`Содержимое ${selectedPath}`}><code>{preview.content}</code></pre>}
              </>}
              {selected.patch_export_notice && <p className="fa-caption fa-export-notice">{selected.patch_export_notice}</p>}
            </div>
          </section>
        </> : <p className="fa-empty">Запустите задачу или выберите сохранённый запуск. Агент изучит реальные файлы, подготовит изменения и сохранит diff отдельно от исходников.</p>}
      </section>
    </div>
    <LifecycleConfirmation value={confirmation} busy={busy} onCancel={() => setConfirmation(undefined)} onConfirm={() => { if (confirmation) void mutate(confirmation.action, confirmation.task) }} />
  </div>
}
