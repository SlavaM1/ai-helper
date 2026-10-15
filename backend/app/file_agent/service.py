"""Durable execution, detached jobs, and guarded host-only workspace lifecycle."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.mcp_tools import GitMcpClient
from app.models import utc_now

from .agent import FileAgent, public_value, safe_error
from .files import Files, digest, git
from .models import FileAgentTask
from .rules import check_rules
from .workspace import PartialApplyError, WorkspaceManager

ACTIVE = {"queued", "running"}


class FileAgentError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        self.status = status
        super().__init__(message)


class FileAgentService:
    def __init__(self, settings: Any, llm: Any, session_factory: Any) -> None:
        self.settings = settings
        self.llm = llm
        self.sessions = session_factory
        self.semaphore = asyncio.Semaphore(1)
        self.jobs: set[asyncio.Task] = set()
        self.task_jobs: dict[str, asyncio.Task] = {}

    def manager(self) -> WorkspaceManager:
        return WorkspaceManager(Path(self.settings.target_repo_path), Path(self.settings.file_agent_storage))

    def require(self, task_id: str, *, include_hidden: bool = False) -> FileAgentTask:
        with self.sessions() as db:
            task = db.get(FileAgentTask, task_id)
            if task is None or (task.parameters.get("history_deleted") and not include_hidden):
                raise FileAgentError("Task not found.", 404)
            db.expunge(task)
            return task

    def serialize(self, task: FileAgentTask) -> dict:
        # SQLite drops timezone information; timestamps are stored in UTC.
        # Preserve that meaning for JS Date.parse and live duration metrics.
        def timestamp(value):
            return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value

        workspace = {key: value for key, value in task.workspace.items() if key in {"id", "base_sha", "created_at", "state"}}
        artifact = task.workspace.get("artifact")
        if artifact:
            workspace["artifact"] = {key: value for key, value in artifact.items() if key not in {"patch_path", "metadata_path"}}
        public_status = task.status
        if task.parameters.get("cancel_requested"):
            if task.status in ACTIVE:
                public_status = "stopping"
            elif task.status == "failed":
                public_status = "cancelled"
        return {
            "id": task.id, "parent_id": task.parent_id, "prompt": redact_prompt(task.prompt),
            "model": task.model, "status": public_status, "base_sha": task.base_sha,
            "workspace": workspace, "operations": task.operations, "read_files": task.read_files,
            "changes": task.changes, "usage": task.usage, "validation": task.validation,
            "parameters": task.parameters, "result": task.result, "error": task.error,
            "created_at": timestamp(task.created_at), "updated_at": timestamp(task.updated_at),
            "started_at": timestamp(task.started_at), "finished_at": timestamp(task.finished_at),
            "can_apply": bool(task.status == "completed" and task.validation.get("ok") and artifact
                              and self.settings.file_agent_allow_source_apply),
            "source_apply_enabled": self.settings.file_agent_allow_source_apply,
            "correctness_guaranteed": False,
            "patch_export_notice": "Downloads are redacted previews, not exact replayable patches. Exact artifacts are private host files under configured storage/.file-agent-owners/<workspace-id>.patch; review and apply them on the host.",
        }

    def history(self, limit: int = 50, offset: int = 0) -> list[dict]:
        with self.sessions() as db:
            tasks = db.scalars(select(FileAgentTask).where(
                FileAgentTask.parameters["history_deleted"].as_boolean().is_not(True)
            ).order_by(FileAgentTask.created_at.desc()).offset(offset).limit(limit)).all()
            return [self.serialize(task) for task in tasks]

    def update(self, task_id: str, **fields: Any) -> None:
        with self.sessions() as db:
            task = db.get(FileAgentTask, task_id)
            for key, value in fields.items():
                setattr(task, key, value)
            task.updated_at = utc_now()
            db.commit()

    def emit(self, task_id: str, **event: Any) -> None:
        with self.sessions() as db:
            task = db.get(FileAgentTask, task_id)
            read_files = event.pop("read_files", None)
            usage = event.get("usage")
            if read_files is not None:
                task.read_files = sorted(set(task.read_files) | set(read_files))
            if usage is not None:
                task.usage = usage
            task.operations = [*task.operations, {"id": len(task.operations) + 1,
                               "timestamp": utc_now().isoformat(), **event}]
            task.updated_at = utc_now()
            db.commit()

    def recover(self) -> int:
        """A process restart cannot silently resume an in-flight provider write."""
        with self.sessions() as db:
            tasks = db.scalars(select(FileAgentTask).where(FileAgentTask.status.in_(ACTIVE))).all()
            for task in tasks:
                task.status = "failed"
                task.error = ("Stopped by user." if task.parameters.get("cancel_requested") else
                              "Execution interrupted by server restart. Rerun from the saved base or replay an existing artifact.")
                task.finished_at = utc_now()
                task.operations = [*task.operations, {"id": len(task.operations) + 1, "timestamp": utc_now().isoformat(),
                                   "phase": "recovery", "status": "error", "error": task.error}]
            db.commit()
            return len(tasks)

    def parameters(self) -> dict:
        return {
            "max_iterations": self.settings.file_agent_max_iterations,
            "max_context_bytes": self.settings.file_agent_max_context_bytes,
            "max_cumulative_context_bytes": self.settings.file_agent_max_cumulative_context_bytes,
            "max_output_tokens": self.settings.file_agent_max_output_tokens,
            "tool_timeout_seconds": self.settings.file_agent_tool_timeout_seconds,
            "timeout_seconds": self.settings.file_agent_timeout_seconds,
            "rules": list(self.settings.file_agent_rules),
        }

    def _insert(self, prompt: str, model: str, base_sha: str, parameters: dict,
                *, parent_id: str | None = None, mode: str = "run") -> FileAgentTask:
        with self.sessions() as db:
            clean_parameters = {key: value for key, value in parameters.items()
                                if key not in {"cancel_requested", "history_deleted"}}
            task = FileAgentTask(id=str(uuid.uuid4()), prompt=prompt, model=model, base_sha=base_sha,
                                 parameters={**clean_parameters, "mode": mode}, parent_id=parent_id)
            db.add(task)
            db.commit()
            db.refresh(task)
            db.expunge(task)
        self.emit(task.id, phase="queued", status="queued")
        return self.require(task.id)

    async def create(self, prompt: str, model: str | None = None) -> dict:
        try:
            base_sha = (await asyncio.to_thread(git, Path(self.settings.target_repo_path),
                                               "rev-parse", "--verify", "HEAD^{commit}", max_bytes=128)).decode().strip()
        except Exception as exc:
            raise FileAgentError("Source repository is unavailable or invalid.", 503) from exc
        task = self._insert(prompt.strip(), model or self.settings.default_model, base_sha, self.parameters())
        self.start(task.id)
        return self.serialize(task)

    def start(self, task_id: str) -> None:
        job = asyncio.create_task(self.execute(task_id))
        self.jobs.add(job)
        self.task_jobs[task_id] = job

        def done(finished: asyncio.Task) -> None:
            self.jobs.discard(finished)
            self.task_jobs.pop(task_id, None)
            # A queued coroutine may be cancelled before entering execute().
            task = self.require(task_id, include_hidden=True)
            if task.status in ACTIVE and task.parameters.get("cancel_requested"):
                self._finish_cancel(task_id)
            if not finished.cancelled():
                finished.exception()  # Retrieve errors; never leak an unhandled task exception.

        job.add_done_callback(done)

    def _finish_cancel(self, task_id: str) -> None:
        task = self.require(task_id, include_hidden=True)
        self.update(task_id, status="failed", error="Stopped by user.", finished_at=utc_now(),
                    validation={**task.validation, "ok": False, "complete": False, "cancelled": True})
        self.emit(task_id, phase="finished", status="cancelled")

    async def stop(self, task_id: str) -> dict:
        task = self.require(task_id)
        if task.status in ACTIVE and not task.parameters.get("cancel_requested"):
            self.update(task_id, parameters={**task.parameters, "cancel_requested": True})
            self.emit(task_id, phase="cancellation", status="requested")
            job = self.task_jobs.get(task_id)
            if job is not None and not job.done():
                job.cancel()
            else:
                self._finish_cancel(task_id)
        return self.serialize(self.require(task_id))

    async def delete_history(self, task_id: str) -> None:
        task = self.require(task_id)
        self._idle(task)
        if task_id in self.task_jobs:
            raise FileAgentError("File agent is finishing an operation. Stop the task and retry after it finishes.")
        # Logical deletion preserves snapshots referenced by existing child tasks.
        # Public endpoints return 404; no source/workspace files are removed.
        self.update(task_id, parameters={**task.parameters, "history_deleted": True})

    @staticmethod
    async def _workspace_thread(action, *args, **kwargs):
        """Do not abandon a filesystem writer when its awaiting task is cancelled."""
        worker = asyncio.create_task(asyncio.to_thread(action, *args, **kwargs))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            await worker
            raise

    async def close(self) -> None:
        jobs = list(self.jobs)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def execute(self, task_id: str) -> None:
        async with self.semaphore:
            task = self.require(task_id)
            if task.status != "queued":
                return
            self.update(task_id, status="running", started_at=utc_now())
            self.emit(task_id, phase="workspace", status="running")
            # Use snapshotted limits/model for reproducible reruns, not changed settings.
            configured = self.settings.model_copy(update={
                "file_agent_tool_timeout_seconds": task.parameters["tool_timeout_seconds"],
                "file_agent_timeout_seconds": task.parameters["timeout_seconds"],
            })
            agent = FileAgent(configured, self.llm, self.sessions, lambda **event: self.emit(task_id, **event))
            agent.should_stop = lambda: bool(self.require(task_id, include_hidden=True).parameters.get("cancel_requested"))
            workspace = None
            failure = None
            result = None
            reserve = min(120, task.parameters["timeout_seconds"] / 3)
            agent.deadline = time.monotonic() + task.parameters["timeout_seconds"] - reserve
            try:
                async with asyncio.timeout(task.parameters["timeout_seconds"] - reserve):
                    manager = self.manager()

                    async def prepare():
                        nonlocal workspace
                        # Capture ownership even if cancellation arrives during clone/replay.
                        def create_workspace():
                            if task.parameters.get("mode") == "replay":
                                parent = self.require(task.parent_id, include_hidden=True)
                                return manager.replay(parent.workspace, task.id)
                            parent = self.require(task.parent_id, include_hidden=True) if task.parent_id else None
                            return manager.create(task.id, task.base_sha,
                                                  snapshot_workspace=parent.workspace if parent else None)

                        async def capture():
                            nonlocal workspace
                            workspace = await asyncio.to_thread(create_workspace)
                            self.update(task_id, workspace=workspace)
                            return workspace

                        worker = asyncio.create_task(capture())
                        try:
                            return await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            await worker
                            raise

                    workspace = await agent.operation("workspace_replay" if task.parameters.get("mode") == "replay" else "workspace_create", {}, prepare, phase="workspace")
                    self.update(task_id, workspace=workspace)
                    if task.parameters.get("mode") == "replay":
                        result = {"summary": "Saved patch replayed into a fresh clone; final checks rerun.", "replayed_from": task.parent_id}
                    else:
                        result = await agent.run(workspace, task.prompt, task.model, task.parameters)
                    self.update(task_id, result=result)
            except (Exception, asyncio.CancelledError) as exc:
                failure = safe_error(exc)
            if agent.should_stop():
                if workspace:
                    try:
                        artifact = await self._workspace_thread(self.manager().save_artifact, workspace)
                        self.update(task_id, changes=artifact["changes"],
                                    workspace={**workspace, "state": "exported", "artifact": artifact})
                    except Exception:
                        self.emit(task_id, phase="artifact", status="error", error="Partial workspace retained; artifact export unavailable.")
                self.update(task_id, usage=agent.usage)
                self._finish_cancel(task_id)
                return
            if workspace:
                try:
                    final_deadline = time.monotonic() + reserve
                    # Reserve a separate slice for private export even when checks time out.
                    agent.deadline = time.monotonic() + reserve * 0.7
                    async with asyncio.timeout(reserve):
                        await self.finalize(task_id, workspace, agent, final_deadline=final_deadline)
                except (Exception, asyncio.CancelledError) as exc:
                    failure = failure or "Final validation/artifact export did not finish: " + safe_error(exc)
            final = self.require(task_id)
            if final.parameters.get("cancel_requested"):
                self.update(task_id, usage=agent.usage)
                self._finish_cancel(task_id)
                return
            if not final.validation.get("ok"):
                failure = failure or "Final validation failed or was incomplete. Inspect diagnostics; source apply is blocked."
            self.update(task_id, status="failed" if failure else "completed", error=failure,
                        result=result, usage=agent.usage, finished_at=utc_now())
            self.emit(task_id, phase="finished", status="failed" if failure else "completed", error=failure)

    async def finalize(self, task_id: str, workspace: dict, agent: FileAgent, *, final_deadline: float | None = None) -> None:
        """Always use real MCP on the final bytes, including after provider failures."""
        mcp = GitMcpClient(workspace["path"], server_module="app.file_agent.mcp_server")
        checks = []
        diff_ok = False
        try:
            diff = await agent.operation("file_diff", {}, lambda: mcp.call("file_diff"), phase="validation")
            self.update(task_id, changes=diff["changes"])
            diff_ok = True
            paths = [change["path"] for change in diff["changes"] if change["status"] != "deleted"]
            if not paths:
                paths = sorted(path for path in agent.read_files if path.lower().endswith((".md", ".markdown", ".java")))
            for path in paths:
                try:
                    check = await agent.operation("file_validate", {"path": path}, lambda path=path: mcp.call("file_validate", {"path": path}), phase="validation")
                except Exception as exc:
                    check = {"path": path, "ok": False, "error": safe_error(exc), "executed": False}
                checks.append(check)
                self.update(task_id, validation={"ok": False, "checks": checks, "complete": False})
            try:
                rules = await agent.operation("architecture_rules", {}, lambda: asyncio.to_thread(
                    check_rules, workspace["path"], self.require(task_id).parameters["rules"], paths), phase="validation")
            except Exception as exc:
                rules = {"error": safe_error(exc), "findings": [], "incomplete": True}
            self.update(task_id, validation={
                "ok": bool(checks) and all(check.get("ok") is True for check in checks) and not rules.get("findings") and not rules.get("incomplete"),
                "complete": True, "checks": checks, "architecture": rules, "executed": False,
                "limitations": "Markdown checks and Java lexical/architecture heuristics only; no build, compilation, tests, or correctness guarantee.",
            })
        finally:
            # Preserve the exact private artifact even for an invalid/partial task.
            agent.deadline = final_deadline
            artifact = await agent.operation("save_artifact", {}, lambda: self._workspace_thread(self.manager().save_artifact, workspace), phase="artifact")
            self.update(task_id, workspace={**workspace, "state": "exported", "artifact": artifact},
                        **({} if diff_ok else {"changes": artifact["changes"]}))

    def _idle(self, task: FileAgentTask) -> None:
        if task.status in ACTIVE:
            raise FileAgentError("Task is still queued or running.")

    async def rerun(self, task_id: str, *, replay: bool = False) -> dict:
        parent = self.require(task_id)
        self._idle(parent)
        if replay and not parent.workspace.get("artifact"):
            raise FileAgentError("No saved artifact to replay.")
        task = self._insert(parent.prompt, parent.model, parent.base_sha, parent.parameters,
                            parent_id=parent.id, mode="replay" if replay else "run")
        self.start(task.id)
        return self.serialize(task)

    async def lifecycle(self, task_id: str, action: str, artifact_sha256: str | None = None) -> dict:
        task = self.require(task_id)
        self._idle(task)
        if self.semaphore.locked():
            raise FileAgentError("File agent is busy. Retry after the running operation finishes.")
        async with self.semaphore:
            task = self.require(task_id)
            self._idle(task)
            if task.status == "discarded":
                raise FileAgentError("Workspace has already been discarded.")
            if not task.workspace:
                raise FileAgentError("Task has no workspace.")
            if action == "apply":
                artifact = task.workspace.get("artifact", {})
                if task.status != "completed" or not task.validation.get("ok") or not artifact:
                    raise FileAgentError("Only completed tasks with successful final validation and a saved artifact can be applied.")
                if artifact_sha256 != artifact.get("sha256"):
                    raise FileAgentError("Artifact SHA-256 does not match the reviewed artifact.")
                if not self.settings.file_agent_allow_source_apply:
                    raise FileAgentError("Source apply is disabled (read-only Docker checkout). Export the redacted preview for review; apply the exact private artifact from configured host storage after review. Set AI_HELPER_FILE_AGENT_ALLOW_SOURCE_APPLY=true only on a writable trusted host.")
            self.emit(task_id, tool=action, phase=action, status="started")
            try:
                manager = self.manager()
                if action == "apply":
                    # Confirm that the workspace still exports precisely the validated artifact.
                    def verify():
                        saved, _, _ = manager._artifact(task.workspace)
                        files = Files(task.workspace["path"])
                        with files._locked() as fd:
                            current_hash = digest(files._patch(files._changes(fd), redacted=False).encode())
                        return saved["sha256"] == artifact_sha256 == current_hash

                    if not await asyncio.to_thread(verify):
                        self.update(task_id, validation={**task.validation, "ok": False, "complete": False})
                        raise FileAgentError("Workspace changed after validation. Replay and validate before applying.")
                    outcome = await asyncio.to_thread(manager.apply, task.workspace, allow_source_write=True)
                else:
                    outcome = await asyncio.to_thread(manager.discard, task.workspace)
            except PartialApplyError:
                message = "Partial source apply: concurrent edits prevented safe rollback. STOP and inspect touched files and the private artifact; no automatic retry."
                self.update(task_id, status="failed", error=message,
                            validation={**task.validation, "ok": False})
                self.emit(task_id, tool=action, phase=action, status="error", error=message)
                raise FileAgentError(message) from None
            except FileAgentError:
                self.emit(task_id, tool=action, phase=action, status="error", error="Artifact no longer matches validation.")
                raise
            except Exception as exc:
                self.emit(task_id, tool=action, phase=action, status="error", error=safe_error(exc))
                raise FileAgentError("Workspace conflict or read-only source. No automatic retry; the private exported artifact remains available.") from exc
            status = "applied" if action == "apply" else "discarded"
            self.update(task_id, status=status, workspace={**task.workspace, "state": status})
            self.emit(task_id, tool=action, phase=action, status="success", result=public_value({key: value for key, value in outcome.items() if key != "path"}))
            return self.serialize(self.require(task_id))

    async def inspect(self, task_id: str, tool: str, path: str | None = None) -> dict:
        task = self.require(task_id)
        if not task.workspace or task.status == "discarded":
            raise FileAgentError("Workspace is not available; replay a saved artifact to inspect it.")
        try:
            mcp = GitMcpClient(task.workspace["path"], server_module="app.file_agent.mcp_server")
            async with asyncio.timeout(self.settings.file_agent_tool_timeout_seconds):
                return await mcp.call(tool, {"path": path} if path is not None else {})
        except Exception as exc:
            raise FileAgentError("File is unavailable, excluded, or outside the workspace.", 400) from exc


def redact_prompt(prompt: str) -> str:
    from .files import redact_text

    return redact_text(prompt)
