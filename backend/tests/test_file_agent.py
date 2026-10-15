from __future__ import annotations

import asyncio
import json
import subprocess
from functools import wraps
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import create_database_engine, init_db
from app.file_agent.agent import FileAgent
from app.file_agent.models import FileAgentTask
from app.file_agent.router import router
from app.file_agent.rules import check_rules
from app.file_agent.service import FileAgentService
from app.file_agent.workspace import PartialApplyError, WorkspaceManager


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout.decode().strip()


class Provider:
    def __init__(self):
        self.actions = []
        self.messages = []

    async def complete_json(self, messages, *, model, max_tokens):
        self.messages.append(messages.copy())
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            action = action(messages)
        return json.dumps(action), {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


@pytest.fixture
def env(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.test")
    (source / "README.md").write_text("# Project\n\nOriginal.\n")
    (source / "Main.java").write_text("public class Main {}\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "base")
    settings = Settings(database_url=f"sqlite:///{tmp_path}/test.db", target_repo_path=str(source),
                        file_agent_storage=str(tmp_path / "storage"), auto_index=False,
                        file_agent_timeout_seconds=180, file_agent_max_iterations=10)
    engine = create_database_engine(settings.database_url)
    init_db(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    provider = Provider()
    svc = FileAgentService(settings, provider, sessions)
    app = FastAPI()
    app.state.file_agent_service = svc
    app.include_router(router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    yield svc, provider, client, source
    asyncio.run(client.aclose())
    engine.dispose()


async def finish(svc, task_id):
    jobs = list(svc.jobs)
    if jobs:
        await asyncio.wait_for(asyncio.gather(*jobs), timeout=150)
    return svc.require(task_id)


def edit_actions(new_text="Updated."):
    def patch(messages):
        data = json.loads(messages[-1]["content"].split(": ", 1)[1])
        return {"tool": "file_apply_patch", "arguments": {"path": "README.md", "expected_hash": data["sha256"],
                                                          "old_text": "Original.", "new_text": new_text}}
    return [
        {"tool": "file_list", "arguments": {}},
        {"tool": "file_read", "arguments": {"path": "Main.java"}},
        {"tool": "file_read", "arguments": {"path": "README.md"}},
        patch,
        {"result": "Updated README.md. Java and Markdown checks are heuristics; source unchanged."},
    ]


def test_history_timestamps_preserve_utc_for_browser_duration(env):
    svc, _, _, source = env
    task = svc._insert("Timestamp regression", "mock-model", git(source, "rev-parse", "HEAD"), svc.parameters())
    payload = svc.serialize(svc.require(task.id))
    assert payload["created_at"].isoformat().endswith("+00:00")
    assert payload["updated_at"].isoformat().endswith("+00:00")
    assert payload["started_at"] is None


@async_test
async def test_stop_queued_task_before_it_starts_and_confirm_history_delete(env):
    svc, provider, client, source = env
    task = svc._insert("Queued stop", "test-model", git(source, "rev-parse", "HEAD"), svc.parameters())
    await svc.semaphore.acquire()
    try:
        svc.start(task.id)
        job = svc.task_jobs[task.id]
        response = await client.post(f"/api/file-agent/tasks/{task.id}/stop", json={})
        assert response.status_code == 202
        await asyncio.gather(job, return_exceptions=True)
    finally:
        svc.semaphore.release()
    stopped = (await client.get(f"/api/file-agent/tasks/{task.id}")).json()
    assert stopped["status"] == "cancelled" and not stopped["workspace"]
    assert not provider.messages
    assert (await client.post(f"/api/file-agent/tasks/{task.id}/stop", json={})).json()["status"] == "cancelled"
    assert (await client.request("DELETE", f"/api/file-agent/tasks/{task.id}", json={"confirmed": False})).status_code == 422
    assert (await client.request("DELETE", f"/api/file-agent/tasks/{task.id}", json={"confirmed": True})).status_code == 204
    assert (await client.get(f"/api/file-agent/tasks/{task.id}")).status_code == 404
    assert (await client.get(f"/api/file-agent/tasks/{task.id}/events")).status_code == 404
    assert not (await client.get("/api/file-agent/tasks")).json()
    # Restart does not restore logically deleted entries; internal snapshot metadata remains.
    svc.recover()
    assert not svc.history()
    assert svc.require(task.id, include_hidden=True).parameters["history_deleted"]


@async_test
async def test_stop_running_provider_preserves_partial_workspace_without_further_calls(env):
    svc, provider, client, source = env
    waiting = asyncio.Event()
    cancelled = asyncio.Event()
    original = provider.complete_json
    provider.actions = [
        {"tool": "file_read", "arguments": {"path": "README.md"}},
        {"tool": "file_create", "arguments": {"path": "docs/partial.md", "content": "# Partial\n\nRetained.\n"}},
    ]
    calls = 0

    async def paused(messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return await original(messages, **kwargs)
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider.complete_json = paused
    created = (await client.post("/api/file-agent/tasks", json={"prompt": "Write docs"})).json()
    await asyncio.wait_for(waiting.wait(), 60)
    path = f"/api/file-agent/tasks/{created['id']}"
    assert (await client.request("DELETE", path, json={"confirmed": True})).status_code == 409
    assert (await client.post(path + "/stop", json={})).status_code == 202
    task = await finish(svc, created["id"])
    assert cancelled.is_set() and calls == 3
    assert svc.serialize(task)["status"] == "cancelled"
    assert not task.validation["ok"]
    assert (Path(task.workspace["path"]) / "docs/partial.md").exists()
    assert Path(task.workspace["artifact"]["patch_path"]).exists()
    assert not (source / "docs/partial.md").exists()
    assert (await client.post(path + "/apply", json={"confirmed": True, "artifact_sha256": task.workspace["artifact"]["sha256"]})).status_code == 409
    provider.complete_json = original
    provider.actions = edit_actions()
    rerun = (await client.post(path + "/rerun", json={})).json()
    repeated = await finish(svc, rerun["id"])
    assert repeated.status == "completed" and not repeated.parameters.get("cancel_requested")
    discarded = await client.post(path + "/discard", json={"confirmed": True})
    assert discarded.json()["status"] == "discarded"


@async_test
async def test_history_delete_preserves_parent_snapshot_for_queued_child(env):
    svc, provider, client, source = env
    provider.actions = edit_actions()
    parent = await svc.create("Edit docs")
    original = await finish(svc, parent["id"])
    await svc.semaphore.acquire()
    try:
        provider.actions = edit_actions()
        child = await svc.rerun(original.id)
        deleted = await client.request("DELETE", f"/api/file-agent/tasks/{original.id}", json={"confirmed": True})
        assert deleted.status_code == 204
        assert Path(original.workspace["path"]).exists()
        assert Path(original.workspace["artifact"]["patch_path"]).exists()
    finally:
        svc.semaphore.release()
    result = await finish(svc, child["id"])
    assert result.status == "completed", result.error
    assert (await client.get(f"/api/file-agent/tasks/{original.id}/diff")).status_code == 404
    assert all(item["id"] != original.id for item in svc.history())
    assert (source / "README.md").read_text() == "# Project\n\nOriginal.\n"


@async_test
async def test_stop_during_workspace_creation_waits_for_writer(env, monkeypatch):
    import threading

    from app.file_agent.workspace import WorkspaceManager

    svc, provider, client, _ = env
    started, release = threading.Event(), threading.Event()
    create = WorkspaceManager.create

    def delayed(manager, *args, **kwargs):
        started.set()
        assert release.wait(10)
        return create(manager, *args, **kwargs)

    monkeypatch.setattr(WorkspaceManager, "create", delayed)
    task = await svc.create("Stop while preparing")
    try:
        assert await asyncio.to_thread(started.wait, 10)
        response = await client.post(f"/api/file-agent/tasks/{task['id']}/stop", json={})
        assert response.json()["status"] == "stopping"
        assert task["id"] in svc.task_jobs
        assert (await client.request("DELETE", f"/api/file-agent/tasks/{task['id']}", json={"confirmed": True})).status_code == 409
    finally:
        release.set()
    result = await finish(svc, task["id"])
    assert svc.serialize(result)["status"] == "cancelled"
    assert Path(result.workspace["path"]).is_dir()
    assert not provider.messages


@async_test
async def test_stop_is_honoured_when_mcp_wraps_cancellation_as_tool_error(env, monkeypatch):
    from app.mcp_tools import GitMcpClient

    svc, provider, client, _ = env
    waiting = asyncio.Event()
    call = GitMcpClient.call

    async def wrapped(mcp, name, arguments=None):
        if name == "file_read":
            waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("Transport wrapped cancellation") from None
        return await call(mcp, name, arguments)

    monkeypatch.setattr(GitMcpClient, "call", wrapped)
    provider.actions = [{"tool": "file_read", "arguments": {"path": "README.md"}}, {"result": "Must not run"}]
    task = await svc.create("Stop tool call")
    await asyncio.wait_for(waiting.wait(), 60)
    await client.post(f"/api/file-agent/tasks/{task['id']}/stop", json={})
    stopped = await finish(svc, task["id"])
    assert svc.serialize(stopped)["status"] == "cancelled"
    assert len(provider.messages) == 1


@async_test
async def test_real_mcp_edit_history_sse_rerun_replay_discard_and_apply(env):
    svc, provider, client, source = env
    provider.actions = edit_actions()
    response = await client.post("/api/file-agent/tasks", json={"prompt": "Update related project documentation", "model": "test-model"})
    assert response.status_code == 202
    task_id = response.json()["id"]
    task = await finish(svc, task_id)
    assert task.status == "completed", task.error
    assert task.validation["ok"] is True
    assert set(task.read_files) == {"Main.java", "README.md"}
    assert task.usage["llm_calls"] == 5
    assert task.usage["input_tokens"] == 500
    assert (source / "README.md").read_text() == "# Project\n\nOriginal.\n"
    assert "Updated." in (Path(task.workspace["path"]) / "README.md").read_text()
    assert Path(task.workspace["artifact"]["patch_path"]).stat().st_mode & 0o777 == 0o600
    assert "Tool schemas" in provider.messages[0][0]["content"]
    assert "UNTRUSTED DATA" in provider.messages[0][0]["content"]
    assert any(op.get("tool") == "file_validate" and op["status"] == "success" for op in task.operations)
    payload = (await client.get(f"/api/file-agent/tasks/{task_id}")).json()
    assert "path" not in payload["workspace"]
    assert str(source.parent) not in json.dumps(payload)
    assert payload["correctness_guaranteed"] is False
    assert (await client.get("/api/file-agent/tasks")).json()[0]["id"] == task_id
    diff = (await client.get(f"/api/file-agent/tasks/{task_id}/diff", params={"path": "README.md"})).json()
    assert "+Updated." in diff["diff"]
    file = await client.get(f"/api/file-agent/tasks/{task_id}/file", params={"path": "README.md"})
    assert file.json()["redacted"] is True
    sse = await client.get(f"/api/file-agent/tasks/{task_id}/events", params={"after": task.operations[-2]["id"]})
    assert "event: operation" in sse.text and "event: done" in sse.text
    assert f'id: {task.operations[-1]["id"]}' in sse.text
    preview = await client.get(f"/api/file-agent/tasks/{task_id}/patch")
    assert "REDACTED PREVIEW ONLY" in preview.text and task.base_sha in preview.text
    sha = task.workspace["artifact"]["sha256"]
    blocked = await client.post(f"/api/file-agent/tasks/{task_id}/apply", json={"confirmed": True, "artifact_sha256": sha})
    assert blocked.status_code == 409 and "host" in blocked.text
    svc.settings.file_agent_allow_source_apply = True
    bad_hash = await client.post(f"/api/file-agent/tasks/{task_id}/apply", json={"confirmed": True, "artifact_sha256": "0" * 64})
    assert bad_hash.status_code == 409
    applied = await client.post(f"/api/file-agent/tasks/{task_id}/apply", json={"confirmed": True, "artifact_sha256": sha})
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    assert "Updated." in (source / "README.md").read_text()

    # Source now has uncommitted applied changes: rerun still starts at saved base.
    provider.actions = edit_actions("Different rerun.")
    rerun = await client.post(f"/api/file-agent/tasks/{task_id}/rerun", json={})
    second = await finish(svc, rerun.json()["id"])
    assert second.status == "completed", second.error
    assert second.model == task.model and second.base_sha == task.base_sha
    assert second.parameters == task.parameters
    assert "Different rerun." in (Path(second.workspace["path"]) / "README.md").read_text()
    discarded = await client.post(f"/api/file-agent/tasks/{task_id}/discard", json={"confirmed": True})
    assert discarded.json()["status"] == "discarded"
    assert not Path(task.workspace["path"]).exists()
    assert Path(task.workspace["artifact"]["patch_path"]).exists()
    calls_before = len(provider.messages)
    replay = await client.post(f"/api/file-agent/tasks/{task_id}/replay", json={})
    replayed = await finish(svc, replay.json()["id"])
    assert replayed.status == "completed", replayed.error
    assert replayed.workspace["artifact"]["sha256"] == sha
    assert len(provider.messages) == calls_before


@async_test
async def test_validation_failure_blocks_apply_but_preserves_result_artifact_and_preview(env):
    svc, provider, client, _ = env
    provider.actions = edit_actions("[Broken](missing.md)")
    created = await client.post("/api/file-agent/tasks", json={"prompt": "Change docs"})
    task = await finish(svc, created.json()["id"])
    assert task.status == "failed" and task.validation["ok"] is False
    assert task.result.startswith("Updated README.md")
    assert task.workspace["artifact"]["sha256"]
    assert any(diagnostic["code"] == "invalid-local-link" for check in task.validation["checks"] for diagnostic in check.get("diagnostics", []))
    svc.settings.file_agent_allow_source_apply = True
    response = await client.post(f"/api/file-agent/tasks/{task.id}/apply", json={"confirmed": True, "artifact_sha256": task.workspace["artifact"]["sha256"]})
    assert response.status_code == 409


@async_test
async def test_provider_failure_still_validates_partial_writes_and_logs_no_exception_paths(env):
    svc, provider, client, _ = env
    actions = edit_actions()
    provider.actions = [*actions[:-1], RuntimeError("secret-token /private/host/path")]
    created = await client.post("/api/file-agent/tasks", json={"prompt": "Edit docs"})
    task = await finish(svc, created.json()["id"])
    assert task.status == "failed" and task.validation["ok"] is True
    assert task.workspace["artifact"]
    payload = (await client.get(f"/api/file-agent/tasks/{task.id}")).text
    assert "secret-token" not in payload and "/private/host" not in payload
    assert any(op.get("tool") == "llm" and op["status"] == "error" for op in task.operations)


@async_test
async def test_csrf_request_limits_paths_confirmation_and_restart_recovery(env):
    svc, _, client, source = env
    assert (await client.post("/api/file-agent/tasks", json={"prompt": "x"}, headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await client.post("/api/file-agent/tasks", json={"prompt": "x"}, headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
    assert (await client.post("/api/file-agent/tasks", content="prompt=x", headers={"content-type": "application/x-www-form-urlencoded"})).status_code == 415
    assert (await client.post("/api/file-agent/tasks", json={"prompt": "x" * 20_001})).status_code == 422
    assert (await client.post("/api/file-agent/tasks", json={"prompt": "   "})).status_code == 422
    assert (await client.post("/api/file-agent/tasks/x/apply", json={"confirmed": False, "artifact_sha256": "0" * 64})).status_code == 422
    assert (await client.get("/api/file-agent/tasks/missing")).status_code == 404
    assert (await client.get("/api/file-agent/tasks/x/file", params={"path": "../outside"})).status_code == 422
    task = svc._insert("recover", "model", git(source, "rev-parse", "HEAD"), svc.parameters())
    assert (await client.post(f"/api/file-agent/tasks/{task.id}/discard", json={"confirmed": True})).status_code == 409
    assert (await client.post(f"/api/file-agent/tasks/{task.id}/rerun", json={})).status_code == 409
    assert svc.recover() == 1
    recovered = svc.require(task.id)
    assert recovered.status == "failed" and "restart" in recovered.error
    assert recovered.operations[-1]["phase"] == "recovery"
    # New SQLite table is registered by normal init_db.
    with svc.sessions() as db:
        assert db.get(FileAgentTask, task.id) is not None


def test_rules_report_only_locations_and_static_messages(tmp_path):
    (tmp_path / "ExampleController.java").write_text('class ExampleController { UserRepository repo; String password = "do-not-leak-actual-value"; }')
    (tmp_path / "migration").mkdir()
    (tmp_path / "migration" / "bad.sql").write_text("SELECT 1;\n")
    report = check_rules(str(tmp_path), ["controller-repository", "hardcoded-credentials", "migration-naming"])
    assert "do-not-leak" not in json.dumps(report)
    assert {finding["code"] for finding in report["findings"]} == {"controller-repository", "hardcoded-credentials", "migration-naming"}
    assert check_rules(str(tmp_path), [])["findings"] == []


@async_test
async def test_architecture_rules_do_not_flag_controller_repository_markdown_report(env):
    svc, provider, client, source = env
    path = "docs/controller-repository-report.md"
    provider.actions = [
        {"tool": "file_read", "arguments": {"path": "README.md"}},
        {"tool": "file_create", "arguments": {"path": path, "content": "# Controller report\n\nControllers should not call UserRepository directly.\n"}},
        {"tool": "architecture_rules", "arguments": {"paths": [path]}},
        {"result": "Created an architectural report; checks are heuristics only."},
    ]
    response = await client.post("/api/file-agent/tasks", json={"prompt": "Write a controller/repository report"})
    task = await finish(svc, response.json()["id"])
    assert task.status == "completed", task.error
    assert task.validation["ok"] is True
    assert task.validation["architecture"]["findings"] == []
    checks = [op for op in task.operations if op.get("tool") == "architecture_rules" and op["status"] == "success"]
    assert len(checks) == 2  # Model-requested rule check and mandatory final check.
    assert all(op["result"]["findings"] == [] for op in checks)
    assert not (source / path).exists()


@pytest.mark.parametrize("reported_usage", [{}, {"input_tokens": 100, "output_tokens": 20}])
@async_test
async def test_missing_provider_usage_is_estimated_in_events_task_and_history(env, reported_usage):
    svc, provider, client, _ = env
    complete = provider.complete_json

    async def without_usage(messages, **kwargs):
        text, _ = await complete(messages, **kwargs)
        return text, reported_usage

    provider.complete_json = without_usage
    provider.actions = [
        {"tool": "file_read", "arguments": {"path": "README.md"}},
        {"result": "Read project documentation; no source changes."},
    ]
    response = await client.post("/api/file-agent/tasks", json={"prompt": "Read documentation"})
    task = await finish(svc, response.json()["id"])
    assert task.status == "completed", task.error
    assert task.usage["estimated"] is True
    assert task.usage["llm_calls"] == 2
    assert task.usage["input_tokens"] > 0 and task.usage["output_tokens"] > 0
    assert task.usage["total_tokens"] == task.usage["input_tokens"] + task.usage["output_tokens"]
    successes = [op for op in task.operations if op.get("tool") == "llm" and op["status"] == "success"]
    assert len(successes) == 2 and all(op["usage"]["estimated"] is True for op in successes)
    assert (await client.get(f"/api/file-agent/tasks/{task.id}")).json()["usage"] == task.usage
    assert (await client.get("/api/file-agent/tasks")).json()[0]["usage"] == task.usage


@pytest.mark.parametrize(("origin", "expected_status"), [
    ("http://localhost:3010", 202),
    ("http://evil.example:3010", 403),
    ("http://localhost:3011", 403),
    ("http://localhost:3010.evil.example", 403),
    ("http://user@localhost:3010", 403),
])
@async_test
async def test_mutation_origin_uses_request_authority_with_nondefault_port(env, monkeypatch, origin, expected_status):
    svc, _, client, _ = env
    calls = []

    async def create(prompt, model):
        calls.append((prompt, model))
        return {"id": "origin-test"}

    monkeypatch.setattr(svc, "create", create)
    response = await client.post("http://localhost:3010/api/file-agent/tasks", json={"prompt": "Read docs"}, headers={"Origin": origin})
    assert response.status_code == expected_status
    assert len(calls) == (1 if expected_status == 202 else 0)
    assert not svc.jobs


@async_test
async def test_partial_source_apply_is_exposed_as_failed_history_and_blocks_retry(env, monkeypatch):
    svc, provider, client, source = env
    provider.actions = edit_actions()
    response = await client.post("/api/file-agent/tasks", json={"prompt": "Update docs"})
    task = await finish(svc, response.json()["id"])
    assert task.status == "completed", task.error
    svc.settings.file_agent_allow_source_apply = True
    calls = []

    def partial_apply(manager, workspace, *, allow_source_write):
        calls.append(workspace)
        assert manager.source == source and allow_source_write is True
        raise PartialApplyError("private failure at /private/host/path")

    monkeypatch.setattr(WorkspaceManager, "apply", partial_apply)
    body = {"confirmed": True, "artifact_sha256": task.workspace["artifact"]["sha256"]}
    response = await client.post(f"/api/file-agent/tasks/{task.id}/apply", json=body)
    assert response.status_code == 409
    assert "Partial source apply" in response.text and "STOP" in response.text
    assert "/private/host/path" not in response.text
    failed = (await client.get(f"/api/file-agent/tasks/{task.id}")).json()
    assert failed["status"] == "failed" and failed["validation"]["ok"] is False
    assert failed["can_apply"] is False
    assert failed["error"] == response.json()["detail"]
    assert failed["operations"][-1]["tool"] == "apply"
    assert failed["operations"][-1]["status"] == "error"
    assert failed["operations"][-1]["error"] == failed["error"]
    assert (await client.get("/api/file-agent/tasks")).json()[0] == failed
    assert Path(task.workspace["artifact"]["patch_path"]).exists()
    assert (await client.post(f"/api/file-agent/tasks/{task.id}/apply", json=body)).status_code == 409
    assert len(calls) == 1


@async_test
async def test_service_rerun_passes_discarded_parent_snapshot_after_source_base_pruning(env, monkeypatch):
    svc, provider, client, source = env
    provider.actions = edit_actions()
    response = await client.post("/api/file-agent/tasks", json={"prompt": "Update docs"})
    parent = await finish(svc, response.json()["id"])
    assert parent.status == "completed", parent.error
    assert (await client.post(f"/api/file-agent/tasks/{parent.id}/discard", json={"confirmed": True})).status_code == 200
    parent = svc.require(parent.id)
    tree = git(source, "rev-parse", "HEAD^{tree}")
    replacement = git(source, "commit-tree", tree, "-m", "replacement without old ancestry")
    git(source, "update-ref", "HEAD", replacement)
    git(source, "reflog", "expire", "--expire=now", "--all")
    git(source, "gc", "--prune=now")
    missing = await asyncio.to_thread(subprocess.run, ["git", "-C", str(source), "cat-file", "-e", parent.base_sha], check=False, capture_output=True)
    assert missing.returncode != 0
    create = WorkspaceManager.create
    snapshots = []

    def from_snapshot(manager, task_id, base_sha=None, snapshot_workspace=None):
        snapshots.append(snapshot_workspace)
        assert base_sha == parent.base_sha and snapshot_workspace == parent.workspace
        return create(manager, task_id, base_sha, snapshot_workspace=snapshot_workspace)

    monkeypatch.setattr(WorkspaceManager, "create", from_snapshot)
    provider.actions = edit_actions("Rerun from the private base.")
    response = await client.post(f"/api/file-agent/tasks/{parent.id}/rerun", json={})
    assert response.status_code == 202
    rerun = await finish(svc, response.json()["id"])
    assert rerun.status == "completed", rerun.error
    assert snapshots == [parent.workspace]
    assert rerun.base_sha == parent.base_sha and rerun.parent_id == parent.id
    assert "Rerun from the private base." in (Path(rerun.workspace["path"]) / "README.md").read_text()
    assert (source / "README.md").read_text() == "# Project\n\nOriginal.\n"


@async_test
async def test_agent_limits_and_stale_rag_metadata(env, monkeypatch):
    svc, provider, _, source = env
    task = svc._insert("Read project", "test", git(source, "rev-parse", "HEAD"), svc.parameters())
    workspace = svc.manager().create(task.id, task.base_sha)
    agent = FileAgent(svc.settings, provider, svc.sessions, lambda **event: None)
    provider.actions = [{"tool": "file_list", "arguments": {}}]
    params = {**svc.parameters(), "max_iterations": 1}
    with pytest.raises(RuntimeError, match="iteration limit"):
        await agent.run(workspace, "Read project", "test", params)
    assert agent.usage["llm_calls"] == 1
    with pytest.raises(RuntimeError, match="context budget"):
        await agent.run(workspace, "Read project", "test", {**params, "max_cumulative_context_bytes": 1})

    class Retriever:
        def __init__(self, sessions):
            assert sessions is svc.sessions

        def search(self, *args, **kwargs):
            return [{"content": "old context", "metadata": {"path": "README.md", "commit": "old"}},
                    {"content": "excluded", "metadata": {"path": ".env", "commit": "old"}}]

    monkeypatch.setattr("app.file_agent.agent.HybridRetriever", Retriever)
    rag = agent.rag_search("docs", task.base_sha)
    assert len(rag["results"]) == 1 and rag["results"][0]["stale"] is True
    assert rag["results"][0]["workspace_base_sha"] == task.base_sha


@async_test
async def test_create_related_file_redaction_and_artifact_tampering(env):
    svc, provider, client, source = env
    provider.actions = [
        {"tool": "file_read", "arguments": {"path": "README.md"}},
        {"tool": "file_create", "arguments": {"path": "docs/guide.md", "content": "# Guide\n\n[Project](../README.md)\n"}},
        {"result": 'Created docs/guide.md. password="must-not-leak"'},
    ]
    response = await client.post("/api/file-agent/tasks", json={"prompt": "Create docs"}, headers={"Origin": "http://localhost:3000"})
    task = await finish(svc, response.json()["id"])
    assert task.status == "completed", task.error
    assert task.changes[0]["status"] == "added"
    assert not (source / "docs/guide.md").exists()
    assert "must-not-leak" not in task.result
    assert task.workspace["artifact"]["sha256"]
    # Host-side changes cannot be mistaken for the earlier validated bytes.
    (Path(task.workspace["path"]) / "docs/guide.md").write_text("# Tampered\n")
    svc.settings.file_agent_allow_source_apply = True
    denied = await client.post(f"/api/file-agent/tasks/{task.id}/apply", json={"confirmed": True, "artifact_sha256": task.workspace["artifact"]["sha256"]})
    assert denied.status_code == 409
    assert svc.require(task.id).validation["ok"] is False
    assert not (source / "docs/guide.md").exists()
    replay = await client.post(f"/api/file-agent/tasks/{task.id}/replay", json={})
    replayed = await finish(svc, replay.json()["id"])
    assert replayed.status == "completed"
    assert "Tampered" not in (Path(replayed.workspace["path"]) / "docs/guide.md").read_text()


@async_test
async def test_singleton_jobs_survive_without_event_stream_and_log_timeout(env):
    svc, provider, client, _ = env
    entered = asyncio.Event()
    release = asyncio.Event()
    complete = provider.complete_json

    async def blocked(messages, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await complete(messages, **kwargs)

    provider.complete_json = blocked
    provider.actions = [
        {"tool": "file_read", "arguments": {"path": "README.md"}}, {"result": "First task finished"},
        {"tool": "file_read", "arguments": {"path": "Main.java"}}, {"result": "Second task finished"},
    ]
    first = (await client.post("/api/file-agent/tasks", json={"prompt": "Read docs"})).json()
    await asyncio.wait_for(entered.wait(), 20)
    second = (await client.post("/api/file-agent/tasks", json={"prompt": "Read Java"})).json()
    assert svc.require(first["id"]).status == "running"
    assert svc.require(second["id"]).status == "queued"
    assert (await client.post(f"/api/file-agent/tasks/{first['id']}/discard", json={"confirmed": True})).status_code == 409
    release.set()
    await finish(svc, first["id"])
    assert svc.require(first["id"]).status == "completed"
    assert svc.require(second["id"]).status == "completed"
    assert not svc.jobs
    events = []
    fast = svc.settings.model_copy(update={"file_agent_tool_timeout_seconds": 0.01})
    agent = FileAgent(fast, provider, svc.sessions, lambda **event: events.append(event))
    with pytest.raises(TimeoutError):
        await agent.operation("slow_tool", {}, lambda: asyncio.sleep(1))
    assert [event["status"] for event in events] == ["started", "error"]
    assert events[-1]["error"] == "Operation timed out."
