import type { McpTool, SummaryInfo, UsageStats } from '../types'
import { ChartIcon, CloseIcon } from './Icons'

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="stat"><span>{label}</span><strong>{value}</strong></div>
}

export function StatsDrawer({ open, loading, summary, current, aggregate, tools, onClose }: {
  open: boolean
  loading?: boolean
  summary?: SummaryInfo
  current?: UsageStats
  aggregate?: UsageStats
  tools: McpTool[]
  onClose: () => void
}) {
  return (
    <>
      <button className={`drawer-scrim${open ? ' is-open' : ''}`} aria-label="Закрыть статистику" onClick={onClose} />
      <aside className={`stats-drawer${open ? ' is-open' : ''}`} aria-label="Статистика и контекст" aria-hidden={!open}>
        <header><div><ChartIcon /><span>Контекст сессии</span></div><button className="icon-button" onClick={onClose} aria-label="Закрыть"><CloseIcon /></button></header>
        {loading ? <div className="drawer-loading"><i /><i /><i /></div> : (
          <div className="drawer-content">
            <section>
              <h2>Текущий диалог</h2>
              <div className="stats-grid">
                <Stat label="Запросов" value={(current?.requests ?? 0).toLocaleString('ru-RU')} />
                <Stat label="Токенов" value={(current?.totalTokens ?? 0).toLocaleString('ru-RU')} />
                <Stat label="Вход" value={(current?.promptTokens ?? 0).toLocaleString('ru-RU')} />
                <Stat label="Выход" value={(current?.completionTokens ?? 0).toLocaleString('ru-RU')} />
              </div>
              {Boolean(current?.cost) && <div className="cost-line"><span>Расход</span><strong>${current?.cost.toFixed(4)}</strong></div>}
            </section>
            <section>
              <h2>Все диалоги</h2>
              <div className="aggregate-line"><strong>{(aggregate?.totalTokens ?? 0).toLocaleString('ru-RU')}</strong><span>токенов в {(aggregate?.requests ?? 0).toLocaleString('ru-RU')} запросах</span></div>
            </section>
            <section>
              <h2>Сводка памяти</h2>
              {summary?.content ? <p className="summary-text">{summary.content}</p> : <p className="muted">Сводка появится после накопления контекста.</p>}
              {summary?.messageCount !== undefined && <span className="summary-meta">Учтено сообщений: {summary.messageCount}</span>}
            </section>
            <section>
              <h2>MCP-инструменты <span>{tools.length}</span></h2>
              <div className="available-tools">
                {tools.map((tool) => (
                  <details key={tool.name}>
                    <summary><code>{tool.name}</code></summary>
                    <p>{tool.description ?? 'Описание не предоставлено сервером.'}</p>
                  </details>
                ))}
                {!tools.length && <p className="muted">Список инструментов недоступен.</p>}
              </div>
            </section>
          </div>
        )}
      </aside>
    </>
  )
}
