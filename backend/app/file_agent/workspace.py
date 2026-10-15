"""Standalone clones and host-only patch export/replay/apply lifecycle.

Ownership records and raw patch artifacts live OUTSIDE the model-accessible
checkout. Raw exported patches may contain secrets; they are private host
artifacts, never tool output. Source writes require explicit host opt-in.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import ExitStack, nullcontext
from datetime import UTC, datetime
from pathlib import Path

from .files import (
    _DIR_FLAGS,
    _FILE_FLAGS,
    _MAX_SHALLOW_BYTES,
    Files,
    digest,
    git,
    open_directory,
    parent_fd,
    safe_path,
    validate_shallow,
)

_MAX_BUNDLE_BYTES = 128_000_000


class PartialApplyError(RuntimeError):
    """Publication failed and concurrent edits prevented a non-clobber rollback."""


class WorkspaceManager:
    def __init__(self, source: Path, storage: Path) -> None:
        self.source = Path(os.path.abspath(source))
        self.storage = Path(os.path.abspath(storage))
        if self.source == self.storage or self.source in self.storage.parents or self.storage in self.source.parents:
            raise ValueError("workspace storage must be outside the source checkout")
        with open_directory(self.source):
            pass
        with open_directory(self.storage, create=True):
            pass
        self.metadata = self.storage / ".file-agent-owners"
        with open_directory(self.metadata, create=True) as fd:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("workspace metadata must be private to the current user")

    @staticmethod
    def _identifier(workspace: dict | str) -> str:
        value = workspace.get("id") if isinstance(workspace, dict) else workspace
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}-[0-9a-f]{32}", value):
            raise ValueError("invalid workspace identifier")
        return value

    def _write_private(self, name: str, data: bytes) -> Path:
        with open_directory(self.metadata) as fd:
            temporary = ".pending-" + uuid.uuid4().hex
            file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(file_fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
        return self.metadata / name

    def _private(self, name: str, maximum: int = 12_000_000) -> bytes:
        with open_directory(self.metadata) as fd:
            file_fd = os.open(name, _FILE_FLAGS, dir_fd=fd)
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_size > maximum:
                    raise ValueError("invalid workspace metadata")
                with os.fdopen(file_fd, "rb", closefd=False) as stream:
                    data = stream.read(maximum + 1)
                if len(data) > maximum:
                    raise ValueError("workspace metadata exceeds limit")
                return data
            finally:
                os.close(file_fd)

    def _record(self, workspace: dict | str, *, active: bool = True) -> dict:
        identifier = self._identifier(workspace)
        record = json.loads(self._private(identifier + ".json", 32_000))
        path = self.storage / identifier
        if record.get("id") != identifier or record.get("path") != str(path) or record.get("source") != str(self.source):
            raise ValueError("workspace ownership mismatch")
        if active:
            if record.get("state") == "discarded":
                raise ValueError("workspace has been discarded")
            with open_directory(path) as fd:
                info = os.fstat(fd)
                if [info.st_dev, info.st_ino] != record.get("identity"):
                    raise ValueError("workspace directory was replaced")
        return record

    def _save_record(self, record: dict) -> None:
        self._write_private(record["id"] + ".json", json.dumps(record, sort_keys=True).encode("utf-8"))

    @staticmethod
    def _public(record: dict) -> dict:
        return {key: record[key] for key in ("id", "path", "base_sha", "created_at", "state")}

    def create(self, task_id: str, base_sha: str | None = None,
               snapshot_workspace: dict | str | None = None) -> dict:
        """Clone a private base bundle/boundary, independent of future source refs.

        Reruns should pass the parent's workspace as snapshot_workspace; discarded
        workspaces retain their private bundles. No source Git config is loaded.
        """
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
            raise ValueError("task_id must be 1-80 letters, digits, underscores or hyphens")
        if base_sha is not None and not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", base_sha):
            raise ValueError("base_sha must be a full commit SHA")
        if snapshot_workspace is not None:
            saved = self._record(snapshot_workspace, active=False)
            if base_sha is not None and base_sha.lower() != saved["base_sha"]:
                raise ValueError("snapshot base mismatch")
            sha = saved["base_sha"]
            bundle = self._private(saved["id"] + ".bundle", _MAX_BUNDLE_BYTES)
            if len(bundle) != saved.get("bundle_bytes") or digest(bundle) != saved.get("bundle_sha256"):
                raise ValueError("base snapshot integrity check failed")
            shallow = self._private(saved["id"] + ".shallow", _MAX_SHALLOW_BYTES) if "shallow_sha256" in saved else b""
            if "shallow_sha256" in saved and (len(shallow) != saved.get("shallow_bytes") or digest(shallow) != saved["shallow_sha256"]):
                raise ValueError("base shallow snapshot integrity check failed")
        else:
            sha = git(self.source, "rev-parse", "--verify", (base_sha or "HEAD") + "^{commit}", max_bytes=128).decode("ascii").strip()
            snapshot_metadata = {}
            bundle = git(self.source, "bundle", "create", "-", "refs/heads/file-agent-base",
                         snapshot_sha=sha, max_bytes=_MAX_BUNDLE_BYTES, snapshot_metadata=snapshot_metadata)
            shallow = snapshot_metadata["shallow"]
        validate_shallow(shallow, len(sha))
        identifier = task_id + "-" + uuid.uuid4().hex
        destination = self.storage / identifier
        bundle_path = self._write_private(identifier + ".bundle", bundle)
        self._write_private(identifier + ".shallow", shallow)
        # A regular clone performs ancestry connectivity checks before we can
        # install the shallow boundary. Import shallow bundles into a clean repo
        # instead, restoring that boundary BEFORE unpacking/checking out objects.
        # Failed init/clone is not removed without a completed ownership record.
        if shallow:
            git(self.storage, "init", "--template=", "--object-format=" + ("sha256" if len(sha) == 64 else "sha1"), "--", str(destination))
        else:
            git(self.storage, "clone", "--no-hardlinks", "--no-checkout", "--no-local", "--template=", "--", str(bundle_path), str(destination))
        with open_directory(destination) as fd:
            info = os.fstat(fd)
        record = {"id": identifier, "path": str(destination), "base_sha": sha,
                  "created_at": datetime.now(UTC).isoformat(), "state": "creating",
                  "source": str(self.source), "identity": [info.st_dev, info.st_ino],
                  "bundle_sha256": digest(bundle), "bundle_bytes": len(bundle),
                  "shallow_sha256": digest(shallow), "shallow_bytes": len(shallow)}
        self._save_record(record)
        try:
            if shallow:
                # Bundle v2/v3 does not encode shallow boundaries. Retain the
                # exact validated boundary privately and restore it in OUR clone,
                # before any checkout/read/history traversal. Never write source.
                with open_directory(destination) as root_fd:
                    metadata_fd = os.open(".git", _DIR_FLAGS, dir_fd=root_fd)
                    try:
                        shallow_fd = os.open("shallow", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=metadata_fd)
                        with os.fdopen(shallow_fd, "wb") as stream:
                            stream.write(shallow)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.fsync(metadata_fd)
                    finally:
                        os.close(metadata_fd)
                git(destination, "bundle", "unbundle", str(bundle_path))
            git(destination, "checkout", "--detach", sha, "--")
            # No remote is needed for an offline edit workspace.
            if not shallow:
                git(destination, "remote", "remove", "origin")
        except Exception:
            self.discard(identifier)
            raise
        record["state"] = "ready"
        self._save_record(record)
        return self._public(record)

    def save_artifact(self, workspace: dict | str) -> dict:
        """Export a private replayable raw patch and integrity/touched-path metadata."""
        record = self._record(workspace)
        files = Files(record["path"])
        if git(files.root, "rev-parse", "HEAD", max_bytes=128).decode().strip() != record["base_sha"]:
            raise ValueError("workspace HEAD no longer matches its base")
        with files._locked() as fd:
            changes = files._changes(fd)
            if len(changes) > files.max_changed_files or sum(c["before_bytes"] + c["after_bytes"] for c in changes) > files.max_total_write_bytes:
                raise ValueError("workspace change budget exceeded")
            patch = files._patch(changes, redacted=False).encode("utf-8")
            payload = json.dumps({change["path"]: base64.b64encode(change["_after"]).decode("ascii") for change in changes}, sort_keys=True).encode()
        patch_path = self._write_private(record["id"] + ".patch", patch)
        self._write_private(record["id"] + ".payload.json", payload)
        artifact = {"workspace_id": record["id"], "base_sha": record["base_sha"], "sha256": digest(patch),
                    "payload_sha256": digest(payload),
                    "patch_path": str(patch_path), "changes": files._metadata(changes),
                    "created_at": datetime.now(UTC).isoformat()}
        metadata_path = self._write_private(record["id"] + ".artifact.json", json.dumps(artifact, sort_keys=True).encode())
        record["state"] = "exported"
        self._save_record(record)
        return {**artifact, "metadata_path": str(metadata_path)}

    def _artifact(self, workspace: dict | str) -> tuple[dict, bytes, dict[str, bytes]]:
        identifier = self._identifier(workspace)
        artifact = json.loads(self._private(identifier + ".artifact.json", 512_000))
        patch = self._private(identifier + ".patch")
        payload = self._private(identifier + ".payload.json")
        if artifact.get("workspace_id") != identifier or digest(patch) != artifact.get("sha256"):
            raise ValueError("artifact integrity check failed")
        if digest(payload) != artifact.get("payload_sha256"):
            raise ValueError("artifact payload integrity check failed")
        contents = {path: base64.b64decode(value, validate=True) for path, value in json.loads(payload).items()}
        if set(contents) != {change["path"] for change in artifact["changes"]}:
            raise ValueError("artifact path mismatch")
        for change in artifact["changes"]:
            safe_path(change["path"])
            if change["status"] != "deleted" and digest(contents[change["path"]]) != change["after_hash"]:
                raise ValueError("artifact content hash mismatch")
        return artifact, patch, contents

    @staticmethod
    def _check_target(target: Path, artifact: dict, *, fd: int | None = None) -> None:
        files = Files(target)
        # Check index too: a staged edit can have unchanged working-tree bytes.
        with files._locked() if fd is None else nullcontext(fd) as root_fd:
            if git(target, "rev-parse", "HEAD", max_bytes=128).decode().strip() != artifact["base_sha"]:
                raise ValueError("target HEAD differs from artifact base")
            for change in artifact["changes"]:
                path = change["path"]
                # Index-only diff never invokes source-configured clean filters.
                indexed = git(target, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", "--", ":(literal)" + path, max_bytes=8192)
                if indexed:
                    raise ValueError("target has changes in a touched path")
                if change["before_hash"] is None:
                    try:
                        with parent_fd(root_fd, path) as (parent, name):
                            os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise ValueError("target creation path already exists")
                elif digest(files._read(root_fd, path)) != change["before_hash"]:
                    raise ValueError("target content differs from artifact base in a touched path")
                elif files._mode(root_fd, path) != change["before_mode"]:
                    raise ValueError("target has mode changes in a touched path")
                # Validate existing ancestors even for a new nested file.
                parts = path.split("/")[:-1]
                parent = os.dup(root_fd)
                try:
                    for part in parts:
                        try:
                            child = os.open(part, _DIR_FLAGS, dir_fd=parent)
                        except FileNotFoundError:
                            break
                        os.close(parent)
                        parent = child
                finally:
                    os.close(parent)

    @staticmethod
    def _apply_patch(target: Path, artifact: dict, patch: bytes, contents: dict[str, bytes]) -> dict:
        """Transactional for cooperating callers; external editors must coordinate.

        All validation, staging, publication and rollback share the directory
        flock. CAS checks include bytes, mode and inode. No POSIX primitive can
        eliminate the final check/rename race with an uncooperative editor.
        Rollback never knowingly overwrites their changes; that case is reported
        explicitly as PartialApplyError with private recovery files retained.
        """
        files = Files(target)
        with files._locked() as fd:
            WorkspaceManager._check_target(target, artifact, fd=fd)
            if patch:
                git(target, "apply", "--cached", "--check", "--whitespace=nowarn", "-", input_data=patch)
            plans = []
            published = []
            created_dirs: list[tuple[int, str]] = []
            retained = set()

            def state(parent: int, name: str) -> tuple | None:
                try:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return None
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("target changed concurrently to an unsupported file")
                return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)

            def stage(parent: int, data: bytes, mode: int, prefix: str) -> str:
                name = prefix + uuid.uuid4().hex
                staged_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
                try:
                    with os.fdopen(staged_fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fchmod(stream.fileno(), mode)
                        os.fsync(stream.fileno())
                except BaseException:
                    os.unlink(name, dir_fd=parent)
                    raise
                return name

            def matches(plan: dict, expected: tuple | None, content: bytes | None) -> bool:
                try:
                    with parent_fd(fd, plan["path"]) as (current_parent, _):
                        current_info = os.fstat(current_parent)
                        held_info = os.fstat(plan["parent"])
                        if (current_info.st_dev, current_info.st_ino) != (held_info.st_dev, held_info.st_ino):
                            return False
                    return state(plan["parent"], plan["name"]) == expected and (
                        content is None or files._read(fd, plan["path"]) == content
                    )
                except (OSError, ValueError):
                    return False

            def intact(parent: int, name: str, expected: tuple, content: bytes) -> bool:
                try:
                    if state(parent, name) != expected:
                        return False
                    staged_fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
                    with os.fdopen(staged_fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        return (info.st_dev, info.st_ino) == expected[:2] and info.st_nlink == 1 and stream.read(len(content) + 1) == content
                except (OSError, ValueError):
                    return False

            with ExitStack() as stack:
                try:
                    # Stage BOTH new bytes and recovery bytes before any target
                    # is changed. Permission/space failures here are harmless.
                    for change in artifact["changes"]:
                        path = change["path"]
                        parent, name = stack.enter_context(parent_fd(fd, path, create=change["before_hash"] is None, created=created_dirs))
                        before_state = state(parent, name)
                        before = files._read(fd, path) if change["before_hash"] is not None else None
                        if before is None:
                            if before_state is not None:
                                raise ValueError("target creation path already exists")
                        elif digest(before) != change["before_hash"] or files._mode(fd, path) != change["before_mode"]:
                            raise ValueError("target changed concurrently")
                        plan = {"path": path, "parent": parent, "name": name, "before": before,
                                "before_state": before_state, "after": None if change["status"] == "deleted" else contents[path],
                                "backup": None, "backup_state": None, "staged": None, "published_state": None}
                        plans.append(plan)
                        if before is not None:
                            plan["backup"] = stage(parent, before, stat.S_IMODE(before_state[2]), ".file-agent-rollback-")
                            plan["backup_state"] = state(parent, plan["backup"])
                        if plan["after"] is not None:
                            mode = 0o755 if change["after_mode"] == "100755" else 0o644
                            plan["staged"] = stage(parent, plan["after"], mode, ".file-agent-stage-")
                            plan["published_state"] = state(parent, plan["staged"])
                    # Recheck HEAD, index, bytes and modes after staging, under
                    # the same lock as all cooperating writes.
                    WorkspaceManager._check_target(target, artifact, fd=fd)
                    for plan in plans:
                        if not matches(plan, plan["before_state"], plan["before"]):
                            raise ValueError("target changed concurrently")
                        parent, name = plan["parent"], plan["name"]
                        if plan["staged"] and not intact(parent, plan["staged"], plan["published_state"], plan["after"]):
                            raise ValueError("staged file changed concurrently")
                        if plan["after"] is None:
                            os.unlink(name, dir_fd=parent)
                        elif plan["before"] is None:
                            os.link(plan["staged"], name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                        else:
                            os.replace(plan["staged"], name, src_dir_fd=parent, dst_dir_fd=parent)
                        # Track publication before fsync or cleanup can fail.
                        published.append(plan)
                        if plan["before"] is None and plan["after"] is not None:
                            os.unlink(plan["staged"], dir_fd=parent)
                            plan["staged"] = None
                        os.fsync(parent)
                except BaseException as exc:
                    failed = []
                    for plan in reversed(published):
                        try:
                            if not matches(plan, plan["published_state"], plan["after"]):
                                raise ValueError("concurrent edit prevents rollback")
                            parent, name = plan["parent"], plan["name"]
                            if plan["backup"] and not intact(parent, plan["backup"], plan["backup_state"], plan["before"]):
                                raise ValueError("recovery file changed concurrently")
                            if plan["before"] is None:
                                os.unlink(name, dir_fd=parent)
                            elif plan["after"] is None:
                                # A concurrent creation cannot be overwritten.
                                os.link(plan["backup"], name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                            else:
                                os.replace(plan["backup"], name, src_dir_fd=parent, dst_dir_fd=parent)
                            os.fsync(parent)
                        except (OSError, ValueError):
                            failed.append(plan["path"])
                            if plan["backup"]:
                                retained.add(plan["backup"])
                                try:
                                    recovery_fd = os.open(plan["backup"], _FILE_FLAGS, dir_fd=plan["parent"])
                                    try:
                                        info = os.fstat(recovery_fd)
                                        if (info.st_dev, info.st_ino) == plan["backup_state"][:2] and info.st_nlink == 1:
                                            os.fchmod(recovery_fd, 0o600)
                                    finally:
                                        os.close(recovery_fd)
                                except OSError:
                                    pass
                    if failed:
                        raise PartialApplyError("Partial apply: safe rollback could not restore all touched paths. Inspect source and any retained .file-agent-rollback-* recovery files before retrying; conflicting edits were not deliberately overwritten.") from exc
                    raise
                finally:
                    for plan in plans:
                        for name in (plan["staged"], plan["backup"]):
                            if name and name not in retained:
                                try:
                                    os.unlink(name, dir_fd=plan["parent"])
                                except OSError:
                                    pass
                    for parent, name in reversed(created_dirs):
                        try:
                            os.rmdir(name, dir_fd=parent)
                        except OSError:
                            pass  # Successful files or a concurrent editor own it now.
                        finally:
                            os.close(parent)
        return {"applied": True, "changes": artifact["changes"], "base_sha": artifact["base_sha"]}

    def replay(self, workspace: dict | str, task_id: str | None = None) -> dict:
        """Recreate a fresh managed clone at the base, then check and replay export."""
        record = self._record(workspace, active=False)
        artifact, patch, contents = self._artifact(workspace)
        if artifact["base_sha"] != record["base_sha"]:
            raise ValueError("artifact base mismatch")
        created = self.create(task_id or "replay", artifact["base_sha"], snapshot_workspace=workspace)
        try:
            self._apply_patch(Path(created["path"]), artifact, patch, contents)
        except Exception:
            self.discard(created)
            raise
        return created

    def apply(self, workspace: dict | str, *, allow_source_write: bool = False) -> dict:
        """HOST ONLY: apply export to configured source with explicit opt-in.

        Never exposed as an MCP tool. Unrelated source edits are permitted; HEAD,
        index and bytes of every touched path must still match the saved base.
        """
        if allow_source_write is not True:
            raise PermissionError("source apply requires explicit host allow_source_write=True; export the patch instead")
        record = self._record(workspace, active=False)
        artifact, patch, contents = self._artifact(workspace)
        if artifact["base_sha"] != record["base_sha"]:
            raise ValueError("artifact base mismatch")
        # os.access alone reports writable for uid 0 on a read-only mount.
        with open_directory(self.source) as fd:
            info = os.fstat(fd)
            if not info.st_mode & 0o222 or os.statvfs(fd).f_flag & os.ST_RDONLY or not os.access(self.source, os.W_OK):
                raise PermissionError("source is read-only; use the exported patch on the host")
        try:
            result = self._apply_patch(self.source, artifact, patch, contents)
        except PartialApplyError:
            raise
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("source apply failed or source is read-only; exported patch is still available") from exc
        return result

    def discard(self, workspace: dict | str) -> dict:
        """Remove only an identity-checked owned directory; keep patch artifacts."""
        record = self._record(workspace)
        identifier = record["id"]
        # shutil's fd-based implementation does not follow symlinks encountered
        # inside a checkout. The target is a single generated child of storage.
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("safe descriptor-based deletion is unavailable")
        with open_directory(self.storage) as fd:
            info = os.stat(identifier, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != record["identity"]:
                raise ValueError("workspace ownership changed")
            shutil.rmtree(identifier, dir_fd=fd)
        record["state"] = "discarded"
        self._save_record(record)
        return self._public(record)
