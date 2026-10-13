import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import { CodeReviews } from './CodeReviews'

vi.mock('../api', () => ({
  api: {
    getPullRequests: vi.fn(),
    getReviews: vi.fn(),
    getReview: vi.fn(),
    runPullRequestReview: vi.fn(),
    syncReviews: vi.fn(),
    discussReview: vi.fn(),
  },
}))

const pullRequest = {
  number: 42,
  title: 'Protect webhook verification',
  author: 'ada',
  headBranch: 'fix/webhooks',
  baseBranch: 'main',
  state: 'open',
  headSha: 'abcdef123456',
  htmlUrl: 'https://github.com/acme/repo/pull/42',
  reviewStatus: 'completed',
}

const summary = {
  id: 'review-42',
  pullRequestNumber: 42,
  status: 'completed',
  findingsCount: 2,
}

const review = {
  ...summary,
  summary: 'One security issue and one maintainability issue.',
  findings: [
    { id: 'f-1', title: 'Unchecked signature', description: 'Verify the signature first.', severity: 'high', category: 'security', filePath: 'src/webhook.ts', lineStart: 18 },
    { id: 'f-2', title: 'Repeated parser', description: 'Reuse the parser.', severity: 'low', category: 'maintainability', filePath: 'src/parser.ts', lineStart: 9 },
  ],
  sources: [],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.getPullRequests).mockResolvedValue([pullRequest])
  vi.mocked(api.getReviews).mockResolvedValue([summary])
  vi.mocked(api.getReview).mockResolvedValue(review)
  vi.mocked(api.runPullRequestReview).mockResolvedValue({ ...review, status: 'pending', findings: [], sources: [] })
  vi.mocked(api.syncReviews).mockResolvedValue({ synced: 1 })
  vi.mocked(api.discussReview).mockResolvedValue({ conversationId: 'conversation-42', reviewId: 'review-42' })
})

describe('CodeReviews', () => {
  it('refreshes, syncs and re-runs a pull request review', async () => {
    render(<CodeReviews onMenu={vi.fn()} onDiscuss={vi.fn()} />)
    expect(await screen.findByText('Protect webhook verification')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Refresh PRs' }))
    expect(api.getPullRequests).toHaveBeenCalledTimes(2)

    await userEvent.click(screen.getByRole('button', { name: 'Sync' }))
    await waitFor(() => expect(api.syncReviews).toHaveBeenCalledOnce())

    await userEvent.click(screen.getByRole('button', { name: 'Re-run' }))
    expect(api.runPullRequestReview).toHaveBeenCalledWith(42)
  })

  it('opens a report and filters findings by severity and category', async () => {
    render(<CodeReviews onMenu={vi.fn()} onDiscuss={vi.fn()} />)
    await userEvent.click(await screen.findByRole('button', { name: 'View report' }))

    expect(await screen.findByText('Unchecked signature')).toBeInTheDocument()
    expect(screen.getByText('Repeated parser')).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Filter by severity' }), 'low')
    expect(screen.queryByText('Unchecked signature')).not.toBeInTheDocument()
    expect(screen.getByText('Repeated parser')).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Filter by category' }), 'security')
    expect(screen.getByText('No findings match these filters.')).toBeInTheDocument()
  })

  it('links a review discussion to the returned conversation', async () => {
    const onDiscuss = vi.fn()
    render(<CodeReviews onMenu={vi.fn()} onDiscuss={onDiscuss} />)
    await userEvent.click(await screen.findByRole('button', { name: 'View report' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Discuss in Chat' }))

    expect(api.discussReview).toHaveBeenCalledWith('review-42')
    expect(onDiscuss).toHaveBeenCalledWith('conversation-42')
  })
})
