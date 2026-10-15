"""Workspace-only, non-executing Markdown and conservative Java checks.

These are diagnostics, not a compiler or a sandbox. No repository-supplied
scripts, Maven plugins, processors, network requests or commands are executed.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .files import Files, safe_path


def _heading_slug(value: str) -> str:
    return re.sub(r"[^\w\- ]", "", value.lower()).replace(" ", "-")


def validate_markdown(files: Files, path: str) -> dict:
    """Check ATX heading levels/duplicates and workspace-local relative links."""
    safe_path(path)
    diagnostics = []
    with files._locked() as fd:
        text = files._read(fd, path).decode("utf-8")
        lines = text.splitlines()
        slugs: set[str] = set()
        last_level = 0
        fenced = False
        visible = []
        for number, line in enumerate(lines, 1):
            if re.match(r"^\s{0,3}(?:`{3,}|~{3,})", line):
                fenced = not fenced
                continue
            if fenced:
                continue
            visible.append((number, line))
            heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if not heading:
                continue
            level = len(heading[1])
            slug = _heading_slug(heading[2])
            if level > last_level + 1:
                diagnostics.append({"line": number, "code": "heading-level", "message": "heading level skips a level"})
            if slug in slugs:
                diagnostics.append({"line": number, "code": "duplicate-heading", "message": "duplicate heading anchor"})
            slugs.add(slug)
            last_level = level
        if not slugs:
            diagnostics.append({"line": 1, "code": "missing-heading", "message": "no ATX Markdown heading found"})
        for number, line in visible:
            for match in re.finditer(r"!?\[[^\]]*\]\(\s*(<?[^\s)>]*>?)\s*(?:[\"'][^\"']*[\"'])?\)", line):
                value = match[1].strip("<>")
                parsed = urlsplit(value)
                if parsed.scheme in {"http", "https", "mailto"}:
                    continue  # Never fetch external links.
                if parsed.scheme or parsed.netloc:
                    diagnostics.append({"line": number, "code": "unsafe-link", "message": "unsupported link scheme or absolute link"})
                    continue
                target = unquote(parsed.path)
                if not target:
                    if parsed.fragment and unquote(parsed.fragment) not in slugs:
                        diagnostics.append({"line": number, "code": "missing-anchor", "message": "local heading anchor not found"})
                    continue
                # A link may use .. within the workspace, but cannot escape it.
                components = list(PurePosixPath(path).parent.parts)
                escaped = target.startswith(("/", "\\"))
                for part in target.split("/"):
                    if part == "..":
                        if not components:
                            escaped = True
                            break
                        components.pop()
                    elif part not in {"", "."}:
                        components.append(part)
                try:
                    if escaped:
                        raise ValueError("outside workspace")
                    relative = safe_path("/".join(components))
                    files._read(fd, relative)
                except (ValueError, OSError):
                    diagnostics.append({"line": number, "code": "invalid-local-link", "message": "link target is missing, excluded or outside the workspace"})
    return {"path": path, "kind": "markdown", "ok": not diagnostics, "diagnostics": diagnostics,
            "executed": False, "limitations": "basic ATX/inline-link checks; no external requests or full Markdown parsing"}


def validate_java(files: Files, path: str) -> dict:
    """Heuristic lexical delimiter check only; NEVER claim compilation success."""
    safe_path(path)
    with files._locked() as fd:
        text = files._read(fd, path).decode("utf-8")
    # Remove comments, text blocks, strings and chars without executing code.
    token_pattern = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
    stripped = token_pattern.sub(lambda match: "\n" * match[0].count("\n"), text)
    stack = []
    diagnostics = []
    pairs = {"}": "{", ")": "(", "]": "["}
    line = 1
    for char in stripped:
        if char == "\n":
            line += 1
        elif char in "{([":
            stack.append((char, line))
        elif char in pairs:
            if not stack or stack[-1][0] != pairs[char]:
                diagnostics.append({"line": line, "code": "delimiter", "message": "possibly unmatched closing delimiter"})
                break
            stack.pop()
    for _, number in stack[:100]:
        diagnostics.append({"line": number, "code": "delimiter", "message": "possibly unclosed delimiter"})
    if not re.search(r"\b(?:class|interface|enum|record|module)\b|@interface\b", stripped):
        diagnostics.append({"line": 1, "code": "declaration", "message": "no recognized Java declaration (heuristic only)"})
    return {"path": path, "kind": "java-heuristic", "ok": not diagnostics, "diagnostics": diagnostics,
            "compiled": False, "executed": False,
            "limitations": "lexical heuristic, not a Java parser/compiler; no type checking or build/plugin execution"}


def validate(files: Files, path: str, *, compile: bool = False) -> dict:
    """Non-executing checks. Compilation is intentionally not enabled/supported."""
    if compile:
        raise ValueError("compilation is disabled: repository build plugins are untrusted; use a separately sandboxed host validator")
    if path.lower().endswith((".md", ".markdown")):
        return validate_markdown(files, path)
    if path.lower().endswith(".java"):
        return validate_java(files, path)
    raise ValueError("validation supports Markdown and Java text only")
