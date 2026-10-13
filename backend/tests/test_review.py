from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from app.config import Settings
from app.review.engine import (
    REVIEW_SYSTEM_PROMPT,
    CodeReviewAgent,
    PullRequestDiffService,
    ReviewContextBuilder,
    parse_review_command,
)
from app.review.formatter import ReviewReportFormatter
from app.review.github import (
    GitHubPRClient,
    GitHubReviewPublisher,
    decode_metadata,
    encode_metadata,
)
from app.review.schemas import (
    FindingCategory,
    PullRequestData,
    PullRequestFile,
    ReviewFinding,
    ReviewReport,
    Severity,
)
from app.review.service import ReviewHistoryService, ReviewSyncService


def make_pr(*files: PullRequestFile) -> PullRequestData:
    return PullRequestData(
        repository="SlavaM1/aurelSpa",
        number=15,
        title="Change order service",
        author="developer",
        base_branch="main",
        head_branch="feature/orders",
        base_sha="a" * 40,
        head_sha="b" * 40,
        state="open",
        html_url="https://github.com/SlavaM1/aurelSpa/pull/15",
        changed_files=len(files),
        files=list(files),
    )


def make_report() -> ReviewReport:
    return ReviewReport(
        repository="SlavaM1/aurelSpa",
        pr_number=15,
        base_sha="a" * 40,
        head_sha="b" * 40,
        model="deepseek-flash",
        summary="One supported issue.",
        findings=[
            ReviewFinding(
                id="AI-1",
                title="Missing null check",
                category=FindingCategory.POTENTIAL_BUG,
                severity=Severity.HIGH,
                description="The changed code dereferences a nullable value.",
                impact="Request failure.",
                evidence="The added line invokes the value directly.",
                filename="src/OrderService.java",
                line_start=11,
                line_end=11,
                suggested_fix="Validate before use.",
                confidence=0.9,
            )
        ],
        files_analyzed=1,
        files_total=1,
        generated_at=datetime.now(UTC),
    )


def test_diff_lines_and_unavailable_files_are_explicit() -> None:
    patch = "@@ -10,2 +10,3 @@\n same\n-old\n+new\n+more"
    assert PullRequestDiffService.changed_lines(patch) == {11, 12}
    context = ReviewContextBuilder().build(
        make_pr(
            PullRequestFile(filename="src/OrderService.java", status="modified", patch=patch),
            PullRequestFile(filename="image.png", status="modified", patch=None),
            PullRequestFile(
                filename="src/New.java",
                previous_filename="src/Old.java",
                status="renamed",
                patch="@@ -1 +1 @@\n-old\n+new",
            ),
        )
    )
    assert context.files_analyzed == 2
    assert context.unreviewed_files == ["image.png"]
    assert context.changed_lines["src/New.java"] == {1}


def test_review_command_accepts_number_and_allowed_url_only() -> None:
    assert parse_review_command("/review 15") == 15
    assert parse_review_command("/review https://github.com/SlavaM1/aurelSpa/pull/15") == 15
    assert parse_review_command("/review https://github.com/other/repo/pull/15") is None
    assert parse_review_command("/review nope") is None


def test_github_files_pagination_and_metadata() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/15"):
            return httpx.Response(
                200,
                json={
                    "number": 15,
                    "title": "PR",
                    "user": {"login": "dev"},
                    "base": {"ref": "main", "sha": "a" * 40},
                    "head": {"ref": "feature", "sha": "b" * 40},
                    "state": "open",
                    "html_url": "https://example.test/pr/15",
                    "changed_files": 101,
                },
            )
        page = int(request.url.params["page"])
        calls.append(page)
        count = 100 if page == 1 else 1
        return httpx.Response(
            200,
            json=[
                {
                    "filename": f"file-{page}-{index}.java",
                    "status": "added" if index else "renamed",
                    "previous_filename": "old.java" if index == 0 else None,
                    "patch": None if index == 1 else "@@ -0,0 +1 @@\n+code",
                }
                for index in range(count)
            ],
        )

    async def exercise() -> None:
        http = httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(handler)
        )
        client = GitHubPRClient("token", "SlavaM1/aurelSpa", client=http)
        pr = await client.get_pull_request(15)
        assert len(pr.files) == 101
        assert pr.files[0].previous_filename == "old.java"
        assert pr.files[1].is_binary_or_unavailable
        assert calls == [1, 2]
        await http.aclose()

    asyncio.run(exercise())


class FakeRag:
    def retrieve(self, pull_request):
        return [], None


class FakeLlm:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    async def complete(self, messages, *, model, max_tokens):
        response = self.responses[self.calls]
        self.calls += 1
        return response, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_engine_retries_invalid_json_and_validates_lines() -> None:
    valid = """{
      "summary": "A real issue was found.",
      "findings": [{
        "title": "Unsafe dereference", "category": "potential_bug", "severity": "HIGH",
        "description": "A nullable value is dereferenced.", "impact": "Runtime failure.",
        "evidence": "Changed call has no guard.", "filename": "src/OrderService.java",
        "line_start": 999, "line_end": 999, "suggested_fix": "Add validation.", "confidence": 0.91
      }]
    }"""

    async def exercise() -> None:
        llm = FakeLlm(["not json", valid])
        agent = CodeReviewAgent(Settings(auto_index=False), llm, FakeRag())
        report = await agent.review(
            make_pr(
                PullRequestFile(
                    filename="src/OrderService.java",
                    status="modified",
                    patch="@@ -10 +10,2 @@\n old\n+nullable.run();",
                )
            )
        )
        assert llm.calls == 2
        assert report.total_tokens == 30
        assert len(report.findings) == 1
        assert report.findings[0].line_start is None
        assert report.findings[0].permalink is not None
        assert "all human-readable review fields in Russian" in REVIEW_SYSTEM_PROMPT

    asyncio.run(exercise())


def test_github_comment_template_is_in_russian() -> None:
    markdown = ReviewReportFormatter().markdown(make_report())

    assert "# AI-ревью кода" in markdown
    assert "## Возможные ошибки" in markdown
    assert "**Предлагаемое исправление:**" in markdown
    assert "## Review Statistics" not in markdown


def test_empty_or_binary_diff_does_not_call_llm() -> None:
    async def exercise() -> None:
        llm = FakeLlm([])
        report = await CodeReviewAgent(Settings(auto_index=False), llm, FakeRag()).review(
            make_pr(PullRequestFile(filename="asset.bin", status="modified"))
        )
        assert llm.calls == 0
        assert report.analysis_completeness == "partial"
        assert report.unreviewed_files == ["asset.bin"]

    asyncio.run(exercise())


def test_low_confidence_findings_are_filtered() -> None:
    low = make_report().findings[0].model_copy(update={"confidence": 0.59})
    high = make_report().findings[0].model_copy(update={"title": "Supported", "confidence": 0.6})
    findings = CodeReviewAgent._validate_findings(
        make_pr(), [low, high], {"src/OrderService.java": {11}}
    )
    assert [item.title for item in findings] == ["Supported"]


def test_review_persistence_and_conversation_link(db) -> None:
    from app.crud import ConversationService
    from app.schemas import ConversationCreate

    history = ReviewHistoryService(db)
    pr = history.upsert_pull_request(make_pr())
    review = history.create_review(pr, source="manual", model="deepseek-flash")
    history.mark_running(review.id)
    history.complete(review.id, make_report())
    saved = history.require(review.id)
    assert saved.status == "completed"
    assert saved.findings[0].severity == "HIGH"
    conversation = ConversationService(db).create(ConversationCreate(title="Discuss review"))
    history.link_conversation(conversation.id, saved.id)
    assert history.conversation_review(conversation.id).id == saved.id


def test_machine_metadata_round_trip() -> None:
    report = make_report()
    encoded = encode_metadata(report)
    body = f"<!-- ai-helper-code-review:v1:{encoded} -->\n# Report"
    decoded = decode_metadata(body)
    assert decoded is not None
    assert decoded["head_sha"] == report.head_sha
    assert decoded["findings"][0]["id"] == "AI-1"


def test_publisher_updates_only_owned_marker_comment() -> None:
    class FakeGitHub:
        updated: int | None = None
        created = False

        async def authenticated_login(self):
            return "ai-bot"

        async def list_comments(self, number):
            return [
                {"id": 1, "body": "<!-- ai-helper-code-review:v1:bad -->", "user": {"login": "other"}},
                {"id": 2, "body": "<!-- ai-helper-code-review:v1:old -->", "user": {"login": "ai-bot"}},
            ]

        async def update_comment(self, comment_id, body):
            self.updated = comment_id
            return {"id": comment_id, "body": body}

        async def create_comment(self, number, body):
            self.created = True
            return {"id": 3, "body": body}

    async def exercise() -> None:
        github = FakeGitHub()
        result = await GitHubReviewPublisher(github, ReviewReportFormatter()).publish(make_report())
        assert result["id"] == 2
        assert github.updated == 2
        assert github.created is False

    asyncio.run(exercise())


def test_sync_is_idempotent_but_imports_newer_comment_metadata(db) -> None:
    class FakeGitHub:
        def __init__(self) -> None:
            self.report = make_report()

        async def list_pull_requests(self, state):
            return [make_pr()]

        async def list_comments(self, number):
            encoded = encode_metadata(self.report)
            return [
                {
                    "id": 99,
                    "html_url": "https://example.test/comment/99",
                    "body": f"<!-- ai-helper-code-review:v1:{encoded} -->",
                }
            ]

    async def exercise() -> None:
        github = FakeGitHub()
        sync = ReviewSyncService(db, github)
        assert (await sync.sync())["synced_reviews"] == 1
        assert (await sync.sync())["synced_reviews"] == 0
        github.report = github.report.model_copy(
            update={
                "generated_at": github.report.generated_at + timedelta(seconds=1),
                "summary": "Updated report",
            }
        )
        assert (await sync.sync())["synced_reviews"] == 1
        assert ReviewHistoryService(db).list()[0].summary == "Updated report"

    asyncio.run(exercise())
