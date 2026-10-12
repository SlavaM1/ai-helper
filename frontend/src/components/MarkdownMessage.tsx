import { useState, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import remarkGfm from 'remark-gfm'
import type { Message, RepositoryInfo, Source } from '../types'
import { CheckIcon, CopyIcon, ExternalIcon } from './Icons'

function CodeBlock({ children, className }: { children?: ReactNode; className?: string }) {
  const [copied, setCopied] = useState(false)
  const source = String(children ?? '').replace(/\n$/, '')
  const language = className?.match(/language-([^ ]+)/)?.[1]

  const copy = async () => {
    await navigator.clipboard.writeText(source)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }

  return (
    <div className="code-block">
      <div className="code-block__bar">
        <span>{language ?? 'text'}</span>
        <button type="button" onClick={copy} aria-label="Копировать код">
          {copied ? <CheckIcon /> : <CopyIcon />}
          {copied ? 'Скопировано' : 'Копировать'}
        </button>
      </div>
      <pre><code className={className}>{source}</code></pre>
    </div>
  )
}

const markdownComponents: Components = {
  code({ children, className, ...props }) {
    const inline = !className && !String(children).includes('\n')
    if (inline) return <code className="inline-code" {...props}>{children}</code>
    return <CodeBlock className={className}>{children}</CodeBlock>
  },
  pre({ children }) {
    return <>{children}</>
  },
  a({ children, href }) {
    return <a href={href} target="_blank" rel="noreferrer">{children}<ExternalIcon /></a>
  },
}

function githubSourceUrl(source: Source, repository?: RepositoryInfo): string | undefined {
  if (source.url) return source.url
  if (!repository?.remoteUrl || !source.path) return undefined
  const remote = repository.remoteUrl
    .replace(/^git@github\.com:/, 'https://github.com/')
    .replace(/\.git$/, '')
  if (!remote.startsWith('https://github.com/')) return undefined
  const ref = repository.commit ?? repository.branch ?? 'HEAD'
  const line = source.lineStart ? `#L${source.lineStart}${source.lineEnd && source.lineEnd !== source.lineStart ? `-L${source.lineEnd}` : ''}` : ''
  return `${remote}/blob/${encodeURIComponent(ref)}/${source.path.replace(/^\//, '')}${line}`
}

function MetaDetails({ message }: { message: Message }) {
  const hasMetrics = message.metrics && Object.values(message.metrics).some((value) => value !== undefined)
  const hasTools = Boolean(message.tools?.length)
  if (!hasMetrics && !hasTools) return null

  return (
    <details className="message-details">
      <summary>Метрики и инструменты</summary>
      {hasMetrics && (
        <div className="metrics-grid">
          {message.metrics?.model && <span><b>Модель</b>{message.metrics.model}</span>}
          {message.metrics?.totalTokens !== undefined && <span><b>Токены</b>{message.metrics.totalTokens.toLocaleString('ru-RU')}</span>}
          {message.metrics?.latencyMs !== undefined && <span><b>Ответ</b>{message.metrics.latencyMs.toLocaleString('ru-RU')} мс</span>}
          {message.metrics?.retrievalMs !== undefined && <span><b>Поиск</b>{message.metrics.retrievalMs.toLocaleString('ru-RU')} мс</span>}
        </div>
      )}
      {hasTools && (
        <div className="tool-log">
          {message.tools?.map((tool, index) => (
            <details key={`${tool.name}-${index}`}>
              <summary><code>{tool.name}</code><span>{tool.status ?? 'выполнен'}{tool.durationMs ? ` · ${tool.durationMs} мс` : ''}</span></summary>
              {(tool.input !== undefined || tool.output !== undefined) && (
                <pre>{JSON.stringify({ input: tool.input, output: tool.output }, null, 2)}</pre>
              )}
            </details>
          ))}
        </div>
      )}
    </details>
  )
}

export function MarkdownMessage({ message, repository }: { message: Message; repository?: RepositoryInfo }) {
  const isUser = message.role === 'user'
  return (
    <article className={`message message--${isUser ? 'user' : 'assistant'}${message.failed ? ' message--failed' : ''}`}>
      <div className="message__rail" aria-hidden="true"><span /></div>
      <div className="message__body">
        <div className="message__meta">
          <span>{isUser ? 'Вы' : 'Assistant'}</span>
          {message.createdAt && <time dateTime={message.createdAt}>{new Date(message.createdAt).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}</time>}
          {message.pending && <span className="stream-state">поток активен</span>}
          {message.failed && <span className="error-state">не отправлено</span>}
        </div>
        <div className="markdown">
          {message.content ? (
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} components={markdownComponents}>
              {message.content}
            </ReactMarkdown>
          ) : message.pending ? (
            <div className="thinking" aria-label="Assistant формирует ответ"><i /><i /><i /></div>
          ) : null}
        </div>
        {Boolean(message.sources?.length) && (
          <div className="sources">
            <span className="sources__label">Источники</span>
            <div className="sources__list">
              {message.sources?.map((source, index) => {
                const url = githubSourceUrl(source, repository)
                const label = `${source.label ?? source.path}${source.lineStart ? `:${source.lineStart}` : ''}`
                return url ? (
                  <a key={`${source.path}-${index}`} href={url} target="_blank" rel="noreferrer" title={source.score !== undefined ? `Релевантность: ${source.score.toFixed(3)}` : undefined}>
                    <span>{label}</span><ExternalIcon />
                  </a>
                ) : <span key={`${source.path}-${index}`} className="source-static">{label}</span>
              })}
            </div>
          </div>
        )}
        <MetaDetails message={message} />
      </div>
    </article>
  )
}
