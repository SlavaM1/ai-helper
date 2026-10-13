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

  it('нормализует pull requests и вложенный последний review', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([{
      number: 42,
      title: 'Protect webhook verification',
      author: { login: 'ada' },
      head_branch: 'fix/webhooks',
      base_branch: 'main',
      state: 'open',
      updated_at: '2026-10-13T10:00:00Z',
      html_url: 'https://github.com/acme/repo/pull/42',
      head_sha: 'abcdef123456',
      review_status: 'completed',
      last_review: { id: 'review-42', pr_number: 42, status: 'completed', findings_count: 2 },
    }]))
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.getPullRequests()).resolves.toEqual([
      expect.objectContaining({
        number: 42,
        author: 'ada',
        headBranch: 'fix/webhooks',
        baseBranch: 'main',
        reviewStatus: 'completed',
        lastReview: expect.objectContaining({ id: 'review-42', pullRequestNumber: 42, findingsCount: 2 }),
      }),
    ])
    expect(fetchMock).toHaveBeenCalledWith('/api/pull-requests', expect.any(Object))
  })

  it('нормализует findings и sources подробного review', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      review_id: 'review/42',
      pull_request_number: 42,
      status: 'completed',
      findings: [{
        finding_id: 'f-1',
        title: 'Unchecked signature',
        description: 'Verify before parsing.',
        severity: 'HIGH',
        category: 'security',
        file_path: 'src/webhook.ts',
        line_start: 18,
        recommendation: 'Validate the HMAC first.',
      }],
      sources: [{ file_path: 'src/webhook.ts', start_line: 18, github_url: 'https://github.com/acme/repo/blob/main/src/webhook.ts#L18' }],
    })))

    await expect(api.getReview('review/42')).resolves.toMatchObject({
      id: 'review/42',
      findings: [{ id: 'f-1', severity: 'high', category: 'security', filePath: 'src/webhook.ts', lineStart: 18, suggestion: 'Validate the HMAC first.' }],
      sources: [{ path: 'src/webhook.ts', lineStart: 18 }],
    })
    expect(fetch).toHaveBeenCalledWith('/api/reviews/review%2F42', expect.any(Object))
  })

  it('вызывает review actions по контрактным endpoints', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ id: 'r-1', pr_number: 7, status: 'pending' }))
      .mockResolvedValueOnce(jsonResponse({ synced: 3 }))
      .mockResolvedValueOnce(jsonResponse({ conversation_id: 'c-review', review_id: 'r-1' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.runPullRequestReview(7)).resolves.toMatchObject({ id: 'r-1', status: 'pending' })
    await expect(api.syncReviews()).resolves.toMatchObject({ synced: 3 })
    await expect(api.discussReview('r-1')).resolves.toEqual({ conversationId: 'c-review', reviewId: 'r-1' })
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/pull-requests/7/review',
      '/api/reviews/sync',
      '/api/reviews/r-1/discuss',
    ])
    expect(fetchMock.mock.calls.every(([, init]) => init.method === 'POST')).toBe(true)
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
