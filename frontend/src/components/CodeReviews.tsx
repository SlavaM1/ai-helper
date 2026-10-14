import { useEffect, useState } from 'react'
import { api } from '../api'
import type { CodeReview, PullRequest, ReviewFinding, ReviewSummary } from '../types'
import { AlertIcon, BranchIcon, ExternalIcon, FilterIcon, MenuIcon, RefreshIcon, ReviewIcon } from './Icons'

const ACTIVE_STATUSES = new Set(['pending', 'running'])

function isActive(status?: string) {
  return ACTIVE_STATUSES.has(status?.toLowerCase() ?? '')
}

function formatDate(value?: string) {
  if (!value) return 'No activity yet'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function shortSha(value?: string) {
  return value ? value.slice(0, 7) : '-------'
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : 'Unknown API error'
}

function statusLabel(status?: string) {
  switch (status?.toLowerCase()) {
    case 'pending': return 'Queued'
    case 'running': return 'Reviewing'
    case 'completed': case 'complete': case 'success': return 'Complete'
    case 'failed': case 'error': return 'Failed'
    case 'outdated': return 'Outdated'
    default: return 'Not reviewed'
  }
}

function findingLocation(finding: ReviewFinding) {
  if (!finding.filePath) return undefined
  if (!finding.lineStart) return finding.filePath
  return `${finding.filePath}:${finding.lineStart}${finding.lineEnd && finding.lineEnd !== finding.lineStart ? `-${finding.lineEnd}` : ''}`
}

interface Props {
  onMenu: () => void
  onDiscuss: (conversationId: string) => void
}

export function CodeReviews({ onMenu, onDiscuss }: Props) {
  const [pullRequests, setPullRequests] = useState<PullRequest[]>([])
  const [reviews, setReviews] = useState<ReviewSummary[]>([])
  const [selectedReview, setSelectedReview] = useState<CodeReview>()
  const [selectedReviewId, setSelectedReviewId] = useState<string>()
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [runningNumber, setRunningNumber] = useState<number>()
  const [discussing, setDiscussing] = useState(false)
  const [loadingReport, setLoadingReport] = useState(false)
  const [severity, setSeverity] = useState('all')
  const [category, setCategory] = useState('all')
  const [notice, setNotice] = useState<string>()
  const [error, setError] = useState<string>()

  const loadLists = async (showLoading = false) => {
    if (showLoading) setLoading(true)
    try {
      const [nextPullRequests, nextReviews] = await Promise.all([api.getPullRequests(), api.getReviews()])
      setPullRequests(nextPullRequests)
      setReviews(nextReviews)
      setError(undefined)
    } catch (reason) {
      setError(`Could not load code reviews: ${errorText(reason)}`)
    } finally {
      if (showLoading) setLoading(false)
    }
  }

  useEffect(() => {
    void loadLists(true)
    const timer = window.setInterval(() => { void loadLists() }, 60_000)
    return () => window.clearInterval(timer)
  }, [])

  const hasActiveReview = reviews.some((review) => isActive(review.status))
    || pullRequests.some((pullRequest) => isActive(pullRequest.reviewStatus))
    || isActive(selectedReview?.status)

  useEffect(() => {
    if (!hasActiveReview) return
    const timer = window.setInterval(() => {
      void Promise.all([
        api.getPullRequests().then(setPullRequests),
        api.getReviews().then(setReviews),
        selectedReviewId ? api.getReview(selectedReviewId).then(setSelectedReview) : Promise.resolve(),
      ]).catch((reason) => setError(`Could not refresh active review: ${errorText(reason)}`))
    }, 2_500)
    return () => window.clearInterval(timer)
  }, [hasActiveReview, selectedReviewId])

  const latestReview = (pullRequest: PullRequest) => pullRequest.lastReview
    ?? reviews.find((review) => review.pullRequestNumber === pullRequest.number)

  const refreshPullRequests = async () => {
    setRefreshing(true)
    try {
      setPullRequests(await api.getPullRequests())
      setNotice('Pull requests refreshed')
      setError(undefined)
    } catch (reason) {
      setError(`Could not refresh pull requests: ${errorText(reason)}`)
    } finally {
      setRefreshing(false)
    }
  }

  const sync = async () => {
    setSyncing(true)
    try {
      const result = await api.syncReviews()
      await loadLists()
      const syncMessage = typeof result.synced === 'number'
        ? `${result.synced} review${result.synced === 1 ? '' : 's'} synced`
        : result.synced ? 'Reviews synced' : 'Reviews are already up to date'
      setNotice(result.message ?? syncMessage)
    } catch (reason) {
      setError(`Could not sync reviews: ${errorText(reason)}`)
    } finally {
      setSyncing(false)
    }
  }

  const runReview = async (pullRequest: PullRequest) => {
    setRunningNumber(pullRequest.number)
    try {
      const review = await api.runPullRequestReview(pullRequest.number)
      setReviews((current) => [review, ...current.filter((item) => item.id !== review.id)])
      setPullRequests((current) => current.map((item) => item.number === pullRequest.number
        ? { ...item, reviewStatus: review.status, lastReview: review }
        : item))
      setNotice(`Review for PR #${pullRequest.number} queued`)
      setError(undefined)
    } catch (reason) {
      setError(`Could not start review for PR #${pullRequest.number}: ${errorText(reason)}`)
    } finally {
      setRunningNumber(undefined)
    }
  }

  const openReport = async (review: ReviewSummary) => {
    setSelectedReviewId(review.id)
    setSelectedReview({ ...review, findings: [], sources: [] })
    setLoadingReport(true)
    setSeverity('all')
    setCategory('all')
    try {
      setSelectedReview(await api.getReview(review.id))
      setError(undefined)
    } catch (reason) {
      setError(`Could not load review report: ${errorText(reason)}`)
    } finally {
      setLoadingReport(false)
    }
  }

  const discuss = async () => {
    if (!selectedReview) return
    setDiscussing(true)
    try {
      const result = await api.discussReview(selectedReview.id)
      if (!result.conversationId) throw new Error('Server did not return a conversation')
      onDiscuss(result.conversationId)
    } catch (reason) {
      setError(`Could not open review discussion: ${errorText(reason)}`)
    } finally {
      setDiscussing(false)
    }
  }

  const severities = selectedReview ? [...new Set(selectedReview.findings.map((finding) => finding.severity))] : []
  const categories = selectedReview ? [...new Set(selectedReview.findings.map((finding) => finding.category))] : []
  const visibleFindings = selectedReview?.findings.filter((finding) => (
    (severity === 'all' || finding.severity === severity)
    && (category === 'all' || finding.category === category)
  )) ?? []
  const selectedPullRequest = pullRequests.find((item) => item.number === selectedReview?.pullRequestNumber)

  return (
    <div className="reviews-workspace">
      <header className="reviews-topbar">
        <button className="icon-button menu-button" onClick={onMenu} aria-label="Open navigation"><MenuIcon /></button>
        <div className="reviews-title"><ReviewIcon /><div><strong>Code Reviews</strong><span>Pull request intelligence</span></div></div>
        {!selectedReviewId && (
          <div className="reviews-actions">
            <button type="button" onClick={() => void refreshPullRequests()} disabled={refreshing}><RefreshIcon />{refreshing ? 'Refreshing' : 'Refresh PRs'}</button>
            <button className="reviews-actions__primary" type="button" onClick={() => void sync()} disabled={syncing}><ReviewIcon />{syncing ? 'Syncing' : 'Sync'}</button>
          </div>
        )}
      </header>

      {error && <div className="error-banner" role="alert"><AlertIcon /><span>{error}</span><button type="button" onClick={() => setError(undefined)}>Close</button></div>}
      {notice && <div className="review-notice" role="status"><span>{notice}</span><button type="button" onClick={() => setNotice(undefined)}>Dismiss</button></div>}

      {selectedReviewId && selectedReview ? (
        <section className="review-detail">
          <div className="report-heading">
            <button className="report-back" type="button" onClick={() => { setSelectedReviewId(undefined); setSelectedReview(undefined) }}>Back to pull requests</button>
            <div className="report-heading__main">
              <div>
                <span className={`review-status review-status--${selectedReview.status.toLowerCase()}`}>{statusLabel(selectedReview.status)}</span>
                <h1>Review report <span>#{selectedReview.pullRequestNumber}</span></h1>
                <p>{selectedPullRequest?.title ?? 'Pull request review'}</p>
              </div>
              <div className="report-actions">
                {selectedPullRequest?.htmlUrl && <a href={selectedPullRequest.htmlUrl} target="_blank" rel="noreferrer"><ExternalIcon />Open GitHub</a>}
                <button type="button" onClick={() => void discuss()} disabled={discussing}><span className="discussion-node" />{discussing ? 'Opening chat' : 'Discuss in Chat'}</button>
              </div>
            </div>
            <div className="report-meta">
              <span><b>Commit</b><code>{shortSha(selectedReview.headSha)}</code></span>
              <span><b>Findings</b><strong>{selectedReview.findings.length}</strong></span>
              <span><b>Updated</b>{formatDate(selectedReview.completedAt ?? selectedReview.updatedAt)}</span>
            </div>
          </div>

          {selectedReview.summary && <div className="report-summary"><span>Assessment</span><p>{selectedReview.summary}</p></div>}

          <div className="finding-toolbar">
            <div><FilterIcon /><strong>Findings</strong><span>{visibleFindings.length} of {selectedReview.findings.length}</span></div>
            <label>Severity<select aria-label="Filter by severity" value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="all">All severities</option>{severities.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
            <label>Category<select aria-label="Filter by category" value={category} onChange={(event) => setCategory(event.target.value)}><option value="all">All categories</option>{categories.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
          </div>

          <div className="finding-list">
            {visibleFindings.map((finding) => (
              <article className={`finding finding--${finding.severity}`} key={finding.id}>
                <div className="finding__rail"><span /></div>
                <div className="finding__body">
                  <header><span className="severity-chip">{finding.severity}</span><span className="category-chip">{finding.category}</span>{findingLocation(finding) && <code>{findingLocation(finding)}</code>}</header>
                  <h2>{finding.title}</h2>
                  {finding.description && <p>{finding.description}</p>}
                  {finding.impact && <p><strong>Impact:</strong> {finding.impact}</p>}
                  {finding.evidence && <p><strong>Evidence:</strong> {finding.evidence}</p>}
                  {finding.codeSnippet && <pre><code>{finding.codeSnippet}</code></pre>}
                  {finding.suggestion && <div className="finding__suggestion"><span>Suggested change</span><p>{finding.suggestion}</p></div>}
                  {finding.permalink && <a className="finding__source" href={finding.permalink} target="_blank" rel="noreferrer"><ExternalIcon />Open source</a>}
                </div>
              </article>
            ))}
            {loadingReport ? (
              <div className="report-empty">Loading review report...</div>
            ) : !visibleFindings.length && (
              <div className="report-empty">
                {isActive(selectedReview.status) ? 'The review is still running. Findings will appear here.' : selectedReview.findings.length ? 'No findings match these filters.' : 'No findings were reported.'}
              </div>
            )}
          </div>

          {selectedReview.sources.length > 0 && (
            <section className="review-sources"><h2>Sources</h2><div>{selectedReview.sources.map((source, index) => source.url
              ? <a href={source.url} target="_blank" rel="noreferrer" key={`${source.path}-${index}`}><ExternalIcon />{source.path}{source.lineStart ? `:${source.lineStart}` : ''}</a>
              : <span key={`${source.path}-${index}`}>{source.path}{source.lineStart ? `:${source.lineStart}` : ''}</span>)}</div></section>
          )}
        </section>
      ) : (
        <section className="reviews-list-view">
          <div className="reviews-intro">
            <div><span>Review queue</span><h1>Pull requests</h1><p>Run an AI review against the latest head commit, publish it to GitHub, then inspect and discuss each finding.</p></div>
            <div className="reviews-count"><strong>{pullRequests.length}</strong><span>tracked</span></div>
          </div>

          {loading ? (
            <div className="chat-loading"><i /><i /><i /></div>
          ) : pullRequests.length === 0 ? (
            <div className="reviews-empty"><ReviewIcon /><h2>No pull requests found</h2><p>Refresh the GitHub list or sync existing review records.</p></div>
          ) : (
            <div className="pull-request-list">
              {pullRequests.map((pullRequest) => {
                const review = latestReview(pullRequest)
                const status = pullRequest.reviewStatus ?? review?.status
                const active = isActive(status)
                return (
                  <article className="pull-request" key={pullRequest.number}>
                    <div className="pull-request__number"><span>PR</span><strong>#{pullRequest.number}</strong></div>
                    <div className="pull-request__content">
                      <div className="pull-request__title"><h2>{pullRequest.title}</h2><span className={`review-status review-status--${status?.toLowerCase() ?? 'none'}`}>{active && <i />}{statusLabel(status)}</span></div>
                      <div className="pull-request__meta"><span>@{pullRequest.author}</span><span><BranchIcon />{pullRequest.headBranch || 'head'} <b>into</b> {pullRequest.baseBranch || 'base'}</span><code>{shortSha(pullRequest.headSha)}</code><time>{formatDate(pullRequest.updatedAt)}</time></div>
                      {review && <div className="pull-request__metrics"><span>{review.findingsCount ?? 0} findings</span>{['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].map((level) => review.severityCounts?.[level] ? <span key={level}>{level} {review.severityCounts[level]}</span> : null)}{review.model && <code>{review.model}</code>}{review.elapsedMs != null && <span>{review.elapsedMs} ms</span>}</div>}
                    </div>
                    <div className="pull-request__actions">
                      {pullRequest.htmlUrl && <a href={pullRequest.htmlUrl} target="_blank" rel="noreferrer"><ExternalIcon />Open GitHub</a>}
                      {review && <button type="button" onClick={() => void openReport(review)}>View report</button>}
                      <button className="run-review" type="button" onClick={() => void runReview(pullRequest)} disabled={active || runningNumber === pullRequest.number}>{runningNumber === pullRequest.number ? 'Starting' : review ? 'Re-run' : 'Run review'}</button>
                    </div>
                  </article>
                )
              })}
            </div>
          )}
        </section>
      )}
    </div>
  )
}
