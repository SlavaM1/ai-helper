from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReviewSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Severity(str, enum.Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class FindingCategory(str, enum.Enum):
    POTENTIAL_BUG = "potential_bug"
    ARCHITECTURE_ISSUE = "architecture_issue"
    RECOMMENDATION = "recommendation"


class PullRequestFile(ReviewSchema):
    filename: str
    previous_filename: str | None = None
    status: str
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    patch: str | None = None
    blob_url: str | None = None
    raw_url: str | None = None

    @property
    def is_binary_or_unavailable(self) -> bool:
        return self.patch is None


class PullRequestData(ReviewSchema):
    repository: str
    number: int = Field(gt=0)
    title: str
    description: str = ""
    author: str
    base_branch: str
    head_branch: str
    base_sha: str
    head_sha: str
    state: str
    draft: bool = False
    html_url: str
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    updated_at: datetime | None = None
    files: list[PullRequestFile] = Field(default_factory=list)


class ReviewFinding(ReviewSchema):
    id: str = ""
    title: str = Field(min_length=1, max_length=300)
    category: FindingCategory
    severity: Severity
    description: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    filename: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    suggested_fix: str = Field(min_length=1)
    permalink: str | None = None
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_lines(self) -> ReviewFinding:
        if self.line_end is not None and self.line_start is None:
            self.line_start = self.line_end
        if self.line_start is not None and self.line_end is None:
            self.line_end = self.line_start
        if self.line_start and self.line_end and self.line_end < self.line_start:
            raise ValueError("line_end must be greater than or equal to line_start")
        return self


class LlmReviewResult(ReviewSchema):
    summary: str = Field(min_length=1)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=50)

    @field_validator("findings")
    @classmethod
    def unique_findings(cls, findings: list[ReviewFinding]) -> list[ReviewFinding]:
        unique: list[ReviewFinding] = []
        seen: set[tuple[str, str | None, int | None]] = set()
        for finding in findings:
            key = (finding.title.casefold().strip(), finding.filename, finding.line_start)
            if key not in seen:
                seen.add(key)
                unique.append(finding)
        return unique


class ReviewSourceData(ReviewSchema):
    path: str
    start_line: int | None = None
    end_line: int | None = None
    commit_sha: str | None = None
    url: str | None = None
    score: float | None = None
    content: str = ""


class ReviewReport(ReviewSchema):
    schema_version: int = 1
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    model: str
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    sources: list[ReviewSourceData] = Field(default_factory=list)
    files_analyzed: int = 0
    files_total: int = 0
    analysis_completeness: str = "complete"
    unreviewed_files: list[str] = Field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    aggregation_tokens: int | None = None
    total_tokens: int | None = None
    elapsed_ms: int | None = None
    generated_at: datetime


class ReviewRead(ReviewSchema):
    id: str
    pull_request_id: str
    source: str
    external_id: str | None
    reviewed_head_sha: str
    base_sha: str
    model: str
    status: str
    summary: str | None
    analysis_completeness: str
    files_analyzed: int
    files_total: int
    unreviewed_files: list[str]
    prompt_tokens: int | None
    completion_tokens: int | None
    aggregation_tokens: int | None
    total_tokens: int | None
    elapsed_ms: int | None
    error: str | None
    github_comment_id: int | None
    github_comment_url: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    repository: str | None = None
    pr_number: int | None = None
    pr_title: str | None = None
    html_url: str | None = None
    findings: list[ReviewFinding] = Field(default_factory=list)
    sources: list[ReviewSourceData] = Field(default_factory=list)


class PullRequestRead(ReviewSchema):
    id: str
    repository: str
    number: int
    title: str
    author: str
    base_branch: str
    head_branch: str
    base_sha: str
    head_sha: str
    state: str
    html_url: str
    github_updated_at: datetime | None
    updated_at: datetime
    review_status: str = "not_reviewed"
    last_review: ReviewRead | None = None
