"""Descriptor-confined UTF-8 operations; no shell, symlinks or linked-file writes.

The caller must supply a managed workspace, never the source checkout. Limits are
computed against HEAD on every write (including untracked files), not an in-memory
counter: restarting the MCP server cannot reset the workspace change budget.
"""

from __future__ import annotations

import difflib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.rag.scanner import EXCLUDED_PARTS, redact_secrets

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_EXCLUDED = EXCLUDED_PARTS | {".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache", ".next", ".nuxt", "coverage"}
_SENSITIVE = re.compile(r"(?:secret|credential|password|passwd|token|keystore|keyring|keychain|api[_-]?key|(?:^|[._-])(?:key|keys)(?:[._-]|$)|^id_(?:rsa|dsa|ecdsa|ed25519))", re.IGNORECASE)
_KEY_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".der"}
_QUOTED_SECRET = re.compile(
    r'''(?i)((?:["']?)(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|client[_-]?secret|password|passwd|secret(?:_key)?|private[_-]?key)(?:["']?)\s*[=:]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
)
_AUTHORIZATION = re.compile(r"(?i)(\b(?:authorization\s*[:=]\s*)?(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_FIELD = r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|client[_-]?secret|password|passwd|secret(?:_key)?|private[_-]?key)"
_XML_SECRET = re.compile(
    rf"(?is)(<(?P<tag>(?:[\w.-]+:)?{_SECRET_FIELD})\b[^>]*>)(.*?)(</(?P=tag)\s*>)"
)
_YAML_BLOCK_SECRET = re.compile(
    rf'''(?ix)^([ \t]*)(?P<sequence>-\s+)?["']?(?:[\w.-]+\.)?{_SECRET_FIELD}["']?\s*:\s*(?:[&!][^\s]+\s+)*[|>](?:[1-9][+-]?|[+-][1-9]?)?\s*(?:\#.*)?$'''
)
_YAML_SECRET_START = re.compile(
    rf'''(?ix)^([ \t]*)(?P<sequence>-\s+)?["']?(?:[\w.-]+\.)?{_SECRET_FIELD}["']?\s*:\s*.*$'''
)
_XML_CONFIG_TAG = re.compile(r"(?is)<(?:[\w.-]+:)?(?:property|entry)\b[^>]*>")
_XML_CONFIG_PAIR = re.compile(r"(?is)(<(?P<kind>(?:[\w.-]+:)?(?:property|entry))\b[^>]*>)(.*?)(</(?P=kind)\s*>)")


def sensitive_field(name: str) -> bool:
    return bool(re.search(rf"(?i)(?:^|[.\-_])(?:{_SECRET_FIELD}|credentials?)$", name))


def _xml_named_secret(tag: str) -> bool:
    return any(sensitive_field(match[2]) for match in re.finditer(r'''(?is)\b(?:name|key)\s*=\s*(["'])(.*?)\1''', tag))


def redact_text(text: str) -> str:
    # Protect quoted values containing spaces/escaped quotes BEFORE the shared
    # scanner's assignment matcher. Preserve line positions for file_read.
    text = _XML_SECRET.sub(
        lambda match: match[1] + "[REDACTED]" + "".join(re.findall(r"\r\n|\r|\n", match[3])) + match[4], text
    )
    text = _XML_CONFIG_PAIR.sub(
        lambda match: (match[1] + "[REDACTED]" + "".join(re.findall(r"\r\n|\r|\n", match[3])) + match[4])
        if _xml_named_secret(match[1]) else match[0], text
    )
    text = _XML_CONFIG_TAG.sub(
        lambda match: re.sub(r'''(?is)(\bvalue\s*=\s*)(["'])(.*?)\2''',
                             lambda value: value[1] + value[2] + "[REDACTED]"
                             + "".join(re.findall(r"\r\n|\r|\n", value[3])) + value[2], match[0])
        if _xml_named_secret(match[0]) else match[0], text
    )
    # Match complete quoted multiline values before the indentation pass can
    # replace their opening quote and leave an unindented continuation behind.
    text = _QUOTED_SECRET.sub(
        lambda match: match[1] + "[REDACTED]" + "".join(re.findall(r"\r\n|\r|\n", match[0][len(match[1]):])), text
    )
    lines = text.splitlines(keepends=True)
    block_indent = None
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        indent = len(body) - len(body.lstrip(" \t"))
        if block_indent is not None:
            if not body.strip() or indent > block_indent:
                lines[index] = body[:indent] + ("[REDACTED]" if body.strip() else "") + line[len(body):]
                continue
            if body.lstrip().startswith("#"):
                continue  # YAML permits unindented comments inside a block.
            block_indent = None
        match = _YAML_BLOCK_SECRET.match(body) or _YAML_SECRET_START.match(body)
        if match:
            block_indent = len(match[1]) + len(match["sequence"] or "")
    text = "".join(lines)
    return redact_secrets(_AUTHORIZATION.sub(lambda match: match[1] + "[REDACTED]", text))


def safe_path(value: str, *, directory: bool = False) -> str:
    """Reject, rather than normalize, dangerous or ambiguous relative paths."""
    if directory and value == ".":
        return "."
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("a bounded relative path is required")
    if value.startswith(("/", "~")) or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("only safe relative paths are allowed")
    parts = value.split("/")
    if len(parts) > 64:
        raise ValueError("workspace directory depth limit exceeded")
    for part in parts:
        lowered = part.lower()
        if (
            part in {"", ".", ".."}
            or ":" in part
            or len(part.encode("utf-8")) > 255
            or lowered in _EXCLUDED
            or lowered.startswith((".env", ".file-agent-"))
            or lowered in {"env", ".netrc", ".npmrc", ".pypirc", ".ssh", ".aws", ".azure", ".config"}
            or _SENSITIVE.search(lowered)
            or Path(lowered).suffix in _KEY_SUFFIXES
        ):
            raise ValueError("path is excluded or sensitive")
    return value


@contextmanager
def open_directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Open every absolute ancestor without following symlinks."""
    absolute = Path(os.path.abspath(path))
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            new_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def parent_fd(root_fd: int, path: str, *, create: bool = False,
              created: list[tuple[int, str]] | None = None) -> Iterator[tuple[int, str]]:
    fd = os.dup(root_fd)
    try:
        parts = safe_path(path).split("/")
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=fd)
                    if created is not None:
                        created.append((os.dup(fd), part))
                except FileExistsError:
                    pass
            new_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
        # Isolation below is also effective on Git versions predating this flag.
        "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1",
    })
    return environment


def _git_read(fd: int, name: str, maximum: int) -> bytes:
    file_fd = os.open(name, _FILE_FLAGS, dir_fd=fd)
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError("unsupported Git metadata or metadata size limit exceeded")
        with os.fdopen(file_fd, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Git metadata size limit exceeded")
        return data
    finally:
        os.close(file_fd)


_MAX_SHALLOW_BYTES = 1_000_000


def validate_shallow(data: bytes, oid_length: int) -> bytes:
    """Validate metadata as bounded full object IDs, never paths or config."""
    if len(data) > _MAX_SHALLOW_BYTES or oid_length not in {40, 64}:
        raise ValueError("Git shallow metadata size limit or object format is invalid")
    if data:
        lines = data.splitlines(keepends=True)
        if len(lines) > 10_000 or any(not re.fullmatch(rb"[0-9a-f]{" + str(oid_length).encode() + rb"}\n", line) for line in lines):
            raise ValueError("invalid Git shallow metadata")
    return data


@contextmanager
def _git_view(root_fd: int, environment: dict[str, str], snapshot_sha: str | None,
              snapshot_metadata: dict[str, bytes] | None = None) -> Iterator[tuple[list[str], tuple[int, ...]]]:
    """Read objects/refs/index without EVER loading the repository's config.

    A standalone .git directory is required. Gitfiles, alternates and promisor
    repositories fail closed. Valid shallow boundaries are copied as data only.
    Config is parsed as data by `git config --file --no-includes` OUTSIDE a repo.
    Only the whitelisted object format is carried into the disposable gitdir.
    """
    metadata = os.open(".git", _DIR_FLAGS, dir_fd=root_fd)
    try:
        objects = os.open("objects", _DIR_FLAGS, dir_fd=metadata)
        try:
            with tempfile.TemporaryDirectory(prefix="file-agent-git-") as temporary:
                view = Path(temporary)
                (view / "refs").mkdir()
                config = _git_read(metadata, "config", 128_000)
                config_file = view / "input-config"
                config_file.write_bytes(config)
                parsed = subprocess.run(
                    ["git", "config", "--file", str(config_file), "--no-includes", "--null", "--list"],
                    cwd=temporary, env=environment, capture_output=True, timeout=30, check=False,
                )
                if parsed.returncode or len(parsed.stdout) > 256_000:
                    raise ValueError("invalid Git configuration")
                values = [item.split(b"\n", 1) for item in parsed.stdout.split(b"\0") if item]
                object_format = "sha1"
                for key, *value in values:
                    setting = value[0].lower() if value else b"true"
                    if key == b"extensions.objectformat":
                        if setting not in {b"sha1", b"sha256"}:
                            raise ValueError("unsupported Git object format")
                        object_format = setting.decode("ascii")
                    if key == b"extensions.partialclone" or (key.startswith(b"remote.") and key.endswith(b".promisor") and setting not in {b"false", b"no", b"off", b"0"}):
                        raise ValueError("promisor repositories are not supported; provide a complete snapshot")
                try:
                    shallow = _git_read(metadata, "shallow", _MAX_SHALLOW_BYTES)
                except FileNotFoundError:
                    shallow = b""
                validate_shallow(shallow, 64 if object_format == "sha256" else 40)
                if shallow:
                    (view / "shallow").write_bytes(shallow)
                if snapshot_metadata is not None:
                    # Capture precisely the boundary used to generate the bundle,
                    # not a second, potentially changed read of source metadata.
                    snapshot_metadata["shallow"] = shallow
                # Do not consult alternate object directories or incomplete packs.
                try:
                    info_fd = os.open("info", _DIR_FLAGS, dir_fd=objects)
                except FileNotFoundError:
                    pass
                else:
                    try:
                        try:
                            os.stat("alternates", dir_fd=info_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise ValueError("object alternates are not supported")
                    finally:
                        os.close(info_fd)
                try:
                    pack_fd = os.open("pack", _DIR_FLAGS, dir_fd=objects)
                except FileNotFoundError:
                    pass
                else:
                    try:
                        if any(name.endswith(".promisor") for name in os.listdir(pack_fd)):
                            raise ValueError("promisor packs are not supported")
                    finally:
                        os.close(pack_fd)
                (view / "config").write_text(
                    "[core]\nrepositoryformatversion = " + ("1" if object_format == "sha256" else "0")
                    + "\nbare = false\n" + ("[extensions]\nobjectFormat = sha256\n" if object_format == "sha256" else "")
                )
                # Git requires an objects directory even with GIT_OBJECT_DIRECTORY.
                (view / "objects").mkdir()
                total = 0
                entries = 0

                def copy_refs(source: int, destination: Path, depth: int = 0) -> None:
                    nonlocal total, entries
                    if depth > 64:
                        raise ValueError("Git ref depth limit exceeded")
                    with os.scandir(source) as items:
                        for entry in items:
                            entries += 1
                            if entries > 100_000:
                                raise ValueError("Git ref count limit exceeded")
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                child = os.open(entry.name, _DIR_FLAGS, dir_fd=source)
                                try:
                                    target = destination / entry.name
                                    target.mkdir()
                                    copy_refs(child, target, depth + 1)
                                finally:
                                    os.close(child)
                            else:
                                data = _git_read(source, entry.name, 4096)
                                total += len(data)
                                if total > 8_000_000:
                                    raise ValueError("Git ref size limit exceeded")
                                (destination / entry.name).write_bytes(data)

                for name, maximum in (("HEAD", 4096), ("packed-refs", 8_000_000), ("index", 16_000_000)):
                    try:
                        data = _git_read(metadata, name, maximum)
                    except FileNotFoundError:
                        if name == "HEAD":
                            raise
                    else:
                        (view / name).write_bytes(data)
                refs = os.open("refs", _DIR_FLAGS, dir_fd=metadata)
                try:
                    copy_refs(refs, view / "refs")
                finally:
                    os.close(refs)
                if snapshot_sha is not None:
                    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", snapshot_sha):
                        raise ValueError("snapshot requires a full commit SHA")
                    (view / "refs" / "heads").mkdir(exist_ok=True)
                    (view / "refs" / "heads" / "file-agent-base").write_text(snapshot_sha + "\n")
                environment["GIT_OBJECT_DIRECTORY"] = f"/proc/self/fd/{objects}"
                yield [f"--git-dir={view}", f"--work-tree=/proc/self/fd/{root_fd}"], (objects,)
        finally:
            environment.pop("GIT_OBJECT_DIRECTORY", None)
            os.close(objects)
    finally:
        os.close(metadata)


def git(root: Path, *args: str, input_data: bytes | None = None, max_bytes: int = 8_000_000,
        snapshot_sha: str | None = None, snapshot_metadata: dict[str, bytes] | None = None) -> bytes:
    """Fixed argument-vector Git runner; source reads use a config-free view.

    Output is spooled rather than accumulated without bound in process memory.
    No caller-controlled command strings or shell expansion are accepted.
    """
    from contextlib import ExitStack

    environment = _git_environment()
    with open_directory(root) as fd, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as standard_input, ExitStack() as stack:
        isolated = args[0] in {"rev-parse", "ls-tree", "cat-file", "diff", "apply", "bundle"}
        view_args, extra_fds = stack.enter_context(_git_view(fd, environment, snapshot_sha, snapshot_metadata)) if isolated else ([], ())
        if input_data is not None:
            standard_input.write(input_data)
            standard_input.seek(0)
        process = subprocess.Popen(
            ["git", *view_args, "-c", f"safe.directory={root}", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "core.untrackedCache=false", "-c", "credential.helper=", "-c", "protocol.allow=never",
             "-c", "protocol.file.allow=always", "-c", "core.attributesFile=/dev/null", *args],
             cwd=f"/proc/self/fd/{fd}", pass_fds=(fd, *extra_fds), env=environment,
            stdin=standard_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        try:
            # Enforce the cap WHILE Git writes, including large private bundles.
            # Drain stderr without retaining repository-controlled error text.
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                selector.register(process.stderr, selectors.EVENT_READ)
                deadline = time.monotonic() + 30
                written = 0
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired("git", 30)
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fileobj.fileno(), 65_536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif key.fileobj is process.stdout:
                            written += len(chunk)
                            if written > max_bytes:
                                raise ValueError("Git output exceeds workspace limits")
                            output.write(chunk)
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except BaseException:
            # Git may have a pack-objects child holding the output pipes open.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        finally:
            process.stdout.close()
            process.stderr.close()
        if process.returncode:
            # Git errors can contain repository-controlled text or secrets.
            raise RuntimeError("Git operation failed (unsafe, conflicting or invalid repository state)")
        output.seek(0)
        data = output.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("Git output exceeds workspace limits")
        return data


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _FileSizeLimit(ValueError):
    pass


class Files:
    def __init__(
        self, root: str | os.PathLike[str], *, max_file_bytes: int = 1_000_000,
        max_files: int = 2_000, max_total_write_bytes: int = 5_000_000,
        max_operation_bytes: int = 1_000_000, max_scan_bytes: int = 20_000_000,
        max_changed_files: int = 100,
    ) -> None:
        self.root = Path(os.path.abspath(root))
        with open_directory(self.root):
            pass
        for value in (max_file_bytes, max_files, max_total_write_bytes, max_operation_bytes, max_scan_bytes, max_changed_files):
            if type(value) is not int or value < 1:
                raise ValueError("limits must be positive integers")
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.max_total_write_bytes = max_total_write_bytes
        self.max_operation_bytes = max_operation_bytes
        self.max_scan_bytes = max_scan_bytes
        self.max_changed_files = max_changed_files

    @contextmanager
    def _locked(self) -> Iterator[int]:
        # Directory-inode locks work across separate stdio MCP invocations.
        with open_directory(self.root) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield fd
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    @staticmethod
    def _limit(value: int, maximum: int, name: str) -> None:
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}")

    def _read(self, root_fd: int, path: str, *, text: bool = True) -> bytes:
        with parent_fd(root_fd, path) as (fd, name):
            file_fd = os.open(name, _FILE_FLAGS, dir_fd=fd)
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("only regular, non-hardlinked files are allowed")
                if info.st_size > self.max_file_bytes:
                    raise _FileSizeLimit("file exceeds size limit")
                chunks = []
                remaining = self.max_file_bytes + 1
                while remaining:
                    chunk = os.read(file_fd, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                if text:
                    self._text(data)
                if len(data) > self.max_file_bytes:
                    raise _FileSizeLimit("file exceeds size limit")
                return data
            finally:
                os.close(file_fd)

    @staticmethod
    def _text(data: bytes) -> str:
        if b"\x00" in data:
            raise ValueError("binary files are not supported")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("only UTF-8 text files are supported") from None

    def _paths(self, root_fd: int) -> list[str]:
        paths: list[str] = []
        visited = 0

        def walk(fd: int, prefix: str, depth: int) -> None:
            nonlocal visited
            if depth > 64:
                raise ValueError("workspace directory depth limit exceeded")
            # scandir(fd) never resolves a repository-controlled parent path.
            with os.scandir(fd) as entries:
                for entry in entries:
                    visited += 1
                    if visited > self.max_files * 10:
                        raise ValueError("workspace entry limit exceeded")
                    path = f"{prefix}/{entry.name}" if prefix else entry.name
                    try:
                        safe_path(path)
                    except ValueError:
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = os.open(entry.name, _DIR_FLAGS, dir_fd=fd)
                        try:
                            walk(child_fd, path, depth + 1)
                        finally:
                            os.close(child_fd)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        paths.append(path)
                        if len(paths) > self.max_files:
                            raise ValueError("workspace file limit exceeded")

        walk(root_fd, "", 0)
        return sorted(paths)

    @staticmethod
    def _scope(fd: int, path: str) -> None:
        if path == ".":
            return
        try:
            with parent_fd(fd, path) as (parent, name):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return  # A nonexistent prefix is an empty list/search scope.
        if stat.S_ISDIR(info.st_mode):
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("scope must not be a symlink, hardlink or special file")

    def _base(self) -> dict[str, tuple[str, str]]:
        data = git(self.root, "ls-tree", "-rz", "--full-tree", "HEAD")
        result = {}
        for item in data.split(b"\0"):
            if not item:
                continue
            header, name = item.split(b"\t", 1)
            mode, kind, oid = header.decode("ascii").split()
            try:
                path = safe_path(name.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if kind == "blob" and mode in {"100644", "100755"}:
                result[path] = (mode, oid)
                if len(result) > self.max_files:
                    raise ValueError("workspace tracked-file limit exceeded")
        return result

    def _blob(self, oid: str) -> bytes:
        # Object IDs come only from ls-tree, never from model input.
        size = int(git(self.root, "cat-file", "-s", oid, max_bytes=32))
        if size > self.max_file_bytes:
            raise ValueError("base file exceeds size limit")
        data = git(self.root, "cat-file", "blob", oid, max_bytes=self.max_file_bytes)
        return data

    @staticmethod
    def _mode(fd: int, path: str) -> str:
        with parent_fd(fd, path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("only regular, non-hardlinked files are allowed")
            return "100755" if info.st_mode & 0o111 else "100644"

    def _unchanged_large(self, fd: int, path: str, mode: str, oid: str, remaining: int) -> int:
        """Untouched large assets must not block small text edits elsewhere.

        They are never returned as content or admitted into a patch. Hash their
        original bytes within the aggregate scan budget and require exact HEAD
        content AND mode; oversized changed/untracked files still fail closed.
        """
        with parent_fd(fd, path) as (parent, name):
            file_fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
            with os.fdopen(file_fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("only regular, non-hardlinked files are allowed")
                if info.st_size > remaining:
                    raise ValueError("workspace scan byte limit exceeded")
                blob_hash = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
                hashed = blob_hash(b"blob " + str(info.st_size).encode("ascii") + b"\0")
                scanned = 0
                while chunk := stream.read(min(65_536, remaining - scanned + 1)):
                    scanned += len(chunk)
                    if scanned > remaining:
                        raise ValueError("workspace scan byte limit exceeded")
                    hashed.update(chunk)
                after = os.fstat(stream.fileno())
                actual_mode = "100755" if info.st_mode & 0o111 else "100644"
                if (scanned == info.st_size == after.st_size and info.st_mtime_ns == after.st_mtime_ns
                        and info.st_mode == after.st_mode and after.st_nlink == 1
                        and actual_mode == mode and hashed.hexdigest() == oid):
                    return scanned
        raise _FileSizeLimit("file exceeds size limit")

    def _changes(self, fd: int, proposed: tuple[str, bytes] | None = None) -> list[dict]:
        baseline = self._base()
        current = set(self._paths(fd))
        if proposed:
            current.add(proposed[0])
        if len(current) > self.max_files:
            raise ValueError("workspace file limit exceeded")
        changes = []
        scanned = 0
        for path in sorted(set(baseline) | current):
            if proposed and path == proposed[0]:
                after = proposed[1]
                try:
                    after_mode = self._mode(fd, path)
                except FileNotFoundError:
                    after_mode = "100644"
            elif path in current:
                try:
                    after = self._read(fd, path, text=False)
                except _FileSizeLimit:
                    if path not in baseline:
                        raise
                    scanned += self._unchanged_large(fd, path, *baseline[path], self.max_scan_bytes - scanned)
                    continue
                after_mode = self._mode(fd, path)
            else:
                # A missing tracked file is a deletion; a symlink/hardlink is
                # not a deletion and must fail closed instead of being hidden.
                try:
                    with parent_fd(fd, path) as (parent, name):
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError("unsupported tracked file type")
                after = b""
                after_mode = None
            scanned += len(after)
            if scanned > self.max_scan_bytes:
                raise ValueError("workspace scan byte limit exceeded")
            if path in baseline and path in current:
                oid = baseline[path][1]
                blob_hash = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
                if blob_hash(b"blob " + str(len(after)).encode("ascii") + b"\0" + after).hexdigest() == oid and baseline[path][0] == after_mode:
                    continue
            before = self._blob(baseline[path][1]) if path in baseline else b""
            scanned += len(before)
            if scanned > self.max_scan_bytes:
                raise ValueError("workspace scan byte limit exceeded")
            if before == after and path in baseline and path in current and baseline[path][0] == after_mode:
                continue
            self._text(before)
            self._text(after)
            old_lines, new_lines = before.decode("utf-8").splitlines(keepends=True), after.decode("utf-8").splitlines(keepends=True)
            additions = deletions = 0
            for tag, a, b, c, d in difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes():
                if tag in {"replace", "delete"}:
                    deletions += b - a
                if tag in {"replace", "insert"}:
                    additions += d - c
            changes.append({
                "path": path, "status": "added" if path not in baseline else "deleted" if path not in current else "modified",
                "additions": additions, "deletions": deletions,
                "before_bytes": len(before), "after_bytes": len(after),
                "before_hash": digest(before) if path in baseline else None,
                "after_hash": digest(after) if path in current else None,
                "before_mode": baseline[path][0] if path in baseline else None, "after_mode": after_mode,
                "_before": before, "_after": after,
            })
        return changes

    @staticmethod
    def _metadata(changes: list[dict]) -> list[dict]:
        return [{key: value for key, value in change.items() if not key.startswith("_")} for change in changes]

    def _budget(self, fd: int, path: str, data: bytes) -> None:
        if len(data) > min(self.max_file_bytes, self.max_operation_bytes):
            raise ValueError("write exceeds per-operation size limit")
        self._text(data)
        changes = self._changes(fd, (path, data))
        # Both removed and replacement content count. Deletions cannot be used
        # as negative credit, and new/untracked files cannot escape the budget.
        total = sum(change["before_bytes"] + change["after_bytes"] for change in changes)
        if total > self.max_total_write_bytes or len(changes) > self.max_changed_files:
            raise ValueError("workspace total change budget exceeded")

    def _atomic_write(self, fd: int, path: str, data: bytes, *, existing: bytes | None, new_mode: int | None = None) -> None:
        with parent_fd(fd, path, create=existing is None) as (parent, name):
            mode = 0o644
            if existing is not None:
                if self._read(fd, path) != existing:
                    raise ValueError("file changed concurrently")
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("refusing linked-file write")
                mode = stat.S_IMODE(info.st_mode) & 0o777
            if new_mode is not None:
                mode = new_mode
            temporary = f".file-agent-{uuid.uuid4().hex}"
            temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            try:
                with os.fdopen(temp_fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
                if existing is None:
                    # link+unlink provides atomic no-clobber publication.
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                else:
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def file_list(self, path: str = ".", limit: int = 500, pattern: str = "*", exclude: list[str] | None = None) -> dict:
        """List permitted regular files (tracked and untracked), never symlinks."""
        safe_path(path, directory=True)
        self._limit(limit, self.max_files, "limit")
        if not isinstance(pattern, str) or len(pattern) > 256 or not pattern:
            raise ValueError("pattern must be a bounded glob")
        if exclude is not None and (not isinstance(exclude, list) or len(exclude) > 30 or any(not isinstance(p, str) or len(p) > 256 for p in exclude)):
            raise ValueError("exclude must contain bounded glob patterns")
        with self._locked() as fd:
            self._scope(fd, path)
            files = [item for item in self._paths(fd)
                     if (path == "." or item == path or item.startswith(path + "/"))
                     and fnmatch.fnmatchcase(item, pattern)
                     and not any(fnmatch.fnmatchcase(item, rule) for rule in (exclude or []))]
        return {"files": files[:limit], "count": min(len(files), limit), "truncated": len(files) > limit}

    def file_read(self, path: str, start_line: int = 1, end_line: int | None = None, max_lines: int = 500) -> dict:
        """Read numbered, redacted lines; sha256 always hashes ORIGINAL bytes."""
        safe_path(path)
        self._limit(start_line, 10_000_000, "start_line")
        self._limit(max_lines, 2_000, "max_lines")
        if end_line is not None:
            self._limit(end_line, 10_000_000, "end_line")
            if end_line < start_line:
                raise ValueError("end_line precedes start_line")
        with self._locked() as fd:
            data = self._read(fd, path)
        lines = redact_text(data.decode("utf-8")).splitlines()
        stop = min(end_line or len(lines), start_line + max_lines - 1)
        selected = lines[start_line - 1:stop]
        return {"path": path, "sha256": digest(data), "size_bytes": len(data), "total_lines": len(lines),
                "type": "text/markdown" if path.lower().endswith((".md", ".markdown")) else "text/x-java-source" if path.lower().endswith(".java") else "text/plain",
                "start_line": start_line, "end_line": start_line + len(selected) - 1,
                "content": "\n".join(f"{number}: {line}" for number, line in enumerate(selected, start_line)),
                "truncated": stop < len(lines), "redacted": True}

    def file_search(self, query: str, path: str = ".", limit: int = 100, case_sensitive: bool = False) -> dict:
        """Bounded literal (not regex) search over redacted UTF-8 text."""
        safe_path(path, directory=True)
        self._limit(limit, 1_000, "limit")
        if not isinstance(query, str) or not query or len(query) > 1024 or "\n" in query or "\r" in query:
            raise ValueError("query must be a nonempty, single-line literal of at most 1024 characters")
        needle = query if case_sensitive else query.casefold()
        matches = []
        scanned = 0
        with self._locked() as fd:
            self._scope(fd, path)
            for item in self._paths(fd):
                if path != "." and item != path and not item.startswith(path + "/"):
                    continue
                try:
                    data = self._read(fd, item)
                except ValueError:
                    continue  # binary/oversized files are not text search inputs
                scanned += len(data)
                if scanned > self.max_scan_bytes:
                    return {"matches": matches, "truncated": True}
                for number, line in enumerate(redact_text(data.decode("utf-8")).splitlines(), 1):
                    if needle in (line if case_sensitive else line.casefold()):
                        if len(matches) == limit:
                            return {"matches": matches, "truncated": True}
                        matches.append({"path": item, "line": number, "text": line[:2000]})
        return {"matches": matches, "truncated": False}

    def file_create(self, path: str, content: str) -> dict:
        """Atomically create a new UTF-8 file; never overwrite existing paths."""
        safe_path(path)
        if not isinstance(content, str):
            raise TypeError("content must be text")
        data = content.encode("utf-8")
        with self._locked() as fd:
            # Check existence before quota evaluation; also rejects symlinks.
            try:
                with parent_fd(fd, path) as (parent, name):
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError("file already exists")
            self._budget(fd, path, data)
            self._atomic_write(fd, path, data, existing=None)
        return {"path": path, "sha256": digest(data), "size_bytes": len(data), "created": True}

    def file_apply_patch(self, path: str, expected_hash: str, old_text: str, new_text: str) -> dict:
        """Exact unique text replacement, NOT unified-diff or arbitrary patch execution.

        expected_hash is SHA-256 of the complete original file. old_text must be
        nonempty and occur exactly once, including no overlapping occurrences.
        """
        safe_path(path)
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("expected_hash must be an original SHA-256")
        if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
            raise ValueError("a nonempty old_text and text new_text are required")
        if len(old_text.encode("utf-8")) + len(new_text.encode("utf-8")) > self.max_operation_bytes:
            raise ValueError("replacement exceeds per-operation size limit")
        with self._locked() as fd:
            before = self._read(fd, path)
            if digest(before) != expected_hash:
                raise ValueError("stale content hash; read the file again")
            text = before.decode("utf-8")
            index = text.find(old_text)
            if index < 0 or text.find(old_text, index + 1) >= 0:
                raise ValueError("old_text must occur exactly once")
            data = (text[:index] + new_text + text[index + len(old_text):]).encode("utf-8")
            self._budget(fd, path, data)
            self._atomic_write(fd, path, data, existing=before)
        return {"path": path, "sha256": digest(data), "previous_hash": expected_hash, "size_bytes": len(data), "replacements": 1}

    def file_status(self) -> dict:
        """Changes against HEAD, including untracked files; excludes secret paths."""
        with self._locked() as fd:
            changes = self._changes(fd)
        return {"changes": self._metadata(changes), "count": len(changes),
                "commit": git(self.root, "rev-parse", "HEAD", max_bytes=128).decode().strip(),
                "workspace_id": self.root.name, "branch": "detached workspace",
                "total_change_bytes": sum(c["before_bytes"] + c["after_bytes"] for c in changes),
                "max_total_write_bytes": self.max_total_write_bytes}

    @staticmethod
    def _patch(changes: list[dict], *, redacted: bool) -> str:
        pieces = []
        for change in changes:
            path = change["path"]
            before, after = change["_before"].decode("utf-8"), change["_after"].decode("utf-8")
            if redacted:
                # Redact before diffing so multiline keys cannot leak fragments.
                before, after = redact_text(before), redact_text(after)
            # Git accepts C-quoted UTF-8 names; quote even ordinary names so
            # spaces, double quotes and patch-like names cannot confuse replay.
            old_path = json.dumps("a/" + path, ensure_ascii=False)
            new_path = json.dumps("b/" + path, ensure_ascii=False)
            old_name = "/dev/null" if change["status"] == "added" else old_path
            new_name = "/dev/null" if change["status"] == "deleted" else new_path
            pieces.append(f"diff --git {old_path} {new_path}\n")
            if change["status"] == "added":
                pieces.append(f"new file mode {change['after_mode']}\n")
            elif change["status"] == "deleted":
                pieces.append(f"deleted file mode {change['before_mode']}\n")
            elif change["before_mode"] != change["after_mode"]:
                pieces.append(f"old mode {change['before_mode']}\nnew mode {change['after_mode']}\n")
            for line in difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile=old_name, tofile=new_name):
                pieces.append(line)
                if not line.endswith("\n"):
                    pieces.append("\n\\ No newline at end of file\n")
        return "".join(pieces)

    def file_diff(self, path: str | None = None, max_bytes: int = 100_000) -> dict:
        """Bounded redacted unified diff, including new files, plus line counts."""
        if path is not None:
            safe_path(path)
        self._limit(max_bytes, 1_000_000, "max_bytes")
        with self._locked() as fd:
            if path is not None:
                self._scope(fd, path)
            changes = self._changes(fd)
        if path is not None:
            changes = [change for change in changes if change["path"] == path]
        data = self._patch(changes, redacted=True).encode("utf-8")
        text = data[:max_bytes].decode("utf-8", errors="ignore")
        return {"diff": text, "bytes": len(text.encode("utf-8")), "truncated": len(data) > max_bytes,
                "changes": self._metadata(changes), "redacted": True}

    def file_validate(self, path: str) -> dict:
        """Non-executing Markdown checks or Java lexical heuristic."""
        from .validation import validate

        return validate(self, path)
