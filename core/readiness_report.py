import json
from datetime import datetime, UTC
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "reports" / "readiness"


def ensure_reports_dir():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def safe_filename_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def create_readiness_report(
    project_dir,
    config: dict,
    checks: list[dict],
    summary: dict,
) -> dict:
    """
    Agent 5D — Readiness Report

    Creates a JSON readiness report after Agent 5 reliability checks.

    Report includes:
    - timestamp
    - project path
    - proxy config
    - readiness score
    - pass/fail status
    - passed checks
    - failed checks
    - detailed check results
    """

    ensure_reports_dir()

    project_dir = Path(project_dir)

    timestamp = datetime.now(UTC).isoformat()
    filename = f"readiness-{safe_filename_timestamp()}.json"
    report_path = REPORTS_DIR / filename

    report = {
        "timestamp": timestamp,
        "project_dir": str(project_dir),
        "proxy_port": config.get("port"),
        "routes": config.get("routes", []),
        "security": config.get("security", {}),
        "ready": summary.get("ready", False),
        "score": summary.get("score", 0),
        "passed_checks": summary.get("passed_checks", []),
        "failed_checks": summary.get("failed_checks", []),
        "checks": checks,
    }

    report_path.write_text(json.dumps(report, indent=2))

    markdown_path = report_path.with_suffix(".md")
    markdown_path.write_text(render_markdown_report(report))

    return {
        "passed": report["ready"],
        "summary": (
            f"Readiness report created with score {report['score']}/100"
        ),
        "report_path": str(report_path),
        "markdown_report_path": str(markdown_path),
        "details": report,
        "error": None if report["ready"] else "Readiness report contains failed checks",
    }


def render_markdown_report(report: dict) -> str:
    status = "READY ✅" if report.get("ready") else "NOT READY ❌"

    lines = [
        "# AI Pingora Infrastructure Generator — Readiness Report",
        "",
        f"**Status:** {status}",
        f"**Score:** {report.get('score', 0)}/100",
        f"**Timestamp:** {report.get('timestamp')}",
        f"**Project:** `{report.get('project_dir')}`",
        f"**Proxy Port:** `{report.get('proxy_port')}`",
        "",
        "## Routes",
        "",
    ]

    routes = report.get("routes", [])

    if routes:
        for route in routes:
            lines.append(
                f"- `{route.get('path')}` → `{route.get('upstream')}`"
            )
    else:
        lines.append("- No routes found.")

    lines.extend(
        [
            "",
            "## Security Policy",
            "",
            "```json",
            json.dumps(report.get("security", {}), indent=2),
            "```",
            "",
            "## Check Summary",
            "",
            "### Passed Checks",
            "",
        ]
    )

    passed_checks = report.get("passed_checks", [])

    if passed_checks:
        for check in passed_checks:
            lines.append(f"- ✅ {check}")
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "### Failed Checks",
            "",
        ]
    )

    failed_checks = report.get("failed_checks", [])

    if failed_checks:
        for check in failed_checks:
            lines.append(f"- ❌ {check}")
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## Detailed Results",
            "",
        ]
    )

    for check in report.get("checks", []):
        passed = "✅" if check.get("passed") else "❌"

        lines.extend(
            [
                f"### {passed} {check.get('name')}",
                "",
                f"**Summary:** {check.get('summary')}",
                "",
            ]
        )

        if check.get("error"):
            lines.extend(
                [
                    f"**Error:** {check.get('error')}",
                    "",
                ]
            )

        lines.extend(
            [
                "```json",
                json.dumps(check.get("details", {}), indent=2),
                "```",
                "",
            ]
        )

    return "\n".join(lines)