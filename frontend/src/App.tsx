import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { aggregateUsage, api, streamChat } from './api'
import { Composer } from './components/Composer'
import { CodeReviews } from './components/CodeReviews'
import { ConfirmDialog } from './components/ConfirmDialog'
import { ConversationSidebar, type AppView } from './components/ConversationSidebar'
import { AlertIcon, BranchIcon, ChartIcon, DatabaseIcon, MenuIcon, RefreshIcon } from './components/Icons'
import { StatsDrawer } from './components/StatsDrawer'
import type {
  ContextPreset,
  Conversation,
  McpTool,
  Message,
  ModelInfo,
  RagStatus,
  RepositoryInfo,
  SummaryInfo,
  UsageStats,
} from './types'

const FALLBACK_MODELS: ModelInfo[] = [
  { id: 'deepseek-flash', name: 'DeepSeek Flash' },
  { id: 'deepseek-v4-pro', name: 'DeepSeek V4 Pro' },
]

const MarkdownMessage = lazy(() => import('./components/MarkdownMessage').then((module) => ({ default: module.MarkdownMessage })))
const FileAgent = lazy(() => import('./components/FileAgent').then((module) => ({ default: module.FileAgent })))

const PRESETS: { id: ContextPreset; label: string }[] = [
  { id: '8192', label: '8K' },
  { id: '16384', label: '16K' },
  { id: '32768', label: '32K' },
  { id: '65536', label: '64K' },
  { id: 'custom', label: 'Свой лимит' },
]

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : 'Неизвестная ошибка API'
}

function ragLabel(status?: string): string {
  switch (status?.toLowerCase()) {
    case 'ready': case 'indexed': case 'complete': return 'RAG готов'
    case 'indexing': case 'running': case 'pending': return 'Индексация'
    case 'error': case 'failed': return 'Ошибка RAG'
    default: return 'RAG неизвестен'
  }
}

function App() {
  const [view, setView] = useState<AppView>('chat')
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<string>()
  const [messages, setMessages] = useState<Message[]>([])
  const [models, setModels] = useState<ModelInfo[]>(FALLBACK_MODELS)
  const [modelError, setModelError] = useState(false)
  const [repository, setRepository] = useState<RepositoryInfo>()
  const [rag, setRag] = useState<RagStatus>()
  const [tools, setTools] = useState<McpTool[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [statsOpen, setStatsOpen] = useState(false)
  const [statsLoading, setStatsLoading] = useState(false)
  const [summary, setSummary] = useState<SummaryInfo>()
  const [currentUsage, setCurrentUsage] = useState<UsageStats>()
  const [totalUsage, setTotalUsage] = useState<UsageStats>()
  const [deleteTarget, setDeleteTarget] = useState<Conversation>()
  const [deleting, setDeleting] = useState(false)
  const [reindexing, setReindexing] = useState(false)
  const [error, setError] = useState<string>()
  const abortRef = useRef<AbortController | undefined>(undefined)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const activeConversation = conversations.find((item) => item.id === activeId)

  useEffect(() => {
    let alive = true
    Promise.allSettled([
      api.getConversations(),
      api.getModels(),
      api.getRepository(),
      api.getRagStatus(),
      api.getTools(),
    ]).then((results) => {
      if (!alive) return
      const [conversationResult, modelResult, repositoryResult, ragResult, toolResult] = results
      if (conversationResult.status === 'fulfilled') {
        setConversations(conversationResult.value)
        setActiveId(conversationResult.value[0]?.id)
      } else {
        setError(`Не удалось загрузить диалоги: ${errorText(conversationResult.reason)}`)
      }
      if (modelResult.status === 'fulfilled' && modelResult.value.length) {
        setModels(modelResult.value)
      } else {
        setModelError(true)
      }
      if (repositoryResult.status === 'fulfilled') setRepository(repositoryResult.value)
      if (ragResult.status === 'fulfilled') setRag(ragResult.value)
      if (toolResult.status === 'fulfilled') setTools(toolResult.value)
      setLoading(false)
    })
    return () => { alive = false }
  }, [])

  useEffect(() => {
    if (!activeId) {
      setMessages([])
      return
    }
    let alive = true
    setLoadingMessages(true)
    api.getMessages(activeId)
      .then((items) => { if (alive) setMessages(items) })
      .catch((reason) => { if (alive) setError(`Не удалось загрузить сообщения: ${errorText(reason)}`) })
      .finally(() => { if (alive) setLoadingMessages(false) })
    return () => { alive = false }
  }, [activeId])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: streaming ? 'instant' : 'smooth', block: 'end' })
  }, [messages, streaming])

  useEffect(() => {
    if (!rag || !['indexing', 'running', 'pending'].includes(rag.status.toLowerCase())) return
    const timer = window.setInterval(() => {
      api.getRagStatus().then(setRag).catch(() => undefined)
    }, 2500)
    return () => window.clearInterval(timer)
  }, [rag?.status])

  const createConversation = async () => {
    try {
      setError(undefined)
      const conversation = await api.createConversation()
      setConversations((current) => [conversation, ...current])
      setActiveId(conversation.id)
      setView('chat')
      setSidebarOpen(false)
    } catch (reason) {
      setError(`Не удалось создать диалог: ${errorText(reason)}`)
    }
  }

  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        void createConversation()
      }
    }
    window.addEventListener('keydown', listener)
    return () => window.removeEventListener('keydown', listener)
  })

  const updateConversation = async (id: string, patch: Record<string, unknown>) => {
    const previous = conversations
    setConversations((current) => current.map((item) => item.id === id ? {
      ...item,
      ...(typeof patch.title === 'string' ? { title: patch.title } : {}),
      ...(typeof patch.model === 'string' ? { model: patch.model } : {}),
      ...(typeof patch.context_window === 'number' ? { contextWindow: patch.context_window } : {}),
      ...(typeof patch.context_preset === 'string' ? { contextPreset: patch.context_preset } : {}),
      ...(typeof patch.custom_context === 'string' ? { customContext: patch.custom_context } : {}),
    } : item))
    try {
      await api.updateConversation(id, patch)
    } catch (reason) {
      setConversations(previous)
      setError(`Настройки не сохранены: ${errorText(reason)}`)
    }
  }

  const deleteConversation = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await api.deleteConversation(deleteTarget.id)
      const remaining = conversations.filter((item) => item.id !== deleteTarget.id)
      setConversations(remaining)
      if (activeId === deleteTarget.id) setActiveId(remaining[0]?.id)
      setDeleteTarget(undefined)
    } catch (reason) {
      setError(`Не удалось удалить диалог: ${errorText(reason)}`)
    } finally {
      setDeleting(false)
    }
  }

  const sendMessage = async (content: string) => {
    if (!activeId || streaming) return
    const userId = `local-user-${crypto.randomUUID()}`
    const assistantId = `local-assistant-${crypto.randomUUID()}`
    const createdAt = new Date().toISOString()
    setMessages((current) => [
      ...current,
      { id: userId, role: 'user', content, createdAt },
      { id: assistantId, role: 'assistant', content: '', createdAt, pending: true },
    ])
    setStreaming(true)
    setError(undefined)
    const controller = new AbortController()
    abortRef.current = controller
    let streamFailed = false

    const updateAssistant = (update: (message: Message) => Message) => {
      setMessages((current) => current.map((message) => message.id === assistantId ? update(message) : message))
    }

    try {
      await streamChat(activeId, content, {
        onDelta: (delta) => updateAssistant((message) => ({ ...message, content: message.content + delta })),
        onSources: (sources) => updateAssistant((message) => ({ ...message, sources })),
        onMetrics: (metrics) => updateAssistant((message) => ({ ...message, metrics })),
        onTool: (tool) => updateAssistant((message) => ({ ...message, tools: [...(message.tools ?? []), tool] })),
        onDone: (serverMessage) => updateAssistant((message) => ({
          ...message,
          ...(serverMessage?.content && !message.content ? { content: serverMessage.content } : {}),
          ...(serverMessage?.id ? { id: serverMessage.id } : {}),
          sources: serverMessage?.sources ?? message.sources,
          metrics: serverMessage?.metrics ?? message.metrics,
          tools: serverMessage?.tools ?? message.tools,
          pending: false,
        })),
        onError: (reason) => {
          streamFailed = true
          updateAssistant((message) => ({ ...message, pending: false, failed: true, content: message.content || reason.message }))
          setError(reason.message)
        },
      }, controller.signal)
    } catch (reason) {
      if (!controller.signal.aborted) {
        streamFailed = true
        updateAssistant((message) => ({ ...message, pending: false, failed: true }))
        setError(`Ответ не получен: ${errorText(reason)}`)
      }
    } finally {
      if (!streamFailed) updateAssistant((message) => ({ ...message, pending: false }))
      setStreaming(false)
      abortRef.current = undefined
    }
  }

  const openStats = async () => {
    setStatsOpen(true)
    setStatsLoading(true)
    try {
      const usageResults = await Promise.all(conversations.map((conversation) => api.getUsage(conversation.id).catch(() => undefined)))
      const validUsage = usageResults.filter((item): item is UsageStats => Boolean(item))
      setTotalUsage(aggregateUsage(validUsage))
      if (activeId) {
        const [usageResult, summaryResult] = await Promise.allSettled([api.getUsage(activeId), api.getSummary(activeId)])
        if (usageResult.status === 'fulfilled') setCurrentUsage(usageResult.value)
        if (summaryResult.status === 'fulfilled') setSummary(summaryResult.value)
      }
    } finally {
      setStatsLoading(false)
    }
  }

  const runReindex = async () => {
    setReindexing(true)
    try {
      setRag(await api.reindex())
    } catch (reason) {
      setError(`Индексация не запущена: ${errorText(reason)}`)
    } finally {
      setReindexing(false)
    }
  }

  const openReviewDiscussion = async (conversationId: string) => {
    setView('chat')
    setActiveId(conversationId)
    setSidebarOpen(false)
    if (conversations.some((conversation) => conversation.id === conversationId)) return
    try {
      const conversation = await api.getConversation(conversationId)
      setConversations((current) => current.some((item) => item.id === conversation.id) ? current : [conversation, ...current])
    } catch (reason) {
      setConversations((current) => [{ id: conversationId, title: 'Review discussion' }, ...current])
      setError(`Диалог открыт, но его данные не загружены: ${errorText(reason)}`)
    }
  }

  const selectedModel = activeConversation?.model ?? models[0]?.id ?? ''
  const contextWindow = activeConversation?.contextWindow ?? 16384
  const selectedPreset = (PRESETS.some((item) => item.id === String(contextWindow)) ? String(contextWindow) : 'custom') as ContextPreset

  return (
    <div className="app-shell">
      <ConversationSidebar
        conversations={conversations}
        activeId={activeId}
        open={sidebarOpen}
        loading={loading}
        view={view}
        onClose={() => setSidebarOpen(false)}
        onViewChange={setView}
        onCreate={() => void createConversation()}
        onSelect={(id) => { setActiveId(id); setView('chat') }}
        onRename={(id, title) => void updateConversation(id, { title })}
        onDelete={setDeleteTarget}
      />

      <main className="workspace">
        {view === 'reviews' ? (
          <CodeReviews onMenu={() => setSidebarOpen(true)} onDiscuss={(conversationId) => void openReviewDiscussion(conversationId)} />
        ) : view === 'file-agent' ? (
          <Suspense fallback={<div className="chat-loading" role="status" aria-label="Загрузка File Agent"><i /><i /><i /></div>}>
            <FileAgent onMenu={() => setSidebarOpen(true)} models={models} initialModel={selectedModel} modelError={modelError} />
          </Suspense>
        ) : <>
          <header className="topbar">
          <button className="icon-button menu-button" onClick={() => setSidebarOpen(true)} aria-label="Открыть диалоги"><MenuIcon /></button>
          <div className="repo-state">
            <BranchIcon />
            <div><span>Ветка</span><strong>{repository?.branch ?? 'не определена'}</strong></div>
            {repository?.dirty && <span className="dirty-dot" title="Есть незакоммиченные изменения" />}
          </div>
          <div className="topbar__controls">
            <label className="select-control">
              <span>Модель{modelError && <i title="Использован резервный список">!</i>}</span>
              <select
                aria-label="Модель"
                value={selectedModel}
                disabled={!activeId}
                onChange={(event) => activeId && void updateConversation(activeId, { model: event.target.value })}
              >
                {models.map((model) => <option value={model.id} key={model.id}>{model.name}</option>)}
              </select>
            </label>
            <label className="select-control context-control">
              <span>Контекст</span>
              <select
                aria-label="Режим контекста"
                value={selectedPreset}
                disabled={!activeId}
                onChange={(event) => {
                  const value = Number(event.target.value)
                  if (activeId && Number.isFinite(value)) void updateConversation(activeId, { context_window: value })
                  if (activeId && event.target.value === 'custom') {
                    setConversations((current) => current.map((item) => item.id === activeId ? { ...item, contextWindow: 12000 } : item))
                  }
                }}
              >
                {PRESETS.map((preset) => <option value={preset.id} key={preset.id}>{preset.label}</option>)}
              </select>
            </label>
            <div className={`rag-chip rag-chip--${rag?.status?.toLowerCase() ?? 'unknown'}`} title={rag?.message ?? `Чанков: ${rag?.totalChunks ?? '—'}`}>
              <DatabaseIcon /><span>{ragLabel(rag?.status)}</span>
              <button type="button" onClick={() => void runReindex()} disabled={reindexing} aria-label="Переиндексировать репозиторий"><RefreshIcon /></button>
            </div>
            <button className="stats-button" type="button" onClick={() => void openStats()}><ChartIcon /><span>Статистика</span></button>
          </div>
          </header>

        {activeId && selectedPreset === 'custom' && (
          <div className="custom-context">
            <span>Лимит контекста, tokens</span>
            <input
              type="number"
              min={4096}
              max={131072}
              step={1024}
              value={contextWindow}
              onChange={(event) => setConversations((current) => current.map((item) => item.id === activeId ? { ...item, contextWindow: Number(event.target.value) } : item))}
              onBlur={(event) => void updateConversation(activeId, { context_window: Number(event.target.value) })}
              aria-label="Пользовательский лимит контекста"
            />
          </div>
        )}

        {error && (
          <div className="error-banner" role="alert"><AlertIcon /><span>{error}</span><button type="button" onClick={() => setError(undefined)}>Закрыть</button></div>
        )}

        <section className="chat" aria-live="polite">
          {loadingMessages ? (
            <div className="chat-loading"><i /><i /><i /></div>
          ) : !activeId ? (
            <div className="welcome-state">
              <div className="welcome-graph" aria-hidden="true"><i /><i /><i /><b /></div>
              <span>Репозиторий подключён</span>
              <h1>Проследите код<br />до причины.</h1>
              <p>Задавайте вопросы об архитектуре, изменениях и связях между файлами. Assistant использует индекс репозитория и read-only Git-инструменты.</p>
              <button type="button" onClick={() => void createConversation()}>Начать диалог</button>
            </div>
          ) : messages.length === 0 ? (
            <div className="empty-chat">
              <div className="route-mark" aria-hidden="true"><span /><span /><span /></div>
              <p>Контекст готов</p>
              <h1>Что исследуем в <code>{repository?.name ?? repository?.path?.split('/').pop() ?? 'репозитории'}</code>?</h1>
              <div className="prompt-grid">
                {['Объясни архитектуру проекта', 'Найди путь обработки API-запроса', 'Что менялось в последних коммитах?'].map((prompt) => (
                  <button type="button" key={prompt} onClick={() => void sendMessage(prompt)}>{prompt}</button>
                ))}
              </div>
            </div>
          ) : (
            <div className="message-list">
              <Suspense fallback={<div className="chat-loading"><i /><i /><i /></div>}>
                {messages.map((message) => <MarkdownMessage key={message.id} message={message} repository={repository} />)}
              </Suspense>
              <div ref={messagesEndRef} />
            </div>
          )}
        </section>

          <Composer
            disabled={!activeId || loadingMessages}
            streaming={streaming}
            onSend={(message) => void sendMessage(message)}
            onStop={() => abortRef.current?.abort()}
          />
        </>}
      </main>

      <StatsDrawer open={statsOpen} loading={statsLoading} summary={summary} current={currentUsage} aggregate={totalUsage} tools={tools} onClose={() => setStatsOpen(false)} />
      <ConfirmDialog conversation={deleteTarget} busy={deleting} onCancel={() => setDeleteTarget(undefined)} onConfirm={() => void deleteConversation()} />
    </div>
  )
}

export default App
