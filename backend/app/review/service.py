from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.crud import _commit
from app.models import (
    CodeReview,
    ConversationReview,
    PullRequest,
    ReviewRun,
    ReviewSource,
    utc_now,
)
from app.models import ReviewFinding as ReviewFindingModel

from .github import GitHubPRClient, decode_metadata
from .schemas import (
    PullRequestData,
    ReviewFinding,
    ReviewRead,
    ReviewReport,
    ReviewSourceData,
)

logger = logging.getLogger(__name__)


class ReviewNotFoundError(LookupError):
    pass


class ActiveReviewError(RuntimeError):
    pass


class ReviewHistoryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert_pull_request(self, data: PullRequestData) -> PullRequest:
        item = self.db.scalar(
            select(PullRequest).where(
                PullRequest.repository == data.repository, PullRequest.number == data.number
            )
        )
        if item is None:
            item = PullRequest(repository=data.repository, number=data.number)
            self.db.add(item)
        for field, value in {
            "title": data.title,
            "description": data.description,
            "author": data.author,
            "base_branch": data.base_branch,
            "head_branch": data.head_branch,
            "base_sha": data.base_sha,
            "head_sha": data.head_sha,
            "state": data.state,
            "html_url": data.html_url,
            "additions": data.additions,
            "deletions": data.deletions,
            "changed_files": data.changed_files,
            "github_updated_at": data.updated_at,
            "synced_at": utc_now(),
        }.items():
            setattr(item, field, value)
        _commit(self.db)
        self.db.refresh(item)
        return item

    def create_review(
        self,
        pull_request: PullRequest,
        *,
        source: str,
        model: str,
        external_id: str | None = None,
    ) -> CodeReview:
        if external_id:
            existing = self.db.scalar(select(CodeReview).where(CodeReview.external_id == external_id))
            if existing:
                return existing
        active = self.db.scalar(
            select(CodeReview).where(
                CodeReview.pull_request_id == pull_request.id,
                CodeReview.reviewed_head_sha == pull_request.head_sha,
                CodeReview.status.in_(("pending", "running")),
            )
        )
        if active:
            raise ActiveReviewError(active.id)
        review = CodeReview(
            pull_request_id=pull_request.id,
            source=source,
            external_id=external_id,
            reviewed_head_sha=pull_request.head_sha,
            base_sha=pull_request.base_sha,
            model=model,
            status="pending",
            files_total=pull_request.changed_files,
        )
        self.db.add(review)
        _commit(self.db)
        self.db.refresh(review)
        return review

    def mark_running(self, review_id: str) -> CodeReview:
        review = self.require(review_id)
        review.status = "running"
        review.started_at = utc_now()
        self.db.add(ReviewRun(review_id=review.id, stage="analysis", status="running"))
        _commit(self.db)
        return review

    def complete(
        self,
        review_id: str,
        report: ReviewReport,
        *,
        github_comment: dict[str, Any] | None = None,
    ) -> CodeReview:
        review = self.require(review_id)
        self.db.execute(delete(ReviewFindingModel).where(ReviewFindingModel.review_id == review.id))
        self.db.execute(delete(ReviewSource).where(ReviewSource.review_id == review.id))
        review.status = "completed"
        review.summary = report.summary
        review.analysis_completeness = report.analysis_completeness
        review.files_analyzed = report.files_analyzed
        review.files_total = report.files_total
        review.unreviewed_files = report.unreviewed_files
        review.prompt_tokens = report.prompt_tokens
        review.completion_tokens = report.completion_tokens
        review.aggregation_tokens = report.aggregation_tokens
        review.total_tokens = report.total_tokens
        review.elapsed_ms = report.elapsed_ms
        review.finished_at = report.generated_at
        review.error = None
        if github_comment:
            review.github_comment_id = int(github_comment["id"])
            review.github_comment_url = github_comment.get("html_url")
        for item in report.findings:
            self.db.add(
                ReviewFindingModel(
                    id=item.id,
                    review_id=review.id,
                    title=item.title,
                    category=item.category.value,
                    severity=item.severity.value,
                    description=item.description,
                    impact=item.impact,
                    evidence=item.evidence,
                    filename=item.filename,
                    line_start=item.line_start,
                    line_end=item.line_end,
                    suggested_fix=item.suggested_fix,
                    permalink=item.permalink,
                    confidence=item.confidence,
                )
            )
        for source in report.sources:
            self.db.add(
                ReviewSource(
                    review_id=review.id,
                    path=source.path,
                    start_line=source.start_line,
                    end_line=source.end_line,
                    commit_sha=source.commit_sha,
                    url=source.url,
                    score=source.score,
                )
            )
        running = self.db.scalar(
            select(ReviewRun).where(
                ReviewRun.review_id == review.id, ReviewRun.status == "running"
            )
        )
        if running:
            running.status = "completed"
            running.prompt_tokens = report.prompt_tokens
            running.completion_tokens = report.completion_tokens
            running.elapsed_ms = report.elapsed_ms
        _commit(self.db)
        return self.require(review.id)

    def fail(self, review_id: str, error: str) -> CodeReview:
        review = self.require(review_id)
        review.status = "failed"
        review.error = error[:2_000]
        review.finished_at = utc_now()
        running = self.db.scalar(
            select(ReviewRun).where(
                ReviewRun.review_id == review.id, ReviewRun.status == "running"
            )
        )
        if running:
            running.status = "failed"
            running.error = error[:2_000]
        _commit(self.db)
        return review

    def get(self, review_id: str) -> CodeReview | None:
        return self.db.scalar(
            select(CodeReview)
            .execution_options(populate_existing=True)
            .options(
                selectinload(CodeReview.pull_request),
                selectinload(CodeReview.findings),
                selectinload(CodeReview.sources),
            )
            .where(CodeReview.id == review_id)
        )

    def require(self, review_id: str) -> CodeReview:
        review = self.get(review_id)
        if review is None:
            raise ReviewNotFoundError(review_id)
        return review

    def list(self, limit: int = 100) -> Sequence[CodeReview]:
        return self.db.scalars(
            select(CodeReview)
            .options(
                selectinload(CodeReview.pull_request),
                selectinload(CodeReview.findings),
                selectinload(CodeReview.sources),
            )
            .order_by(CodeReview.created_at.desc())
            .limit(limit)
        ).all()

    def list_pull_requests(self) -> Sequence[PullRequest]:
        return self.db.scalars(
            select(PullRequest)
            .options(selectinload(PullRequest.reviews).selectinload(CodeReview.findings))
            .order_by(PullRequest.github_updated_at.desc(), PullRequest.number.desc())
        ).all()

    def link_conversation(self, conversation_id: str, review_id: str) -> ConversationReview:
        link = self.db.scalar(
            select(ConversationReview).where(ConversationReview.conversation_id == conversation_id)
        )
        if link is None:
            link = ConversationReview(conversation_id=conversation_id, review_id=review_id)
            self.db.add(link)
        else:
            link.review_id = review_id
        _commit(self.db)
        return link

    def conversation_review(self, conversation_id: str) -> CodeReview | None:
        link = self.db.scalar(
            select(ConversationReview).where(ConversationReview.conversation_id == conversation_id)
        )
        return self.get(link.review_id) if link else None

    @staticmethod
    def serialize(review: CodeReview) -> dict[str, Any]:
        pr = review.pull_request
        return ReviewRead(
            id=review.id,
            pull_request_id=review.pull_request_id,
            source=review.source,
            external_id=review.external_id,
            reviewed_head_sha=review.reviewed_head_sha,
            base_sha=review.base_sha,
            model=review.model,
            status=review.status,
            summary=review.summary,
            analysis_completeness=review.analysis_completeness,
            files_analyzed=review.files_analyzed,
            files_total=review.files_total,
            unreviewed_files=review.unreviewed_files or [],
            prompt_tokens=review.prompt_tokens,
            completion_tokens=review.completion_tokens,
            aggregation_tokens=review.aggregation_tokens,
            total_tokens=review.total_tokens,
            elapsed_ms=review.elapsed_ms,
            error=review.error,
            github_comment_id=review.github_comment_id,
            github_comment_url=review.github_comment_url,
            created_at=review.created_at,
            started_at=review.started_at,
            finished_at=review.finished_at,
            repository=pr.repository if pr else None,
            pr_number=pr.number if pr else None,
            pr_title=pr.title if pr else None,
            html_url=pr.html_url if pr else None,
            findings=[ReviewFinding.model_validate(item) for item in review.findings],
            sources=[
                ReviewSourceData(
                    path=item.path,
                    start_line=item.start_line,
                    end_line=item.end_line,
                    commit_sha=item.commit_sha,
                    url=item.url,
                    score=item.score,
                )
                for item in review.sources
            ],
        ).model_dump(mode="json")

    def report(self, review_id: str) -> ReviewReport:
        review = self.require(review_id)
        pr = review.pull_request
        return ReviewReport(
            repository=pr.repository,
            pr_number=pr.number,
            base_sha=review.base_sha,
            head_sha=review.reviewed_head_sha,
            model=review.model,
            summary=review.summary or "Review is not complete.",
            findings=[ReviewFinding.model_validate(item) for item in review.findings],
            sources=[
                ReviewSourceData(
                    path=item.path,
                    start_line=item.start_line,
                    end_line=item.end_line,
                    commit_sha=item.commit_sha,
                    url=item.url,
                    score=item.score,
                )
                for item in review.sources
            ],
            files_analyzed=review.files_analyzed,
            files_total=review.files_total,
            analysis_completeness=review.analysis_completeness,
            unreviewed_files=review.unreviewed_files or [],
            prompt_tokens=review.prompt_tokens,
            completion_tokens=review.completion_tokens,
            aggregation_tokens=review.aggregation_tokens,
            total_tokens=review.total_tokens,
            elapsed_ms=review.elapsed_ms,
            generated_at=review.finished_at or datetime.now(UTC),
        )


class ReviewSyncService:
    def __init__(self, db: Session, github: GitHubPRClient) -> None:
        self.db = db
        self.github = github
        self.history = ReviewHistoryService(db)

    async def sync(self) -> dict[str, Any]:
        pull_requests = await self.github.list_pull_requests("all")
        synced_reviews = 0
        for data in pull_requests:
            pr = self.history.upsert_pull_request(data)
            comments = await self.github.list_comments(data.number)
            for comment in comments:
                metadata = decode_metadata(str(comment.get("body", "")))
                if metadata is None:
                    continue
                try:
                    report = ReviewReport.model_validate(metadata)
                except Exception as exc:
                    logger.warning("Ignoring invalid AI review metadata on comment %s: %s", comment.get("id"), exc)
                    continue
                if report.repository != data.repository or report.pr_number != data.number:
                    continue
                external_id = f"github:{comment.get('id')}:{report.head_sha}"
                try:
                    review = self.history.create_review(
                        pr, source="github_action", model=report.model, external_id=external_id
                    )
                except ActiveReviewError:
                    continue
                if review.status != "completed":
                    self.history.complete(review.id, report, github_comment=comment)
                    synced_reviews += 1
        return {"pull_requests": len(pull_requests), "synced_reviews": synced_reviews, "synced_at": utc_now()}
