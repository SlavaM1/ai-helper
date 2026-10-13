from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.agent import DeepSeekClient
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.mcp_tools import GitMcpClient
from app.rag import HybridRetriever, RagIndexer

from .engine import CodeReviewAgent, ReviewRAGService
from .formatter import ReviewReportFormatter
from .github import GitHubPRClient, GitHubReviewPublisher
from .service import ReviewHistoryService


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run the shared AI Helper PR review engine")
    result.add_argument("--repo", default=None, help="Allowed GitHub repository in owner/name form")
    result.add_argument("--pr", required=True, type=int, help="Pull request number")
    result.add_argument("--repository-path", help="Trusted base-revision checkout used for isolated RAG")
    result.add_argument("--model", help="DeepSeek model override")
    result.add_argument("--no-publish", action="store_true", help="Do not write the report to GitHub")
    return result


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    repository = args.repo or settings.github_repository
    github = GitHubPRClient(settings.github_token, repository)
    try:
        pull_request = await github.get_pull_request(args.pr)
        init_db()
        repository_path = Path(args.repository_path or settings.target_repo_path)
        if repository_path.is_dir():
            with SessionLocal() as db:
                from app.crud import RagService

                state = RagService(db).get_index_state()
            if state is None or state.commit_sha != pull_request.base_sha:
                RagIndexer(SessionLocal, repository_path).index()

        with SessionLocal() as db:
            history = ReviewHistoryService(db)
            pr = history.upsert_pull_request(pull_request)
            review = history.create_review(
                pr,
                source="github_action" if not args.no_publish else "manual",
                model=args.model or settings.review_model or settings.default_model,
            )
            history.mark_running(review.id)
            llm = DeepSeekClient(settings.deepseek_api_key, settings.deepseek_base_url)
            agent = CodeReviewAgent(
                settings,
                llm,
                ReviewRAGService(db, HybridRetriever(db), settings),
                GitMcpClient(repository_path) if repository_path.is_dir() else None,
            )
            report = await agent.review(pull_request, model=args.model)
            comment = None
            if not args.no_publish:
                comment = await GitHubReviewPublisher(github, ReviewReportFormatter()).publish(report)
            history.complete(review.id, report, github_comment=comment)
            print(
                json.dumps(
                    {
                        "review_id": review.id,
                        "repository": repository,
                        "pr": args.pr,
                        "head_sha": report.head_sha,
                        "findings": len(report.findings),
                        "files_analyzed": report.files_analyzed,
                        "published": bool(comment),
                    }
                )
            )
        return 0
    except Exception as exc:
        print(f"AI review failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await github.close()


def main() -> None:
    args = parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
