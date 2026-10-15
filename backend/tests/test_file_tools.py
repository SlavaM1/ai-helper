from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from app.file_agent import Files, WorkspaceManager
from app.file_agent.files import git as guarded_git
from app.file_agent.validation import validate
from app.file_agent.workspace import PartialApplyError
from app.mcp_tools import GitMcpClient


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args], check=True, capture_output=True).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "File Tools Test")
    git(root, "config", "user.email", "file-tools@example.com")
    (root / "README.md").write_text("# Example\n\nWelcome.\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "Main.java").write_text("public class Main { }\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root


def manager_for(repo: Path) -> WorkspaceManager:
    return WorkspaceManager(repo, repo.parent / "workspaces")


def test_clone_is_standalone_committed_only_and_source_untouched(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("dirty source\n")
    (repo / "untracked.txt").write_text("never copy this\n")
    manager = manager_for(repo)
    record = manager.create("task_34")
    clone = Path(record["path"])
    assert set(record) == {"id", "path", "base_sha", "created_at", "state"}
    assert record["base_sha"] == base and record["state"] == "ready"
    assert (clone / "README.md").read_text().startswith("# Example")
    assert not (clone / "untracked.txt").exists()
    assert (clone / ".git").is_dir()
    assert not (clone / ".git" / "objects" / "info" / "alternates").exists()
    assert git(clone, "rev-parse", "HEAD") == base
    result = subprocess.run(["git", "-C", str(clone), "symbolic-ref", "-q", "HEAD"], check=False, capture_output=True)
    assert result.returncode == 1
    assert all(path.stat().st_nlink == 1 for path in (clone / ".git" / "objects").rglob("*") if path.is_file())
    assert (repo / "README.md").read_text() == "dirty source\n"
    assert "untracked.txt" in git(repo, "status", "--porcelain")


@pytest.mark.parametrize("path", [
    "../escape", "/etc/passwd", "src/../../escape", "src/./Main.java", "src//Main.java", "src\\Main.java",
    ".git/config", ".GIT/config", ".env", ".env.production", "src/.env.local", "secrets.json",
    "credentials.json", "api_key.txt", "private.key", "id_rsa", "node_modules/a.js", "target/a.java",
    "build/a.txt", "src/../README.md", "a\nb", "C:/Windows/file", ".file-agent-owners/x.json",
])
def test_rejects_unsafe_paths_for_every_path_tool(repo: Path, path: str) -> None:
    files = Files(repo)
    operations = [
        lambda: files.file_read(path), lambda: files.file_create(path, "bad"),
        lambda: files.file_apply_patch(path, "0" * 64, "x", "y"),
        lambda: files.file_list(path), lambda: files.file_search("x", path), lambda: files.file_diff(path),
    ]
    for operation in operations:
        with pytest.raises((ValueError, OSError)):
            operation()


def test_create_read_patch_exact_unique_and_hash(repo: Path) -> None:
    files = Files(repo)
    created = files.file_create("docs/new.md", "# New\n\nOriginal.\n")
    read = files.file_read("docs/new.md", start_line=2, end_line=3)
    assert read["content"] == "2: \n3: Original."
    assert read["sha256"] == created["sha256"] == hashlib.sha256((repo / "docs/new.md").read_bytes()).hexdigest()
    result = files.file_apply_patch("docs/new.md", read["sha256"], "Original.", "Changed.")
    assert result["replacements"] == 1 and result["sha256"] != read["sha256"]
    with pytest.raises(ValueError, match="stale"):
        files.file_apply_patch("docs/new.md", read["sha256"], "Changed.", "bad")
    with pytest.raises(FileExistsError):
        files.file_create("docs/new.md", "overwrite")
    repeated = files.file_create("repeated.txt", "aaa")
    for old in ("", "aa", "missing"):
        with pytest.raises(ValueError):
            files.file_apply_patch("repeated.txt", repeated["sha256"], old, "no")
    assert (repo / "repeated.txt").read_text() == "aaa"


def test_glob_exclusions_and_status_identity(repo: Path) -> None:
    files = Files(repo)
    assert files.file_list(pattern="*.java")["files"] == ["src/Main.java"]
    assert files.file_list(pattern="*", exclude=["src/*"])["files"] == ["README.md"]
    assert files.file_status()["commit"] == git(repo, "rev-parse", "HEAD")
    assert files.file_status()["workspace_id"] == repo.name
    assert files.file_read("src/Main.java")["type"] == "text/x-java-source"


def test_spring_xml_yaml_continuations_and_structured_secrets(repo: Path) -> None:
    from app.file_agent.agent import public_value
    from app.file_agent.files import redact_text

    text = ('<property name="spring.mail.password" value="XML_ATTR_VALUE"/>\n'
            '<entry key="password">XML_BODY_VALUE</entry>\n'
            'password: YAML_FIRST_VALUE\n  YAML_CONTINUATION_VALUE\nother: visible\n')
    clean = redact_text(text)
    assert clean.count("\n") == text.count("\n")
    for secret in ("XML_ATTR_VALUE", "XML_BODY_VALUE", "YAML_FIRST_VALUE", "YAML_CONTINUATION_VALUE"):
        assert secret not in clean
    assert "other: visible" in clean
    assert public_value({"password": "STRUCTURED_VALUE", "nested": {"apiKey": "OTHER_VALUE"}}) == {
        "password": "[REDACTED]", "nested": {"apiKey": "[REDACTED]"}}
    assert public_value({"total_tokens": 10}) == {"total_tokens": 10}
    files = Files(repo)
    files.file_create("config.xml", text)
    assert "XML_ATTR_VALUE" not in files.file_read("config.xml")["content"]
    assert not files.file_search("XML_ATTR_VALUE")["matches"]
    assert "XML_BODY_VALUE" not in files.file_diff()["diff"]


def test_no_symlinks_hardlinks_or_special_files(repo: Path, tmp_path: Path) -> None:
    external = tmp_path / "outside"
    external.mkdir()
    victim = external / "victim.txt"
    victim.write_text("outside secret")
    (repo / "linked").symlink_to(external, target_is_directory=True)
    (repo / "leaf.txt").symlink_to(victim)
    os.link(victim, repo / "hard.txt")
    os.mkfifo(repo / "pipe.txt")
    files = Files(repo)
    for path in ("linked/victim.txt", "leaf.txt", "hard.txt", "pipe.txt"):
        with pytest.raises((OSError, ValueError)):
            files.file_read(path)
        with pytest.raises((OSError, ValueError)):
            files.file_apply_patch(path, hashlib.sha256(b"outside secret").hexdigest(), "outside", "bad")
        with pytest.raises((OSError, ValueError)):
            files.file_create(path, "bad")
        for operation in (files.file_list, files.file_diff):
            with pytest.raises((OSError, ValueError)):
                operation(path)
        with pytest.raises((OSError, ValueError)):
            files.file_search("outside", path)
    assert not {"leaf.txt", "hard.txt", "pipe.txt"} & set(files.file_list()["files"])
    assert not files.file_search("outside secret")["matches"]
    assert victim.read_text() == "outside secret"


def test_atomic_write_leaf_swap_does_not_follow_symlink(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "victim.txt"
    outside.write_text("must survive\n")
    files = Files(repo)
    read = files.file_read("README.md")
    real_replace = os.replace

    def swap_before_replace(src: str, dst: str, **kwargs: object) -> None:
        if dst == "README.md":
            (repo / "README.md").unlink()
            (repo / "README.md").symlink_to(outside)
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", swap_before_replace)
    files.file_apply_patch("README.md", read["sha256"], "Welcome.", "Safe replacement.")
    assert outside.read_text() == "must survive\n"
    assert not (repo / "README.md").is_symlink()


def test_atomic_write_parent_swap_fails_closed(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    external = tmp_path / "outside"
    external.mkdir()
    (external / "Main.java").write_text("must survive")
    files = Files(repo)
    expected = files.file_read("src/Main.java")["sha256"]
    real_atomic = files._atomic_write

    def swap_parent(fd: int, path: str, data: bytes, *, existing: bytes | None) -> None:
        (repo / "src").rename(repo / "old-src")
        (repo / "src").symlink_to(external, target_is_directory=True)
        real_atomic(fd, path, data, existing=existing)

    monkeypatch.setattr(files, "_atomic_write", swap_parent)
    with pytest.raises(OSError):
        files.file_apply_patch("src/Main.java", expected, "Main", "Changed")
    assert (external / "Main.java").read_text() == "must survive"


def test_persistent_change_byte_and_count_limits(repo: Path) -> None:
    options = {"max_total_write_bytes": 10, "max_operation_bytes": 8}
    Files(repo, **options).file_create("one.txt", "123456")
    with pytest.raises(ValueError, match="total change budget"):
        Files(repo, **options).file_create("two.txt", "12345")
    assert not (repo / "two.txt").exists()
    with pytest.raises(ValueError, match="per-operation"):
        Files(repo, **options).file_create("big.txt", "123456789")
    with pytest.raises(ValueError, match="file limit"):
        Files(repo, max_files=3).file_create("fourth.txt", "small")
    for content in ("binary\x00payload",):
        with pytest.raises(ValueError, match="binary"):
            Files(repo).file_create("binary.txt", content)
    (repo / "invalid.txt").write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        Files(repo).file_read("invalid.txt")


def test_redaction_search_diff_untracked_and_change_counts(repo: Path) -> None:
    original = "password=supersecret\npublic setting\n"
    (repo / "settings.txt").write_text(original)
    git(repo, "add", "settings.txt")
    git(repo, "commit", "-qm", "settings")
    files = Files(repo)
    read = files.file_read("settings.txt")
    assert "supersecret" not in read["content"] and "[REDACTED]" in read["content"]
    assert read["sha256"] == hashlib.sha256(original.encode()).hexdigest()
    assert not files.file_search("supersecret")["matches"]
    assert files.file_search("PASSWORD")["matches"][0]["text"] == "password=[REDACTED]"
    files.file_apply_patch("settings.txt", read["sha256"], "supersecret", "newsecret")
    files.file_create("added.txt", "first\nsecond\n")
    output = files.file_diff()
    assert "supersecret" not in output["diff"] and "newsecret" not in output["diff"]
    assert "+first" in output["diff"]
    added = next(change for change in output["changes"] if change["path"] == "added.txt")
    assert added["status"] == "added" and added["additions"] == 2 and added["deletions"] == 0
    bounded = files.file_diff(max_bytes=20)
    assert bounded["bytes"] <= 20 and bounded["truncated"]
    assert files.file_status()["count"] == 2


def test_redaction_of_quoted_values_and_multiline_private_keys(repo: Path) -> None:
    files = Files(repo)
    content = ('password="two words with \\"escaped\\" quotes"\n'
               'Authorization: Bearer abcdef.123456\n'
               '-----BEGIN PRIVATE KEY-----\nTOPSECRETLINE\n-----END PRIVATE KEY-----\n')
    created = files.file_create("settings.txt", content)
    read = files.file_read("settings.txt")
    assert read["sha256"] == created["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    for hidden in ("two words", "escaped", "abcdef.123456", "TOPSECRETLINE"):
        assert hidden not in read["content"]
        assert not files.file_search(hidden)["matches"]
        assert hidden not in files.file_diff()["diff"]
    assert read["total_lines"] == len(content.splitlines())


def test_export_replay_apply_opt_in_and_dirty_source_protection(repo: Path) -> None:
    manager = manager_for(repo)
    record = manager.create("patch")
    files = Files(record["path"])
    read = files.file_read("README.md")
    files.file_apply_patch("README.md", read["sha256"], "Welcome.", "Updated.")
    files.file_create('docs/a "quoted" é.md', "# Export\nNo trailing newline")
    files.file_create("empty.txt", "")
    artifact = manager.save_artifact(record)
    assert Path(artifact["patch_path"]).is_file()
    assert Path(artifact["metadata_path"]).is_file()
    assert Path(artifact["patch_path"]).stat().st_mode & 0o077 == 0
    replayed = manager.replay(record)
    assert Files(replayed["path"]).file_status()["changes"] == files.file_status()["changes"]
    assert (Path(replayed["path"]) / 'docs/a "quoted" é.md').read_text().endswith("No trailing newline")
    assert (Path(replayed["path"]) / "empty.txt").exists()
    with pytest.raises(PermissionError, match="explicit host"):
        manager.apply(record)
    (repo / "README.md").write_text("user work\n")
    with pytest.raises(ValueError, match="touched path"):
        manager.apply(record, allow_source_write=True)
    assert (repo / "README.md").read_text() == "user work\n"
    (repo / "README.md").write_text("# Example\n\nWelcome.\n")
    # Unrelated changes must be preserved, not cause broad resets.
    (repo / "unrelated.txt").write_text("keep me\n")
    result = manager.apply(record, allow_source_write=True)
    assert result["applied"]
    assert "Updated." in (repo / "README.md").read_text()
    assert (repo / "unrelated.txt").read_text() == "keep me\n"


def test_export_replay_deletions_and_missing_newline(repo: Path) -> None:
    (repo / "no-newline.txt").write_text("before")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "no newline")
    manager = manager_for(repo)
    record = manager.create("delete")
    root = Path(record["path"])
    (root / "src/Main.java").unlink()
    files = Files(root)
    files.file_apply_patch("no-newline.txt", files.file_read("no-newline.txt")["sha256"], "before", "after")
    artifact = manager.save_artifact(record)
    assert any(change["status"] == "deleted" for change in artifact["changes"])
    replayed = Path(manager.replay(record)["path"])
    assert not (replayed / "src/Main.java").exists()
    assert (replayed / "no-newline.txt").read_bytes() == b"after"


def test_source_apply_checks_index_and_head_without_running_repo_filters(repo: Path, tmp_path: Path) -> None:
    (repo / ".gitattributes").write_text("README.md filter=untrusted\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-qm", "attributes")
    executed = tmp_path / "filter-was-executed"
    git(repo, "config", "filter.untrusted.clean", f"touch {executed}; cat")
    git(repo, "config", "filter.untrusted.smudge", f"touch {executed}; cat")
    git(repo, "config", "core.fsmonitor", f"touch {executed}")
    manager = manager_for(repo)
    record = manager.create("safe-config")
    files = Files(record["path"])
    read = files.file_read("README.md")
    files.file_apply_patch("README.md", read["sha256"], "Welcome.", "Updated.")
    manager.save_artifact(record)
    # Stage a different blob directly, without invoking a configured filter.
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=b"staged\n", capture_output=True, check=True).stdout.decode().strip()
    git(repo, "update-index", "--cacheinfo", "100644", blob, "README.md")
    with pytest.raises(ValueError, match="touched path"):
        manager.apply(record, allow_source_write=True)
    original_blob = git(repo, "rev-parse", "HEAD:README.md")
    git(repo, "update-index", "--cacheinfo", "100644", original_blob, "README.md")
    manager.apply(record, allow_source_write=True)
    assert not executed.exists()
    # A commit made after workspace creation must always prevent source apply.
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    commit = git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "new HEAD")
    git(repo, "update-ref", "HEAD", commit)
    with pytest.raises(ValueError, match="HEAD"):
        manager.apply(record, allow_source_write=True)
    assert not executed.exists()


def test_artifact_integrity_readonly_and_owned_discard(repo: Path, tmp_path: Path) -> None:
    manager = manager_for(repo)
    record = manager.create("owned")
    Files(record["path"]).file_create("new.txt", "hello\n")
    artifact = manager.save_artifact(record)
    repo.chmod(0o555)
    try:
        with pytest.raises(PermissionError, match="read-only"):
            manager.apply(record, allow_source_write=True)
    finally:
        repo.chmod(0o755)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep")
    forged = {**record, "path": str(unrelated)}
    # Caller-supplied paths are ignored; only externally stored ownership wins.
    assert manager.discard(forged)["state"] == "discarded"
    assert (unrelated / "keep.txt").read_text() == "keep"
    assert Path(artifact["patch_path"]).is_file()
    with pytest.raises((ValueError, FileNotFoundError)):
        manager.discard("unrelated")
    Path(artifact["patch_path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        manager.replay(record)


def test_discard_rejects_replaced_workspace_and_preserves_symlink_targets(repo: Path, tmp_path: Path) -> None:
    manager = manager_for(repo)
    record = manager.create("replace")
    root = Path(record["path"])
    saved = root.with_name(root.name + "-moved")
    root.rename(saved)
    root.mkdir()
    (root / "keep.txt").write_text("unowned")
    with pytest.raises(ValueError, match="replaced"):
        manager.discard(record)
    (root / "keep.txt").unlink()
    root.rmdir()
    saved.rename(root)
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("keep")
    (root / "link").symlink_to(external, target_is_directory=True)
    manager.discard(record)
    assert (external / "keep.txt").read_text() == "keep"


def test_markdown_and_java_validation_never_executes(repo: Path) -> None:
    files = Files(repo)
    files.file_create("docs/check.md", "# First\n### Skipped\n[missing](absent.md)\n[escape](../../outside.txt)\n[env](../.env)\n[ok](../README.md)\n[anchor](#absent)\n")
    markdown = validate(files, "docs/check.md")
    codes = [item["code"] for item in markdown["diagnostics"]]
    assert "heading-level" in codes and "missing-anchor" in codes
    assert codes.count("invalid-local-link") == 3
    assert markdown["executed"] is False
    java = validate(files, "src/Main.java")
    assert java["kind"] == "java-heuristic" and java["compiled"] is False and java["ok"]
    files.file_create("Broken.java", "class Broken {\n")
    assert not validate(files, "Broken.java")["ok"]
    with pytest.raises(ValueError, match="compilation is disabled"):
        validate(files, "src/Main.java", compile=True)


def test_real_fastmcp_stdio_operations_and_restart_persistence(repo: Path) -> None:
    async def exercise() -> None:
        record = manager_for(repo).create("mcp")
        client = GitMcpClient(record["path"], server_module="app.file_agent.mcp_server")
        names = {tool["name"] for tool in await client.list_tools()}
        assert names == {"file_list", "file_search", "file_read", "file_create", "file_apply_patch", "file_diff", "file_status", "file_validate"}
        assert "README.md" in (await client.call("file_list"))["files"]
        created = await client.call("file_create", {"path": "stdio.txt", "content": "first\n"})
        read = await client.call("file_read", {"path": "stdio.txt"})
        assert read["sha256"] == created["sha256"] and read["content"] == "1: first"
        await client.call("file_apply_patch", {"path": "stdio.txt", "expected_hash": read["sha256"], "old_text": "first", "new_text": "second"})
        assert (await client.call("file_search", {"query": "second"}))["matches"][0]["path"] == "stdio.txt"
        assert (await client.call("file_status"))["total_change_bytes"] == len("second\n")
        assert "+second" in (await client.call("file_diff"))["diff"]
        assert (await client.call("file_validate", {"path": "src/Main.java"}))["compiled"] is False
        for name, arguments in [("file_read", {"path": "../source/README.md"}), ("file_create", {"path": ".git/config", "content": "bad"})]:
            with pytest.raises(RuntimeError):
                await client.call(name, arguments)
        assert not (repo / "stdio.txt").exists()

    asyncio.run(exercise())


def test_git_rejects_promisor_lazy_fetch_command_execution(repo: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    subprocess.run(["git", "clone", "--no-hardlinks", str(repo), str(remote)], check=True, capture_output=True)
    marker = tmp_path / "uploadpack-executed"
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "config", "core.repositoryformatversion", "1")
    git(repo, "config", "extensions.partialClone", "origin")
    git(repo, "config", "remote.origin.url", str(remote))
    git(repo, "config", "remote.origin.promisor", "true")
    git(repo, "config", "remote.origin.uploadpack", f"touch {marker}; git-upload-pack")
    (repo / ".git" / "objects" / sha[:2] / sha[2:]).unlink()
    before = (repo / ".git" / "config").read_bytes()
    with pytest.raises(ValueError, match="promisor"):
        guarded_git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    with pytest.raises(ValueError, match="promisor"):
        manager_for(repo).create("untrusted")
    assert not marker.exists()
    assert not (repo / ".git" / "objects" / sha[:2] / sha[2:]).exists()
    assert (repo / ".git" / "config").read_bytes() == before


def test_git_isolates_included_config_even_without_lazy_fetch_flag(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = tmp_path / "remote"
    subprocess.run(["git", "clone", "--no-hardlinks", str(repo), str(remote)], check=True, capture_output=True)
    marker = tmp_path / "uploadpack-executed"
    sha = git(repo, "rev-parse", "HEAD")
    include = tmp_path / "included-config"
    include.write_text('[extensions]\npartialClone = origin\n[remote "origin"]\n'
                       f'url = {remote}\npromisor = true\nuploadpack = touch {marker}; git-upload-pack\n')
    git(repo, "config", "include.path", str(include))
    (repo / ".git" / "objects" / sha[:2] / sha[2:]).unlink()
    real_popen = subprocess.Popen
    invocations = []

    def old_git(*args, **kwargs):
        environment = kwargs.get("env")
        if environment is not None:
            assert environment["GIT_NO_LAZY_FETCH"] == "1"
            invocations.append(args[0])
            kwargs["env"] = {key: value for key, value in environment.items() if key != "GIT_NO_LAZY_FETCH"}
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", old_git)
    with pytest.raises(RuntimeError, match="Git operation failed"):
        guarded_git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    assert len(invocations) == 2  # config-as-data parser, then isolated object read
    assert not marker.exists()
    assert not (repo / ".git" / "objects" / sha[:2] / sha[2:]).exists()


@pytest.mark.parametrize("kind", ["alternates", "promisor-pack"])
def test_git_rejects_incomplete_object_stores(repo: Path, kind: str) -> None:
    if kind == "alternates":
        (repo / ".git" / "objects" / "info" / "alternates").write_text("/untrusted/objects\n")
    else:
        (repo / ".git" / "objects" / "pack" / "missing.promisor").touch()
    with pytest.raises(ValueError, match="not supported"):
        manager_for(repo).create("incomplete")


def test_base_bundle_survives_discard_rewrite_and_source_object_pruning(repo: Path) -> None:
    manager = manager_for(repo)
    work = manager.create("durable")
    files = Files(work["path"])
    files.file_apply_patch("README.md", files.file_read("README.md")["sha256"], "Welcome.", "Replayed.")
    artifact = manager.save_artifact(work)
    bundle = manager.metadata / (work["id"] + ".bundle")
    assert bundle.stat().st_mode & 0o777 == 0o600
    manager.discard(work)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    replacement = git(repo, "commit-tree", tree, "-m", "unrelated replacement")
    git(repo, "update-ref", "HEAD", replacement)
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "--prune=now")
    missing = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", work["base_sha"]], check=False, capture_output=True)
    assert missing.returncode != 0
    replayed = manager.replay(work)
    assert (Path(replayed["path"]) / "README.md").read_text().endswith("Replayed.\n")
    assert manager.save_artifact(replayed)["sha256"] == artifact["sha256"]
    rerun = manager.create("rerun", work["base_sha"], snapshot_workspace=work)
    assert rerun["base_sha"] == work["base_sha"]
    assert (Path(rerun["path"]) / "README.md").read_text().endswith("Welcome.\n")
    bundle.write_bytes(bundle.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="snapshot integrity"):
        manager.create("tampered", work["base_sha"], snapshot_workspace=work)


def test_base_bundle_size_is_bounded(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.file_agent.workspace as module

    monkeypatch.setattr(module, "_MAX_BUNDLE_BYTES", 32)
    with pytest.raises(ValueError, match="Git output exceeds"):
        manager_for(repo).create("oversized")


@pytest.fixture
def shallow_repo(repo: Path, tmp_path: Path) -> Path:
    (repo / "README.md").write_text("# Shallow project\n\nWelcome.\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "shallow boundary")
    (repo / "details.txt").write_text("Latest commit\n")
    git(repo, "add", "details.txt")
    git(repo, "commit", "-qm", "head after boundary")
    source = tmp_path / "shallow-source"
    subprocess.run(["git", "clone", "--depth=2", "--template=", "--", repo.as_uri(), str(source)], check=True, capture_output=True)
    assert git(source, "rev-parse", "--is-shallow-repository") == "true"
    assert (source / ".git" / "shallow").read_text().strip() != git(source, "rev-parse", "HEAD")
    return source


def test_shallow_base_read_edit_export_discard_replay_and_rerun_preserve_source(shallow_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.file_agent.workspace as module

    source = shallow_repo
    original = {str(path.relative_to(source)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in source.rglob("*") if path.is_file()}
    boundary = (source / ".git" / "shallow").read_bytes()
    manager = manager_for(source)
    work = manager.create("shallow")
    root = Path(work["path"])
    assert (root / ".git" / "shallow").read_bytes() == boundary
    assert guarded_git(root, "rev-parse", "--is-shallow-repository").strip() == b"true"
    files = Files(root)
    read = files.file_read("README.md")
    files.file_apply_patch("README.md", read["sha256"], "Welcome.", "Replayed from shallow snapshot.")
    files.file_create("docs/new.md", "# New document\n")
    assert files.file_status()["count"] == 2
    assert "+Replayed from shallow snapshot." in files.file_diff()["diff"]
    artifact = manager.save_artifact(work)
    private_boundary = manager.metadata / (work["id"] + ".shallow")
    assert private_boundary.read_bytes() == boundary
    assert private_boundary.stat().st_mode & 0o777 == 0o600
    manager.discard(work)
    assert not root.exists()
    # Prove saved snapshots never consult source again, without changing even
    # the fixture source's metadata to simulate its unavailability.
    real_git = module.git

    def no_source(root, *args, **kwargs):
        assert root != source, "replay/rerun must use the saved base, not source"
        return real_git(root, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(module, "git", no_source)
        replayed = manager.replay(work)
        assert (Path(replayed["path"]) / ".git" / "shallow").read_bytes() == boundary
        assert "Replayed from shallow snapshot." in (Path(replayed["path"]) / "README.md").read_text()
        assert manager.save_artifact(replayed)["sha256"] == artifact["sha256"]
        rerun = manager.create("shallow-rerun", work["base_sha"], snapshot_workspace=work)
        assert (Path(rerun["path"]) / ".git" / "shallow").read_bytes() == boundary
        assert (Path(rerun["path"]) / "README.md").read_text().endswith("Welcome.\n")
    assert {str(path.relative_to(source)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in source.rglob("*") if path.is_file()} == original
    private_boundary.write_bytes(b"0" * 40 + b"\n")
    with pytest.raises(ValueError, match="shallow snapshot integrity"):
        manager.create("tampered-shallow", snapshot_workspace=work)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "invalid", "oversized"])
def test_shallow_metadata_is_descriptor_validated_and_bounded(repo: Path, tmp_path: Path, kind: str) -> None:
    marker = repo / ".git" / "shallow"
    external = tmp_path / "outside-boundary"
    external.write_bytes((git(repo, "rev-parse", "HEAD") + "\n").encode())
    original = external.read_bytes()
    if kind == "symlink":
        marker.symlink_to(external)
    elif kind == "hardlink":
        os.link(external, marker)
    elif kind == "fifo":
        os.mkfifo(marker)
    elif kind == "invalid":
        marker.write_bytes(b"../objects\n")
    else:
        marker.write_bytes(b"a" * 1_000_001)
    with pytest.raises((ValueError, OSError)):
        manager_for(repo).create("invalid-shallow")
    assert external.read_bytes() == original


def test_unchanged_large_tracked_assets_do_not_block_bounded_text_edits(repo: Path) -> None:
    content = b"\x00unchanged binary asset\n" * 60_000
    (repo / "large.jpg").write_bytes(content)
    git(repo, "add", "large.jpg")
    git(repo, "commit", "-qm", "large tracked asset")
    manager = manager_for(repo)
    work = manager.create("large-baseline")
    root = Path(work["path"])
    files = Files(root)
    with pytest.raises(ValueError, match="file exceeds size limit"):
        files.file_read("large.jpg")
    files.file_create("docs/small.md", "# Small edit\n")
    assert [change["path"] for change in files.file_status()["changes"]] == ["docs/small.md"]
    assert "large.jpg" not in files.file_diff()["diff"]
    artifact = manager.save_artifact(work)
    replayed = manager.replay(work)
    assert manager.save_artifact(replayed)["sha256"] == artifact["sha256"]
    assert (Path(replayed["path"]) / "large.jpg").read_bytes() == content
    with pytest.raises(ValueError, match="scan byte limit"):
        Files(root, max_scan_bytes=1_000_000).file_status()
    (root / "large.jpg").write_bytes(content + b"changed")
    with pytest.raises(ValueError, match="file exceeds size limit"):
        files.file_status()
    assert (repo / "large.jpg").read_bytes() == content
    assert not (repo / "docs").exists()


def edited_workspace(repo: Path, *, delete: bool = False) -> tuple[WorkspaceManager, dict]:
    manager = manager_for(repo)
    work = manager.create("transaction")
    root = Path(work["path"])
    files = Files(root)
    if delete:
        (root / "README.md").unlink()
    else:
        files.file_apply_patch("README.md", files.file_read("README.md")["sha256"], "Welcome.", "Updated.")
    files.file_create("docs/new.md", "# Created\n")
    files.file_apply_patch("src/Main.java", files.file_read("src/Main.java")["sha256"], "Main", "Renamed")
    manager.save_artifact(work)
    return manager, work


@pytest.mark.parametrize("delete", [False, True])
def test_apply_rolls_back_earlier_publications_on_write_failure(repo: Path, monkeypatch: pytest.MonkeyPatch, delete: bool) -> None:
    manager, work = edited_workspace(repo, delete=delete)
    original = {path: (repo / path).read_bytes() for path in ("README.md", "src/Main.java")}
    real_replace = os.replace

    def no_space(src, dst, **kwargs):
        if dst == "Main.java":
            raise OSError(errno.ENOSPC, "simulated publication failure")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", no_space)
    with pytest.raises(RuntimeError, match="source apply failed"):
        manager.apply(work, allow_source_write=True)
    assert {path: (repo / path).read_bytes() for path in original} == original
    assert not (repo / "docs").exists()
    assert not list(repo.rglob(".file-agent-*"))


def test_apply_preflights_all_staged_writes_before_publication(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, work = edited_workspace(repo)
    real_open = os.open
    staged = 0

    def no_space(path, flags, *args, **kwargs):
        nonlocal staged
        if isinstance(path, str) and path.startswith(".file-agent-stage-"):
            staged += 1
            if staged == 2:
                raise OSError(errno.ENOSPC, "simulated staging failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_space)
    with pytest.raises(RuntimeError, match="source apply failed"):
        manager.apply(work, allow_source_write=True)
    assert (repo / "README.md").read_text().endswith("Welcome.\n")
    assert "class Main" in (repo / "src/Main.java").read_text()
    assert not (repo / "docs").exists()
    assert not list(repo.rglob(".file-agent-*"))


def test_apply_revalidates_under_one_lock_after_staging(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, work = edited_workspace(repo)
    real_check = WorkspaceManager._check_target
    calls = 0

    def check(target, artifact, *, fd=None):
        nonlocal calls
        calls += 1
        assert fd is not None
        other = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)
        if calls == 2:
            (repo / "README.md").write_text("user edit during staging\n")
        return real_check(target, artifact, fd=fd)

    monkeypatch.setattr(WorkspaceManager, "_check_target", staticmethod(check))
    with pytest.raises(ValueError, match="touched path"):
        manager.apply(work, allow_source_write=True)
    assert calls == 2
    assert (repo / "README.md").read_text() == "user edit during staging\n"
    assert "class Main" in (repo / "src/Main.java").read_text()
    assert not (repo / "docs").exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_apply_rollback_preserves_concurrent_edits_and_reports_partial(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink: bool) -> None:
    manager, work = edited_workspace(repo)
    original = (repo / "README.md").read_bytes()
    external = tmp_path / "external.md"
    external.write_text("external edit after publication\n")
    real_replace = os.replace

    def conflict(src, dst, **kwargs):
        if dst == "Main.java":
            if symlink:
                (repo / "README.md").unlink()
                (repo / "README.md").symlink_to(external)
            else:
                (repo / "README.md").write_text("external edit after publication\n")
            raise OSError(errno.ENOSPC, "simulated publication failure")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", conflict)
    with pytest.raises(PartialApplyError, match="Partial apply"):
        manager.apply(work, allow_source_write=True)
    assert (repo / "README.md").read_text() == "external edit after publication\n"
    assert (repo / "README.md").is_symlink() is symlink
    assert external.read_text() == "external edit after publication\n"
    assert "class Main" in (repo / "src/Main.java").read_text()
    assert not (repo / "docs").exists()
    recovery = list(repo.glob(".file-agent-rollback-*"))
    assert len(recovery) == 1 and recovery[0].read_bytes() == original
    assert recovery[0].stat().st_mode & 0o777 == 0o600


def test_apply_rejects_replaced_staged_file_without_following_symlinks(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, work = edited_workspace(repo)
    external = tmp_path / "external.md"
    external.write_text("outside must survive\n")
    real_check = WorkspaceManager._check_target
    calls = 0

    def check(target, artifact, *, fd=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            staged = next(repo.glob(".file-agent-stage-*"))
            staged.unlink()
            staged.symlink_to(external)
        return real_check(target, artifact, fd=fd)

    monkeypatch.setattr(WorkspaceManager, "_check_target", staticmethod(check))
    with pytest.raises(ValueError, match="staged file changed"):
        manager.apply(work, allow_source_write=True)
    assert (repo / "README.md").read_text().endswith("Welcome.\n")
    assert external.read_text() == "outside must survive\n"
    assert not (repo / "docs").exists()
    assert not list(repo.rglob(".file-agent-*"))


def test_apply_rolls_back_after_publication_fsync_failure(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, work = edited_workspace(repo)
    real_fsync = os.fsync
    identity = (repo.stat().st_dev, repo.stat().st_ino)
    failed = False

    def fail_once(fd):
        nonlocal failed
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity and not failed:
            failed = True
            raise OSError(errno.ENOSPC, "simulated directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_once)
    with pytest.raises(RuntimeError, match="source apply failed"):
        manager.apply(work, allow_source_write=True)
    assert failed
    assert (repo / "README.md").read_text().endswith("Welcome.\n")
    assert "class Main" in (repo / "src/Main.java").read_text()
    assert not (repo / "docs").exists()
    assert not list(repo.rglob(".file-agent-*"))


def test_sha256_repository_base_bundle_replay(tmp_path: Path) -> None:
    source = tmp_path / "sha256"
    source.mkdir()
    git(source, "init", "-q", "--object-format=sha256")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.test")
    (source / "README.md").write_text("# SHA-256\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "base")
    manager = manager_for(source)
    work = manager.create("sha256")
    assert len(work["base_sha"]) == 64
    files = Files(work["path"])
    files.file_apply_patch("README.md", files.file_read("README.md")["sha256"], "SHA-256", "Updated")
    manager.save_artifact(work)
    manager.discard(work)
    replayed = manager.replay(work)
    assert (Path(replayed["path"]) / "README.md").read_text() == "# Updated\n"


@pytest.mark.parametrize("content", [
    "<settings>\n<password>xml-credential</password>\n<name>visible</name>\n</settings>\n",
    '<ns:password attr="x">\nxml-credential\nsecond-credential\n</ns:password>\nvisible\n',
    "password: |\n  yaml-credential\n\n  second-credential\nvisible: yes\n",
    'nested:\r\n  "password": >+2 # folded\r\n    yaml-credential\r\n    second-credential\r\nvisible: yes\r\n',
    "- password: |-\n    yaml-credential\n    second-credential\n- visible: yes\n",
    "- password: &credential |-\n    yaml-credential\n    second-credential\n  visible: yes\n",
    "spring.datasource.password: |2+\n  yaml-credential\n# comment\n  second-credential\nvisible: yes\n",
    'password:\r\n  "yaml-credential\r\nsecond-credential"\r\nvisible: yes\r\n',
])
def test_xml_and_yaml_block_credentials_redacted_before_read_search_diff(repo: Path, content: str) -> None:
    files = Files(repo)
    files.file_create("settings.txt", content)
    read = files.file_read("settings.txt")
    assert read["total_lines"] == len(content.splitlines())
    assert read["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert "[REDACTED]" in read["content"]
    for output in (read["content"], str(files.file_search("credential")), files.file_diff()["diff"]):
        for secret in ("xml-credential", "yaml-credential", "second-credential"):
            assert secret not in output
    visible_line = next(number for number, line in enumerate(content.splitlines(), 1) if "visible" in line)
    assert f"{visible_line}: " in read["content"]
    assert files.file_search("visible")["matches"][0]["line"] == visible_line
