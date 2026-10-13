from __future__ import annotations

from typing import ClassVar

from .schemas import FindingCategory, ReviewFinding, ReviewReport


class ReviewReportFormatter:
    LABELS: ClassVar[dict[FindingCategory, str]] = {
        FindingCategory.POTENTIAL_BUG: "Potential Bugs",
        FindingCategory.ARCHITECTURE_ISSUE: "Architecture Issues",
        FindingCategory.RECOMMENDATION: "Recommendations",
    }

    def markdown(self, report: ReviewReport) -> str:
        lines = [
            "# AI Code Review",
            "",
            f"**Repository:** {report.repository}  ",
            f"**Pull Request:** #{report.pr_number}  ",
            f"**Model:** {report.model}  ",
            f"**Base / Head:** `{report.base_sha[:12]}` / `{report.head_sha[:12]}`",
            "",
            "## Summary",
            "",
            report.summary,
        ]
        for category, label in self.LABELS.items():
            lines.extend(["", f"## {label}", ""])
            findings = [item for item in report.findings if item.category == category]
            if not findings:
                lines.append("No supported findings in this category.")
            for finding in findings:
                lines.extend(self._finding(finding))
        lines.extend(
            [
                "",
                "## Review Statistics",
                "",
                f"- Files analyzed: {report.files_analyzed}/{report.files_total}",
                f"- Findings: {len(report.findings)}",
                f"- Model: `{report.model}`",
                f"- Tokens: {report.total_tokens if report.total_tokens is not None else 'provider did not report'}",
                f"- Duration: {report.elapsed_ms or 0} ms",
                f"- Commit SHA: `{report.head_sha}`",
                f"- Completeness: {report.analysis_completeness}",
            ]
        )
        if report.unreviewed_files:
            lines.extend(["", "Unreviewed files: " + ", ".join(f"`{path}`" for path in report.unreviewed_files)])
        lines.extend(
            [
                "",
                "> AI review is advisory and does not replace tests, security analysis, or human review.",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _finding(finding: ReviewFinding) -> list[str]:
        location = finding.filename or "General context"
        if finding.line_start:
            location += f":{finding.line_start}"
            if finding.line_end and finding.line_end != finding.line_start:
                location += f"-{finding.line_end}"
        if finding.permalink:
            location = f"[{location}]({finding.permalink})"
        return [
            f"### [{finding.severity.value}] {finding.title}",
            "",
            f"**Location:** {location}  ",
            f"**Confidence:** {finding.confidence:.0%}",
            "",
            finding.description,
            "",
            f"**Impact:** {finding.impact}",
            "",
            f"**Evidence:** {finding.evidence}",
            "",
            f"**Suggested fix:** {finding.suggested_fix}",
            "",
        ]
