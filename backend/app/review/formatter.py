from __future__ import annotations

from typing import ClassVar

from .schemas import FindingCategory, ReviewFinding, ReviewReport


class ReviewReportFormatter:
    LABELS: ClassVar[dict[FindingCategory, str]] = {
        FindingCategory.POTENTIAL_BUG: "Возможные ошибки",
        FindingCategory.ARCHITECTURE_ISSUE: "Архитектурные проблемы",
        FindingCategory.RECOMMENDATION: "Рекомендации",
    }
    COMPLETENESS: ClassVar[dict[str, str]] = {
        "complete": "полный анализ",
        "partial": "частичный анализ",
        "empty": "нет данных для анализа",
    }

    def markdown(self, report: ReviewReport) -> str:
        lines = [
            "# AI-ревью кода",
            "",
            f"**Репозиторий:** {report.repository}  ",
            f"**Пул-реквест:** #{report.pr_number}  ",
            f"**Модель:** {report.model}  ",
            f"**Базовый / текущий коммит:** `{report.base_sha[:12]}` / `{report.head_sha[:12]}`",
            "",
            "## Итог",
            "",
            report.summary,
        ]
        for category, label in self.LABELS.items():
            lines.extend(["", f"## {label}", ""])
            findings = [item for item in report.findings if item.category == category]
            if not findings:
                lines.append("Подтверждённых замечаний в этой категории нет.")
            for finding in findings:
                lines.extend(self._finding(finding))
        lines.extend(
            [
                "",
                "## Статистика ревью",
                "",
                f"- Проанализировано файлов: {report.files_analyzed}/{report.files_total}",
                f"- Замечаний: {len(report.findings)}",
                f"- Модель: `{report.model}`",
                f"- Токены: {report.total_tokens if report.total_tokens is not None else 'провайдер не сообщил'}",
                f"- Длительность: {report.elapsed_ms or 0} мс",
                f"- SHA коммита: `{report.head_sha}`",
                f"- Полнота: {self.COMPLETENESS.get(report.analysis_completeness, report.analysis_completeness)}",
            ]
        )
        if report.unreviewed_files:
            lines.extend(
                ["", "Файлы без анализа: " + ", ".join(f"`{path}`" for path in report.unreviewed_files)]
            )
        lines.extend(
            [
                "",
                "> AI-ревью носит рекомендательный характер и не заменяет тесты, анализ безопасности и проверку человеком.",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _finding(finding: ReviewFinding) -> list[str]:
        location = finding.filename or "Общий контекст"
        if finding.line_start:
            location += f":{finding.line_start}"
            if finding.line_end and finding.line_end != finding.line_start:
                location += f"-{finding.line_end}"
        if finding.permalink:
            location = f"[{location}]({finding.permalink})"
        return [
            f"### [{finding.severity.value}] {finding.title}",
            "",
            f"**Расположение:** {location}  ",
            f"**Уверенность:** {finding.confidence:.0%}",
            "",
            finding.description,
            "",
            f"**Последствия:** {finding.impact}",
            "",
            f"**Обоснование:** {finding.evidence}",
            "",
            f"**Предлагаемое исправление:** {finding.suggested_fix}",
            "",
        ]
