import { useEffect, useRef, useState } from 'react'
import type { Conversation } from '../types'
import { ChatIcon, CloseIcon, EditIcon, PlusIcon, ReviewIcon, SparkIcon, TrashIcon } from './Icons'

export type AppView = 'chat' | 'reviews'

interface Props {
  conversations: Conversation[]
  activeId?: string
  open: boolean
  loading?: boolean
  view: AppView
  onClose: () => void
  onViewChange: (view: AppView) => void
  onCreate: () => void
  onSelect: (id: string) => void
  onRename: (id: string, title: string) => void
  onDelete: (conversation: Conversation) => void
}

export function ConversationSidebar({ conversations, activeId, open, loading, view, onClose, onViewChange, onCreate, onSelect, onRename, onDelete }: Props) {
  const [editingId, setEditingId] = useState<string>()
  const [title, setTitle] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (editingId) inputRef.current?.select()
  }, [editingId])

  const beginRename = (conversation: Conversation) => {
    setEditingId(conversation.id)
    setTitle(conversation.title)
  }

  const submitRename = () => {
    const nextTitle = title.trim()
    if (editingId && nextTitle) onRename(editingId, nextTitle)
    setEditingId(undefined)
  }

  return (
    <>
      <button className={`mobile-scrim${open ? ' is-open' : ''}`} aria-label="Закрыть список диалогов" onClick={onClose} />
      <aside className={`sidebar${open ? ' is-open' : ''}`} aria-label="Диалоги">
        <div className="brand">
          <div className="brand__mark"><SparkIcon /></div>
          <div><strong>Repo Assistant</strong><span>developer workspace</span></div>
          <button className="icon-button sidebar__close" onClick={onClose} aria-label="Закрыть"><CloseIcon /></button>
        </div>
        <nav className="section-nav" aria-label="Разделы">
          <button className={view === 'chat' ? 'is-active' : ''} type="button" onClick={() => { onViewChange('chat'); onClose() }}>
            <ChatIcon /><span>Чат</span>
          </button>
          <button className={view === 'reviews' ? 'is-active' : ''} type="button" onClick={() => { onViewChange('reviews'); onClose() }}>
            <ReviewIcon /><span>Code Reviews</span>
          </button>
        </nav>
        <button className="new-chat" type="button" onClick={onCreate} disabled={loading}>
          <PlusIcon /> <span>Новый диалог</span><kbd>⌘ K</kbd>
        </button>
        <div className="conversation-heading"><span>Диалоги</span><span>{conversations.length}</span></div>
        <nav className="conversation-list">
          {conversations.map((conversation) => (
            <div className={`conversation${activeId === conversation.id ? ' is-active' : ''}`} key={conversation.id}>
              {editingId === conversation.id ? (
                <form onSubmit={(event) => { event.preventDefault(); submitRename() }}>
                  <input
                    ref={inputRef}
                    value={title}
                    onChange={(event) => setTitle(event.target.value)}
                    onBlur={submitRename}
                    onKeyDown={(event) => { if (event.key === 'Escape') setEditingId(undefined) }}
                    aria-label="Название диалога"
                  />
                </form>
              ) : (
                <button className="conversation__select" type="button" onClick={() => { onSelect(conversation.id); onClose() }}>
                  <span className="conversation__node" />
                  <span>{conversation.title}</span>
                </button>
              )}
              {editingId !== conversation.id && (
                <div className="conversation__actions">
                  <button type="button" onClick={() => beginRename(conversation)} aria-label={`Переименовать «${conversation.title}»`}><EditIcon /></button>
                  <button type="button" onClick={() => onDelete(conversation)} aria-label={`Удалить «${conversation.title}»`}><TrashIcon /></button>
                </div>
              )}
            </div>
          ))}
          {!loading && conversations.length === 0 && (
            <div className="sidebar-empty">Здесь появится история работы с репозиторием.</div>
          )}
        </nav>
        <div className="sidebar__graph" aria-hidden="true">
          <i /><i /><i /><span>main</span>
        </div>
      </aside>
    </>
  )
}
