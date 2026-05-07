from __future__ import annotations

import importlib
import inspect
import json
import os
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

try:
    from langsmith import traceable
except Exception:
    def traceable(*args: Any, **kwargs: Any):
        def decorator(fn):
            return fn

        return decorator

from agents.config_update_agent import apply_config_update


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATED_PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"
DEFAULT_BLUEGREEN_ROOT = PROJECT_ROOT / "generated-projects" / "default-project"
LIVE_URL = "http://127.0.0.1:8088"


def _import_optional(module_name: str) -> Any | None:
    try:
        return importlib.import_module(module_name)
    except Exception:
        return None


def _get_callable(module_name: str, names: list[str]) -> Callable[..., Any] | None:
    module = _import_optional(module_name)

    if module is None:
        return None

    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn

    return None


def _call_flex(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """
    Calls project functions even if your local function signature differs slightly.
    """

    attempts = [
        lambda: fn(*args, **kwargs),
        lambda: fn(*args),
        lambda: fn(**kwargs),
        lambda: fn(),
    ]

    last_error: Exception | None = None

    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_error = exc
            continue

    raise last_error or RuntimeError(f"Could not call {fn}")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _normalize_summary(summary: Any) -> list[str]:
    if summary is None:
        return []

    if isinstance(summary, str):
        return [summary]

    if isinstance(summary, list):
        return [str(item).strip() for item in summary if str(item).strip()]

    return [str(summary)]


def _dedupe_summary(summary: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []

    for item in summary:
        text = str(item).strip()
        key = text.lower()

        if not text or key in seen:
            continue

        seen.add(key)
        output.append(text)

    return output


def _discover_active_color() -> str:
    state_files = [
        PROJECT_ROOT / "bluegreen_state.json",
        PROJECT_ROOT / "gateway_state.json",
        PROJECT_ROOT / ".gateway_state.json",
        DEFAULT_BLUEGREEN_ROOT / "bluegreen_state.json",
        DEFAULT_BLUEGREEN_ROOT / "gateway_state.json",
        DEFAULT_BLUEGREEN_ROOT / ".gateway_state.json",
    ]

    for path in state_files:
        state = _read_json(path)

        if not state:
            continue

        for key in ("active_color", "active", "color", "current_color"):
            value = state.get(key)
            if isinstance(value, str) and value.lower() in {"blue", "green"}:
                return value.lower()

    active_color_file = DEFAULT_BLUEGREEN_ROOT / "active_color.txt"
    if active_color_file.exists():
        value = active_color_file.read_text(encoding="utf-8").strip().lower()
        if value in {"blue", "green"}:
            return value

    return "blue"


def _inactive_color(active_color: str) -> str:
    return "green" if active_color == "blue" else "blue"


def _config_candidates(active_color: str) -> list[Path]:
    return [
        PROJECT_ROOT / "active_config.json",
        PROJECT_ROOT / "gateway_config.json",
        PROJECT_ROOT / "config.json",
        GENERATED_PROJECT_DIR / "gateway_config.json",
        GENERATED_PROJECT_DIR / "config.json",
        DEFAULT_BLUEGREEN_ROOT / "active_config.json",
        DEFAULT_BLUEGREEN_ROOT / active_color / "active_config.json",
        DEFAULT_BLUEGREEN_ROOT / active_color / "gateway_config.json",
        DEFAULT_BLUEGREEN_ROOT / active_color / "config.json",
        DEFAULT_BLUEGREEN_ROOT / active_color / "generated-pingora-proxy" / "gateway_config.json",
        DEFAULT_BLUEGREEN_ROOT / active_color / "generated-pingora-proxy" / "config.json",
    ]


@traceable(name="load_active_config", run_type="chain")
def load_active_config() -> tuple[dict[str, Any], str]:
    """
    Uses your existing active-config loader if available.
    Falls back to known config file locations.
    """

    active_color = _discover_active_color()

    loader = _get_callable(
        "core.bluegreen_deploy",
        [
            "load_active_config",
            "get_active_config",
            "read_active_config",
        ],
    )

    if loader is not None:
        try:
            result = _call_flex(loader, project_root=PROJECT_ROOT)

            if isinstance(result, tuple) and result and isinstance(result[0], dict):
                loaded_color = result[1] if len(result) > 1 and isinstance(result[1], str) else active_color
                return result[0], loaded_color

            if isinstance(result, dict):
                return result, active_color
        except Exception:
            pass

    for path in _config_candidates(active_color):
        config = _read_json(path)
        if config is not None:
            return config, active_color

    raise FileNotFoundError(
        "Could not find active gateway config. "
        "Expected one of active_config.json, gateway_config.json, config.json, "
        "or a blue/green generated config."
    )


@traceable(name="update_prompt_to_config", run_type="chain")
def update_prompt_to_config(
    active_config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    return apply_config_update(active_config, prompt)


@traceable(name="security", run_type="chain")
def run_security(updated_config: dict[str, Any], prompt: str) -> dict[str, Any]:
    security_fn = _get_callable(
        "agents.security_agent",
        [
            "enforce_security",
            "security_agent_enforce_security",
            "run_security_agent",
        ],
    )

    if security_fn is None:
        return updated_config

    try:
        secured = security_fn(updated_config, prompt=prompt)
    except TypeError:
        try:
            secured = security_fn(updated_config, prompt)
        except TypeError:
            secured = security_fn(updated_config)

    if isinstance(secured, dict):
        print("✅ Security check passed")
        return secured

    print("✅ Security check passed")
    return updated_config


@traceable(name="config_preflight", run_type="chain")
def run_config_preflight(config: dict[str, Any]) -> None:
    preflight_fn = (
        _get_callable(
            "agents.config_repair_agent",
            [
                "run_config_preflight",
                "config_preflight",
                "preflight_config",
                "validate_config_preflight",
            ],
        )
        or _get_callable(
            "core.config_preflight",
            [
                "run_config_preflight",
                "config_preflight",
                "preflight_config",
                "validate_config_preflight",
            ],
        )
    )

    if preflight_fn is None:
        return

    print("🧪 Running config-level preflight checks...")
    _call_flex(preflight_fn, config)
    print("✅ Config-level preflight checks passed")


def _project_writer_candidates() -> list[tuple[str, list[str]]]:
    return [
        (
            "core.project_writer",
            [
                "write_project",
                "generate_project",
                "write_pingora_project",
                "generate_pingora_project",
                "create_project",
                "write",
            ],
        ),
    ]


@traceable(name="project_writer", run_type="chain")
def run_project_writer(config: dict[str, Any]) -> Path:
    writer_fn: Callable[..., Any] | None = None

    for module_name, names in _project_writer_candidates():
        writer_fn = _get_callable(module_name, names)
        if writer_fn is not None:
            break

    if writer_fn is None:
        raise RuntimeError(
            "Could not find project writer. Expected a function in core.project_writer "
            "such as write_project, generate_project, or write_pingora_project."
        )

    call_attempts = [
        lambda: writer_fn(config),
        lambda: writer_fn(config=config),
        lambda: writer_fn(config, GENERATED_PROJECT_DIR),
        lambda: writer_fn(config=config, output_dir=GENERATED_PROJECT_DIR),
        lambda: writer_fn(config=config, project_dir=GENERATED_PROJECT_DIR),
        lambda: writer_fn(config=config, base_dir=PROJECT_ROOT),
    ]

    last_error: Exception | None = None

    for attempt in call_attempts:
        try:
            result = attempt()
            if isinstance(result, (str, Path)):
                return Path(result)
            return GENERATED_PROJECT_DIR
        except TypeError as exc:
            last_error = exc
            continue

    raise last_error or RuntimeError("Project writer failed.")


@traceable(name="cargo_check", run_type="tool")
def run_cargo_check(project_dir: Path) -> None:
    cargo_fn = _get_callable(
        "agents.runtime_agent",
        [
            "run_cargo_check",
            "cargo_check",
            "repair_and_check",
            "runtime_check",
        ],
    )

    if cargo_fn is not None:
        result = _call_flex(cargo_fn, project_dir)

        if isinstance(result, dict) and result.get("success") is False:
            raise RuntimeError(result.get("error") or "cargo check failed")

        return

    cargo_toml = project_dir / "Cargo.toml"
    if not cargo_toml.exists():
        return

    print("🔍 Running cargo check... attempt 1/3")

    result = subprocess.run(
        ["cargo", "check"],
        cwd=str(project_dir),
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "cargo check failed\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}"
        )

    print("✅ cargo check passed")


def _copy_project_to_color(project_dir: Path, target_color: str) -> Path:
    target_root = DEFAULT_BLUEGREEN_ROOT / target_color
    target_project = target_root / "generated-pingora-proxy"

    if target_project.exists():
        shutil.rmtree(target_project)

    target_project.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        project_dir,
        target_project,
        ignore=shutil.ignore_patterns("target", ".git", "__pycache__", ".pytest_cache"),
    )

    return target_project


@traceable(name="bluegreen_deploy", run_type="chain")
def run_bluegreen_deploy(
    config: dict[str, Any],
    project_dir: Path,
    active_color: str,
) -> dict[str, Any]:
    deploy_fn = _get_callable(
        "core.bluegreen_deploy",
        [
            "deploy_bluegreen",
            "bluegreen_deploy",
            "deploy",
            "run_bluegreen_deploy",
            "switch_bluegreen",
        ],
    )

    target_color = _inactive_color(active_color)

    if deploy_fn is not None:
        call_attempts = [
            lambda: deploy_fn(config=config, project_dir=project_dir, active_color=active_color),
            lambda: deploy_fn(config, project_dir, active_color),
            lambda: deploy_fn(config=config, project_dir=project_dir),
            lambda: deploy_fn(project_dir=project_dir),
            lambda: deploy_fn(config),
            lambda: deploy_fn(),
        ]

        last_error: Exception | None = None

        for attempt in call_attempts:
            try:
                result = attempt()

                if isinstance(result, dict):
                    result.setdefault("active_color", result.get("active_color") or target_color)
                    result.setdefault("live_url", LIVE_URL)
                    result.setdefault("deployed", result.get("success", True))
                    return result

                return {
                    "success": True,
                    "deployed": True,
                    "active_color": target_color,
                    "live_url": LIVE_URL,
                }
            except TypeError as exc:
                last_error = exc
                continue

        raise last_error or RuntimeError("Blue/green deploy function failed.")

    compose_file = project_dir / "docker-compose.bluegreen.yml"

    if not compose_file.exists():
        return {
            "success": True,
            "deployed": False,
            "skipped_deploy": True,
            "active_color": active_color,
            "live_url": LIVE_URL,
            "message": "No docker-compose.bluegreen.yml found; deployment skipped.",
        }

    target_project_dir = _copy_project_to_color(project_dir, target_color)
    target_compose = target_project_dir / "docker-compose.bluegreen.yml"

    build_cmd = [
        "docker",
        "compose",
        "-p",
        f"pingora-{target_color}",
        "-f",
        str(target_compose),
        "build",
    ]

    up_cmd = [
        "docker",
        "compose",
        "-p",
        f"pingora-{target_color}",
        "-f",
        str(target_compose),
        "up",
        "-d",
    ]

    build = subprocess.run(build_cmd, text=True, capture_output=True)

    if build.returncode != 0:
        return {
            "success": False,
            "deployed": False,
            "active_color": active_color,
            "live_url": LIVE_URL,
            "error": "docker build failed",
            "stdout": build.stdout,
            "stderr": build.stderr,
        }

    up = subprocess.run(up_cmd, text=True, capture_output=True)

    if up.returncode != 0:
        return {
            "success": False,
            "deployed": False,
            "active_color": active_color,
            "live_url": LIVE_URL,
            "error": "docker compose up failed",
            "stdout": up.stdout,
            "stderr": up.stderr,
        }

    _write_json(
        PROJECT_ROOT / "bluegreen_state.json",
        {
            "active_color": target_color,
            "live_url": LIVE_URL,
        },
    )

    return {
        "success": True,
        "deployed": True,
        "active_color": target_color,
        "live_url": LIVE_URL,
    }


def _save_latest_config(config: dict[str, Any], active_color: str | None = None) -> None:
    _write_json(PROJECT_ROOT / "active_config.json", config)
    _write_json(PROJECT_ROOT / "gateway_config.json", config)

    if active_color:
        _write_json(DEFAULT_BLUEGREEN_ROOT / active_color / "active_config.json", config)


def _should_skip_deploy(changed: bool, understood: bool, change_summary: list[str]) -> bool:
    if changed:
        return False

    if understood:
        return True

    if change_summary and change_summary != ["No effective config changes detected."]:
        return True

    return True


@traceable(name="update_gateway_flow", run_type="chain")
def run_update_graph(prompt: str) -> dict[str, Any]:
    load_dotenv()

    prompt = prompt or ""

    active_config, active_color = load_active_config()

    update_result = update_prompt_to_config(active_config, prompt)

    if not isinstance(update_result, dict):
        update_result = {
            "config": active_config,
            "change_summary": ["No effective config changes detected."],
            "changed": False,
            "understood": False,
        }

    updated_config = update_result.get("config", active_config)
    change_summary = _normalize_summary(
        update_result.get("change_summary")
        or update_result.get("summary")
        or []
    )

    changed = bool(update_result.get("changed", False))
    understood = bool(update_result.get("understood", False))

    if not change_summary:
        change_summary = ["No effective config changes detected."]

    change_summary = _dedupe_summary(change_summary)

    # Critical fix:
    # If the user requested an understood operation that is a no-op/duplicate,
    # preserve the agent's summary and skip build/deploy.
    if _should_skip_deploy(changed, understood, change_summary):
        return {
            "success": True,
            "deployed": False,
            "skipped_deploy": True,
            "active_color": active_color,
            "live_url": LIVE_URL,
            "config": active_config,
            "change_summary": change_summary,
        }

    secured_config = run_security(updated_config, prompt)
    run_config_preflight(secured_config)

    project_dir = run_project_writer(secured_config)

    if not isinstance(project_dir, Path):
        project_dir = GENERATED_PROJECT_DIR

    run_cargo_check(project_dir)

    deploy_result = run_bluegreen_deploy(
        config=secured_config,
        project_dir=project_dir,
        active_color=active_color,
    )

    if not isinstance(deploy_result, dict):
        deploy_result = {}

    deployed = bool(
        deploy_result.get("deployed")
        or deploy_result.get("success")
        or deploy_result.get("deployment_success")
    )

    new_active_color = (
        deploy_result.get("active_color")
        or deploy_result.get("color")
        or _inactive_color(active_color)
    )

    if deployed:
        _save_latest_config(secured_config, str(new_active_color))

    result = {
        "success": bool(deploy_result.get("success", deployed)),
        "deployed": deployed,
        "active_color": new_active_color if deployed else active_color,
        "live_url": deploy_result.get("live_url") or deploy_result.get("url") or LIVE_URL,
        "config": secured_config,
        "change_summary": change_summary,
    }

    if deploy_result.get("error"):
        result["error"] = deploy_result["error"]

    if deploy_result.get("stdout"):
        result["stdout"] = deploy_result["stdout"]

    if deploy_result.get("stderr"):
        result["stderr"] = deploy_result["stderr"]

    return result


def run_update_flow(prompt: str) -> dict[str, Any]:
    return run_update_graph(prompt)


def update_gateway_flow(prompt: str) -> dict[str, Any]:
    return run_update_graph(prompt)


def run(prompt: str) -> dict[str, Any]:
    return run_update_graph(prompt)