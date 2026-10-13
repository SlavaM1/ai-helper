from __future__ import annotations

import base64
import json
import os
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from .schemas import PullRequestData, PullRequestFile, ReviewReport

MARKER_PREFIX = "<!-- ai-helper-code-review:v1:"
MARKER_RE = re.compile(r"<!-- ai-helper-code-review:v1:([A-Za-z0-9_-]+) -->")


class GitHubApiError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubPRClient:
    def __init__(
        self,
        token: str,
        repository: str,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub repository must be owner/name")
        self.repository = repository
        self._owns_client = client is None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-helper-code-review",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise GitHubApiError(504, "GitHub API timeout") from exc
        except httpx.HTTPError as exc:
            raise GitHubApiError(502, f"GitHub API connection failed: {exc}") from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = str(payload.get("message", response.text))
            except (ValueError, AttributeError):
                message = response.text
            raise GitHubApiError(response.status_code, f"GitHub API: {message[:500]}")
        if response.status_code == 204:
            return None
        return response.json()

    async def _paginate(self, path: str, *, params: dict[str, Any] | None = None) -> list[Any]:
        page = 1
        items: list[Any] = []
        while True:
            query = {**(params or {}), "per_page": 100, "page": page}
            batch = await self._request("GET", path, params=query)
            if not isinstance(batch, list):
                raise GitHubApiError(502, "GitHub API returned an invalid paginated response")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1
            if page > 100:
                raise GitHubApiError(502, "GitHub API pagination limit exceeded")

    async def list_pull_requests(self, state: str = "open") -> list[PullRequestData]:
        rows = await self._paginate(f"/repos/{self.repository}/pulls", params={"state": state})
        return [self._parse_pr(row, files=[]) for row in rows]

    async def get_pull_request(self, number: int) -> PullRequestData:
        if number <= 0:
            raise ValueError("Pull request number must be positive")
        data = await self._request("GET", f"/repos/{self.repository}/pulls/{number}")
        file_rows = await self._paginate(f"/repos/{self.repository}/pulls/{number}/files")
        files = [
            PullRequestFile(
                filename=str(item.get("filename", "")),
                previous_filename=item.get("previous_filename"),
                status=str(item.get("status", "modified")),
                additions=int(item.get("additions", 0)),
                deletions=int(item.get("deletions", 0)),
                changes=int(item.get("changes", 0)),
                patch=item.get("patch"),
                blob_url=item.get("blob_url"),
                raw_url=item.get("raw_url"),
            )
            for item in file_rows
            if item.get("filename")
        ]
        return self._parse_pr(data, files)

    async def list_comments(self, number: int) -> list[dict[str, Any]]:
        return await self._paginate(f"/repos/{self.repository}/issues/{number}/comments")

    async def authenticated_login(self) -> str | None:
        try:
            user = await self._request("GET", "/user")
            return str(user.get("login")) if user.get("login") else None
        except GitHubApiError:
            return "github-actions[bot]" if os.getenv("GITHUB_ACTIONS") == "true" else None

    async def create_comment(self, number: int, body: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/repos/{self.repository}/issues/{number}/comments", json={"body": body}
        )

    async def update_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        return await self._request(
            "PATCH", f"/repos/{self.repository}/issues/comments/{comment_id}", json={"body": body}
        )

    def _parse_pr(self, data: dict[str, Any], files: list[PullRequestFile]) -> PullRequestData:
        return PullRequestData(
            repository=self.repository,
            number=int(data["number"]),
            title=str(data.get("title") or "Untitled pull request"),
            description=str(data.get("body") or ""),
            author=str((data.get("user") or {}).get("login") or "unknown"),
            base_branch=str((data.get("base") or {}).get("ref") or ""),
            head_branch=str((data.get("head") or {}).get("ref") or ""),
            base_sha=str((data.get("base") or {}).get("sha") or ""),
            head_sha=str((data.get("head") or {}).get("sha") or ""),
            state=str(data.get("state") or "unknown"),
            draft=bool(data.get("draft", False)),
            html_url=str(data.get("html_url") or ""),
            additions=int(data.get("additions", 0)),
            deletions=int(data.get("deletions", 0)),
            changed_files=int(data.get("changed_files", len(files))),
            updated_at=_parse_datetime(data.get("updated_at")),
            files=files,
        )


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC)
    except ValueError:
        return None


def encode_metadata(report: ReviewReport) -> str:
    payload = report.model_dump(mode="json", exclude={"sources": {"__all__": {"content"}}})
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_metadata(body: str) -> dict[str, Any] | None:
    match = MARKER_RE.search(body)
    if not match:
        return None
    encoded = match.group(1)
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        value = json.loads(raw)
        return value if isinstance(value, dict) and value.get("schema_version") == 1 else None
    except (ValueError, json.JSONDecodeError):
        return None


class GitHubReviewPublisher:
    def __init__(self, client: GitHubPRClient, formatter: Any) -> None:
        self.client = client
        self.formatter = formatter

    async def publish(self, report: ReviewReport) -> dict[str, Any]:
        body = self.formatter.markdown(report)
        metadata = encode_metadata(report)
        body = f"{MARKER_PREFIX}{metadata} -->\n{body}"
        if len(body) > 65_000:
            raise ValueError("Review comment exceeds GitHub's safe comment size")

        login = await self.client.authenticated_login()
        comments = await self.client.list_comments(report.pr_number)
        owned = next(
            (
                item
                for item in comments
                if MARKER_PREFIX in str(item.get("body", ""))
                and login
                and str((item.get("user") or {}).get("login", "")).casefold() == login.casefold()
            ),
            None,
        )
        if owned:
            return await self.client.update_comment(int(owned["id"]), body)
        return await self.client.create_comment(report.pr_number, body)
