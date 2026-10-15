"""Configurable, non-executing architectural heuristics. Never report values."""

import re
from pathlib import PurePosixPath

from .files import Files, safe_path

RULES = {
    "controller-repository": "Controllers should call services, not repositories directly.",
    "hardcoded-credentials": "Do not hardcode credentials; use external configuration.",
    "migration-naming": "SQL migrations use V<version>__<description>.sql or R__<description>.sql.",
}
_CREDENTIAL = re.compile(
    r'''(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\b\s*[=:]\s*["'][^"']+["']'''
)


def check_rules(root: str, enabled: list[str], paths: list[str] | None = None) -> dict:
    files = Files(root)
    if paths is not None and (not isinstance(paths, list) or len(paths) > 100):
        raise ValueError("paths must contain at most 100 relative files")
    selected = paths if paths is not None else files.file_list(limit=2000)["files"]
    findings = []
    scanned = 0
    for path in selected:
        safe_path(path)
        with files._locked() as fd:
            try:
                data = files._read(fd, path)
            except (OSError, ValueError):
                continue
        scanned += len(data)
        if scanned > files.max_scan_bytes:
            raise ValueError("rule scan byte budget exceeded")
        text = data.decode("utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            codes = []
            if ("controller-repository" in enabled and path.lower().endswith(".java")
                    and "controller" in path.lower() and re.search(r"\b\w*(?:Repository|Repostirory)\b", line)):
                codes.append("controller-repository")
            if "hardcoded-credentials" in enabled and _CREDENTIAL.search(line):
                # Inspect original bytes but emit ONLY static messages and locations.
                codes.append("hardcoded-credentials")
            for code in codes:
                findings.append({"path": path, "line": number, "code": code, "message": RULES[code]})
            if len(findings) >= 500:
                return {"findings": findings, "heuristic": True, "truncated": True, "rules": enabled}
        name = PurePosixPath(path).name
        if ("migration-naming" in enabled and "migration" in path.lower() and name.endswith(".sql")
                and not re.fullmatch(r"(?:V[0-9][0-9_.]*|R)__[A-Za-z0-9_]+\.sql", name)):
            findings.append({"path": path, "line": 1, "code": "migration-naming", "message": RULES["migration-naming"]})
    return {"findings": findings, "heuristic": True, "truncated": False, "rules": enabled}
