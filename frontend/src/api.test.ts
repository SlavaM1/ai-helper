import { afterEach, describe, expect, it, vi } from 'vitest'
import { aggregateUsage, api, streamChat } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('API client', () => {
  it('создаёт диалог через POST /api/conversations', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: 'c-1', title: 'Новый диалог' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.createConversation()).resolves.toMatchObject({ id: 'c-1', title: 'Новый диалог' })
    expect(fetchMock).toHaveBeenCalledWith('/api/conversations', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ title: 'Новый диалог' }),
    }))
  })

  it('сохраняет модель и контекст через PATCH выбранного диалога', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      id: 'c-2',
      title: 'API flow',
      model: 'deepseek-reasoner',
      context_preset: 'deep',
    }))
    vi.stubGlobal('fetch', fetchMock)

    const updated = await api.updateConversation('c-2', { model: 'deepseek-reasoner', context_preset: 'deep' })

    expect(updated).toMatchObject({ model: 'deepseek-reasoner', contextPreset: 'deep' })
    expect(fetchMock).toHaveBeenCalledWith('/api/conversations/c-2', expect.objectContaining({
      method: 'PATCH',
      body: JSON.stringify({ model: 'deepseek-reasoner', context_preset: 'deep' }),
    }))
  })

  it('удаляет диалог через DELETE', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    await api.deleteConversation('conversation with spaces')

    expect(fetchMock).toHaveBeenCalledWith('/api/conversations/conversation%20with%20spaces', expect.objectContaining({ method: 'DELETE' }))
  })

  it('суммирует usage всех диалогов', () => {
    expect(aggregateUsage([
      { promptTokens: 100, completionTokens: 40, totalTokens: 140, requests: 2, cost: 0.02 },
      { promptTokens: 250, completionTokens: 110, totalTokens: 360, requests: 3, cost: 0.05 },
    ])).toEqual({ promptTokens: 350, completionTokens: 150, totalTokens: 500, requests: 5, cost: 0.07 })
  })

  it('разбирает именованные и завершающие SSE-события POST chat', async () => {
    const body = [
      'event: token\ndata: {"delta":"Привет"}\n\n',
      'event: sources\ndata: {"sources":[{"path":"src/app.py","line_start":12}]}\n\n',
      'data: [DONE]\n\n',
    ].join('')
    const fetchMock = vi.fn().mockResolvedValue(new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const onDelta = vi.fn()
    const onSources = vi.fn()
    const onDone = vi.fn()

    await streamChat('c-1', 'test', { onDelta, onSources, onDone })

    expect(fetchMock).toHaveBeenCalledWith('/api/conversations/c-1/chat', expect.objectContaining({ method: 'POST', body: '{"message":"test"}' }))
    expect(onDelta).toHaveBeenCalledWith('Привет')
    expect(onSources).toHaveBeenCalledWith([expect.objectContaining({ path: 'src/app.py', lineStart: 12 })])
    expect(onDone).toHaveBeenCalledOnce()
  })
})
