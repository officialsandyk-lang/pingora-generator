from pathlib import Path
from typing import Any

from langsmith import traceable


def normalize_check_result(
    name: str,
    result: dict | None = None,
    error: Exception | None = None,
) -> dict:
    if error is not None:
        return {
            "name": name,
            "passed": False,
            "summary": f"{name} failed",
            "details": {},
            "error": str(error),
        }

    if result is None:
        return {
            "name": name,
            "passed": False,
            "summary": f"{name} returned no result",
            "details": {},
            "error": "No result returned",
        }

    return {
        "name": name,
        "passed": bool(result.get("passed", False)),
        "summary": result.get("summary", name),
        "details": result.get("details", {}),
        "error": result.get("error"),
    }


@traceable(name="reliability_agent_run_check", run_type="tool")
def run_check(name: str, check_fn, *args, **kwargs) -> dict:
    print(f"🔎 Agent 5: running {name}...")

    try:
        result = check_fn(*args, **kwargs)
        normalized = normalize_check_result(name, result=result)

        if normalized["passed"]:
            print(f"✅ {name} passed")
        else:
            print(f"❌ {name} failed")
            if normalized.get("error"):
                print(normalized["error"])

        return normalized

    except Exception as e:
        normalized = normalize_check_result(name, error=e)
        print(f"❌ {name} failed")
        print(e)
        return normalized


def calculate_score(checks: list[dict]) -> int:
    if not checks:
        return 0

    passed = sum(1 for check in checks if check.get("passed"))
    total = len(checks)

    return int((passed / total) * 100)


def build_agent_summary(checks: list[dict]) -> dict:
    score = calculate_score(checks)
    failed_checks = [check for check in checks if not check.get("passed")]

    return {
        "ready": len(failed_checks) == 0,
        "score": score,
        "passed_checks": [check["name"] for check in checks if check.get("passed")],
        "failed_checks": [check["name"] for check in failed_checks],
        "checks": checks,
    }


@traceable(name="reliability_agent", run_type="chain")
def run_reliability_agent(
    project_dir,
    config: dict[str, Any],
    require_200: bool = True,
    latency_threshold_ms: int = 500,
    enforce: bool = True,
) -> dict:
    """
    Agent 5 — Protection / Reliability Agent

    Runs after pre-deploy sandbox verification.

    Checks:
    - security protection behavior
    - route performance / latency
    - Docker Compose resource limits
    - final readiness report

    If enforce=True, raises an error if the generated infrastructure is not ready.
    """

    print("")
    print("🛡️ Running Agent 5 — Protection / Reliability checks...")

    project_dir = Path(project_dir)

    from core.protection_tests import run_protection_tests
    from core.performance_check import run_performance_checks
    from core.resource_limits import verify_resource_limits
    from core.readiness_report import create_readiness_report

    checks = []

    checks.append(
        run_check(
            "Security protection tests",
            run_protection_tests,
            config,
            require_200=require_200,
        )
    )

    checks.append(
        run_check(
            "Performance checks",
            run_performance_checks,
            config,
            latency_threshold_ms=latency_threshold_ms,
        )
    )

    checks.append(
        run_check(
            "Resource limit verification",
            verify_resource_limits,
            project_dir,
            config,
        )
    )

    summary = build_agent_summary(checks)

    report = create_readiness_report(
        project_dir=project_dir,
        config=config,
        checks=checks,
        summary=summary,
    )

    print("")
    print("📊 Agent 5 Readiness Summary")
    print(f"Score: {summary['score']}/100")
    print(f"Ready: {'YES' if summary['ready'] else 'NO'}")

    if report.get("report_path"):
        print(f"Report: {report['report_path']}")

    if enforce and not summary["ready"]:
        failed = ", ".join(summary["failed_checks"])
        raise RuntimeError(
            "Agent 5 reliability checks failed. "
            f"Failed checks: {failed}"
        )

    print("✅ Agent 5 reliability checks completed")

    return {
        "ready": summary["ready"],
        "score": summary["score"],
        "summary": summary,
        "report": report,
    }