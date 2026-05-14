from __future__ import annotations

import re
import sys
from typing import Any

from dotenv import load_dotenv

from agents.control_plane_repair_agent import resolve_update_flow
from core.safety import (
    create_safety_backup,
    detect_destructive_intent,
    format_destructive_warning,
    normalize_safe_route_prompt,
    parse_update_cli_args,
    rewrite_confirmed_destructive_prompt,
)


BACKEND_UPDATE_PATTERN = re.compile(
    r"\b(add|remove|delete)\s+(backend|upstream)\b",
    flags=re.IGNORECASE,
)

LOAD_BALANCER_UPDATE_PATTERN = re.compile(
    r"\b(balance|balanced|load[- ]?balance|load[- ]?balanced)\b",
    flags=re.IGNORECASE,
)

ALGORITHM_UPDATE_PATTERN = re.compile(
    r"\bset\s+/[A-Za-z0-9_.\-/]+\s+"
    r"(algorithm|balancing|load[- ]?balancing)",
    flags=re.IGNORECASE,
)


def _should_preserve_prompt(prompt: str) -> bool:
    """
    Some update prompts must not be passed through normalize_safe_route_prompt().

    Example bug:
      "remove backend 127.0.0.1:9104 from /users"

    can be incorrectly normalized into:
      "remove /backend"

    So backend/upstream and load-balancer commands are passed through unchanged.
    """

    text = prompt.strip()

    if BACKEND_UPDATE_PATTERN.search(text):
        return True

    if LOAD_BALANCER_UPDATE_PATTERN.search(text):
        return True

    if ALGORITHM_UPDATE_PATTERN.search(text):
        return True

    if "host.docker.internal:" in text:
        return True

    return False


def _run_update_flow(prompt: str) -> dict[str, Any]:
    """
    Runs the update graph through the control-plane repair agent.

    The CLI does not hardcode run_update_flow or run_update_graph.
    The agent resolves the correct orchestration entrypoint.
    """

    update_flow = resolve_update_flow()
    result = update_flow(prompt)

    if result is None:
        return {}

    if isinstance(result, dict):
        return result

    return {"result": result}


def _get_summary(result: dict[str, Any]) -> list[str]:
    summary = (
        result.get("change_summary")
        or result.get("summary")
        or result.get("update_summary")
        or []
    )

    if isinstance(summary, str):
        return [summary]

    if isinstance(summary, list):
        return [str(item) for item in summary]

    return []


def _print_update_summary(result: dict[str, Any]) -> None:
    summary = _get_summary(result)

    print()
    print("📋 Update summary:")

    if not summary:
        print("ℹ️ No effective config changes detected.")
        return

    for item in summary:
        text = str(item).strip()

        if not text:
            continue

        lower = text.lower()

        if lower.startswith("removed route"):
            print(f"✅ {text}")
        elif lower.startswith("added route"):
            print(f"✅ {text}")
        elif lower.startswith("updated route"):
            print(f"✅ {text}")
        elif lower.startswith("removed backend"):
            print(f"✅ {text}")
        elif lower.startswith("added backend"):
            print(f"✅ {text}")
        elif lower.startswith("configured load balancer"):
            print(f"✅ {text}")
        elif lower.startswith("set ") and "algorithm" in lower:
            print(f"✅ {text}")
        elif lower.startswith("security changed"):
            print(f"✅ {text}")
        elif "already absent" in lower:
            print(f"ℹ️ {text}")
        elif "already exists" in lower:
            print(f"ℹ️ {text}")
        elif "already existed" in lower:
            print(f"ℹ️ {text}")
        elif "duplicate" in lower:
            print(f"ℹ️ {text}")
        elif "no effective" in lower:
            print(f"ℹ️ {text}")
        else:
            print(f"ℹ️ {text}")


def _print_deploy_status(result: dict[str, Any]) -> None:
    active_color = (
        result.get("active_color")
        or result.get("color")
        or result.get("active")
    )

    live_url = (
        result.get("live_url")
        or result.get("url")
        or "http://127.0.0.1:8088"
    )

    deployed = (
        result.get("deployed")
        or result.get("deployment_success")
        or result.get("success")
    )

    if deployed is False:
        print()
        print("⚠️ Update completed, but deployment may not have succeeded.")
        return

    if active_color:
        print()
        print("✅ Update deployed with blue/green switching")
        print(f"Active color: {active_color}")
        print(f"Live URL: {live_url}")


def main() -> int:
    load_dotenv()

    command = parse_update_cli_args(sys.argv[1:])

    if not command.prompt:
        print('Usage: python update.py "remove /analytics"')
        print('       python update.py "delete /analytics" --confirm')
        print()
        print("Examples:")
        print('  python update.py "add /inventory to backend 9001"')
        print('  python update.py "remove /admin"')
        print('  python update.py "add backend 9104 to /users"')
        print('  python update.py "remove backend 9104 from /users"')
        print('  python update.py "delete /analytics" --confirm')
        return 1

    destructive = detect_destructive_intent(command.prompt)

    if destructive.detected and not command.confirm:
        print(format_destructive_warning(command, destructive))
        return 2

    if destructive.detected and command.confirm:
        backup_path = create_safety_backup()
        print(f"✅ Safety backup created: {backup_path}")

        effective_prompt = rewrite_confirmed_destructive_prompt(
            command.prompt,
            destructive,
        )
    else:
        if _should_preserve_prompt(command.prompt):
            effective_prompt = command.prompt.strip()
        else:
            effective_prompt = normalize_safe_route_prompt(command.prompt)

    if command.dry_run:
        print("🧪 Dry run requested.")
        print(f"Original prompt: {command.prompt}")
        print(f"Effective prompt: {effective_prompt}")
        print("No changes were applied.")
        return 0

    result = _run_update_flow(effective_prompt)

    _print_deploy_status(result)
    _print_update_summary(result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
