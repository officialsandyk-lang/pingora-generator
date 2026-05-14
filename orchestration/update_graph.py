from __future__ import annotations

import importlib
import json
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
from core.reliability import ReliabilityBrain

try:
    from core.preflight import ConfigPreflightError, PreflightError
except Exception:
    class PreflightError(RuntimeError):
        pass

    class ConfigPreflightError(PreflightError):
        pass


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
    except Exception:
        return None

    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _normalize_summary(summary: Any) -> list[str]:
    if summary is None:
        return []

    if isinstance(summary, str):
        text = summary.strip()
        return [text] if text else []

    if isinstance(summary, list):
        return [str(item).strip() for item in summary if str(item).strip()]

    text = str(summary).strip()
    return [text] if text else []


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


def _stage_for_exception(stage: str, exc: BaseException) -> str:
    text = str(exc).lower()

    if isinstance(exc, ConfigPreflightError):
        return "config_preflight"

    if isinstance(exc, PreflightError):
        return "environment_preflight"

    if stage:
        return stage

    if "config-level preflight" in text or "proxy/backend port conflict" in text:
        return "config_preflight"

    if "preflight checks failed" in text:
        return "environment_preflight"

    if "cargo check" in text or "could not compile" in text:
        return "cargo_check"

    if "docker" in text or "compose" in text or "registry" in text:
        return "bluegreen_deploy"

    if "health check" in text or "readiness" in text:
        return "bluegreen_deploy"

    if "httppeer" in text or "host:port" in text:
        return "runtime"

    return "unknown"


def _discover_active_color() -> str:
    get_active_color = _get_callable(
        "core.deployment_state",
        ["get_active_color"],
    )

    if get_active_color is not None:
        try:
            color = _call_flex(get_active_color)

            if isinstance(color, str) and color.lower() in {"blue", "green"}:
                return color.lower()
        except Exception:
            pass

    state_files = [
        DEFAULT_BLUEGREEN_ROOT / "deployment_state.json",
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
        DEFAULT_BLUEGREEN_ROOT / "current_config.json",
        PROJECT_ROOT / "active_config.json",
        PROJECT_ROOT / "gateway_config.json",
        PROJECT_ROOT / "config.json",
        GENERATED_PROJECT_DIR / "config.json",
        GENERATED_PROJECT_DIR / "gateway_config.json",
        DEFAULT_BLUEGREEN_ROOT / "active_config.json",
        DEFAULT_BLUEGREEN_ROOT / active_color / "active_config.json",
        DEFAULT_BLUEGREEN_ROOT / active_color / "gateway_config.json",
        DEFAULT_BLUEGREEN_ROOT / active_color / "config.json",
        DEFAULT_BLUEGREEN_ROOT
        / active_color
        / "generated-pingora-proxy"
        / "config.json",
        DEFAULT_BLUEGREEN_ROOT
        / active_color
        / "generated-pingora-proxy"
        / "gateway_config.json",
    ]


@traceable(name="load_active_config", run_type="chain")
def load_active_config() -> tuple[dict[str, Any], str]:
    active_color = _discover_active_color()

    loader = _get_callable(
        "core.project_store",
        [
            "load_current_config",
            "load_project_config",
            "get_current_config",
            "read_current_config",
        ],
    )

    if loader is not None:
        try:
            result = _call_flex(loader)

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
        "Expected generated-projects/default-project/current_config.json, "
        "generated-pingora-proxy/config.json, active_config.json, "
        "gateway_config.json, or a blue/green generated config."
    )


@traceable(name="update_prompt_to_config", run_type="chain")
def update_prompt_to_config(
    active_config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    return apply_config_update(deepcopy(active_config), prompt)


@traceable(name="update_security", run_type="chain")
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

    print("✅ Security check passed")

    return secured if isinstance(secured, dict) else updated_config


@traceable(name="update_config_preflight", run_type="tool")
def run_config_preflight(config: dict[str, Any]) -> None:
    preflight_fn = (
        _get_callable(
            "core.preflight",
            [
                "preflight_check_config",
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
        or _get_callable(
            "agents.config_repair_agent",
            [
                "run_config_preflight",
                "config_preflight",
                "preflight_config",
                "validate_config_preflight",
            ],
        )
    )

    if preflight_fn is None:
        print("ℹ️ No config preflight function found; skipping config preflight.")
        return

    print("Running config-level preflight checks...")

    try:
        preflight_fn(
            config,
            use_docker=False,
            use_compose=True,
            check_listen_port_available=False,
        )
    except TypeError:
        # Backward-compatible fallback for older preflight functions.
        try:
            preflight_fn(
                config,
                use_docker=False,
                use_compose=True,
            )
        except TypeError:
            _call_flex(preflight_fn, config)

    print("✅ Config-level preflight checks passed")


@traceable(name="update_project_writer", run_type="tool")
def run_project_writer(config: dict[str, Any]) -> Path:
    writer_fn = _get_callable(
        "core.project_writer",
        [
            "write_project",
            "generate_project",
            "write_pingora_project",
            "generate_pingora_project",
            "create_project",
            "write",
        ],
    )

    if writer_fn is None:
        raise RuntimeError(
            "Could not find project writer. "
            "Expected core.project_writer.write_project or equivalent."
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


@traceable(name="update_container_files", run_type="tool")
def run_container_files(config: dict[str, Any]) -> None:
    compose_fn = _get_callable(
        "core.compose_writer",
        ["write_compose_files", "write_compose", "generate_compose_files"],
    )

    docker_fn = _get_callable(
        "core.docker_writer",
        ["write_docker_files", "write_dockerfile", "generate_docker_files"],
    )

    if compose_fn is not None:
        _call_flex(compose_fn, config)
        print("✅ Compose files generated")
        return

    if docker_fn is not None:
        _call_flex(docker_fn, config)
        print("✅ Docker files generated")
        return

    print("ℹ️ No container file writer found; continuing.")


@traceable(name="update_cargo_check", run_type="tool")
def run_cargo_check(
    prompt: str,
    config: dict[str, Any],
    project_dir: Path,
) -> None:
    cargo_fn = _get_callable(
        "core.runner",
        [
            "cargo_check",
        ],
    )

    if cargo_fn is not None:
        result = None

        call_attempts = [
            lambda: cargo_fn(prompt, config, project_dir=project_dir),
            lambda: cargo_fn(prompt=prompt, config=config, project_dir=project_dir),
            lambda: cargo_fn(prompt, config),
            lambda: cargo_fn(config=config),
            lambda: cargo_fn(project_dir),
        ]

        last_error: Exception | None = None

        for attempt in call_attempts:
            try:
                result = attempt()
                break
            except TypeError as exc:
                last_error = exc
                continue

        if result is False:
            raise RuntimeError("Cargo check failed after debug attempts.")

        if result is None and last_error is not None:
            raise last_error

        return

    cargo_toml = project_dir / "Cargo.toml"

    if not cargo_toml.exists():
        print(f"ℹ️ Cargo.toml not found at {cargo_toml}; skipping cargo check.")
        return

    print("Running cargo check...")

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
        ignore=shutil.ignore_patterns(
            "target",
            ".git",
            "__pycache__",
            ".pytest_cache",
        ),
    )

    return target_project


@traceable(name="update_bluegreen_deploy", run_type="chain")
def run_bluegreen_deploy(
    config: dict[str, Any],
    project_dir: Path,
    active_color: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    reliability = ReliabilityBrain()
    target_color = _inactive_color(active_color)

    reliability.record_deployment_event(
        run_id=run_id,
        event_type="update_bluegreen_deploy",
        status="started",
        color=target_color,
        details={
            "previous_active_color": active_color,
            "port": config.get("port"),
            "route_count": len(config.get("routes", [])),
        },
    )

    deploy_fn = (
        _get_callable(
            "core.bluegreen_deployer",
            [
                "deploy_config_bluegreen",
                "deploy_bluegreen",
                "bluegreen_deploy",
                "deploy",
                "run_bluegreen_deploy",
                "switch_bluegreen",
            ],
        )
        or _get_callable(
            "core.bluegreen_deploy",
            [
                "deploy_config_bluegreen",
                "deploy_bluegreen",
                "bluegreen_deploy",
                "deploy",
                "run_bluegreen_deploy",
                "switch_bluegreen",
            ],
        )
    )

    if deploy_fn is not None:
        call_attempts = [
            lambda: deploy_fn(config=config, project_dir=project_dir, active_color=active_color),
            lambda: deploy_fn(config, project_dir, active_color),
            lambda: deploy_fn(config=config, project_dir=project_dir),
            lambda: deploy_fn(config=config),
            lambda: deploy_fn(config),
            lambda: deploy_fn(project_dir=project_dir),
            lambda: deploy_fn(),
        ]

        last_error: Exception | None = None

        for attempt in call_attempts:
            try:
                result = attempt()

                if not isinstance(result, dict):
                    result = {
                        "success": True,
                        "deployed": True,
                        "active_color": target_color,
                        "live_url": LIVE_URL,
                    }

                result.setdefault("success", True)
                result.setdefault("deployed", result.get("success", True))
                result.setdefault("active_color", result.get("active_color") or target_color)
                result.setdefault("live_url", result.get("live_url") or LIVE_URL)

                if not bool(result.get("success", True)):
                    reliability.record_deployment_event(
                        run_id=run_id,
                        event_type="update_bluegreen_deploy",
                        status="failed",
                        color=result.get("active_color") or target_color,
                        version=result.get("version"),
                        details=result,
                    )
                    raise RuntimeError(result.get("error") or "Blue/green deploy failed.")

                reliability.record_deployment_event(
                    run_id=run_id,
                    event_type="update_bluegreen_deploy",
                    status="success",
                    color=result.get("active_color"),
                    version=result.get("version"),
                    details=result,
                )

                return result

            except TypeError as exc:
                last_error = exc
                continue

        raise last_error or RuntimeError("Blue/green deploy function failed.")

    compose_file = project_dir / "docker-compose.bluegreen.yml"

    if not compose_file.exists():
        result = {
            "success": True,
            "deployed": False,
            "skipped_deploy": True,
            "active_color": active_color,
            "live_url": LIVE_URL,
            "message": "No docker-compose.bluegreen.yml found; deployment skipped.",
        }

        reliability.record_deployment_event(
            run_id=run_id,
            event_type="update_bluegreen_deploy",
            status="skipped",
            color=active_color,
            details=result,
        )

        return result

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
        result = {
            "success": False,
            "deployed": False,
            "active_color": active_color,
            "live_url": LIVE_URL,
            "error": "docker build failed",
            "stdout": build.stdout,
            "stderr": build.stderr,
        }

        reliability.record_deployment_event(
            run_id=run_id,
            event_type="update_bluegreen_deploy",
            status="failed",
            color=target_color,
            details=result,
        )

        raise RuntimeError(
            "docker build failed\n\n"
            f"STDOUT:\n{build.stdout}\n\n"
            f"STDERR:\n{build.stderr}"
        )

    up = subprocess.run(up_cmd, text=True, capture_output=True)

    if up.returncode != 0:
        result = {
            "success": False,
            "deployed": False,
            "active_color": active_color,
            "live_url": LIVE_URL,
            "error": "docker compose up failed",
            "stdout": up.stdout,
            "stderr": up.stderr,
        }

        reliability.record_deployment_event(
            run_id=run_id,
            event_type="update_bluegreen_deploy",
            status="failed",
            color=target_color,
            details=result,
        )

        raise RuntimeError(
            "docker compose up failed\n\n"
            f"STDOUT:\n{up.stdout}\n\n"
            f"STDERR:\n{up.stderr}"
        )

    _write_json(
        PROJECT_ROOT / "bluegreen_state.json",
        {
            "active_color": target_color,
            "live_url": LIVE_URL,
        },
    )

    result = {
        "success": True,
        "deployed": True,
        "active_color": target_color,
        "live_url": LIVE_URL,
    }

    reliability.record_deployment_event(
        run_id=run_id,
        event_type="update_bluegreen_deploy",
        status="success",
        color=target_color,
        details=result,
    )

    return result


def _save_latest_config(config: dict[str, Any], active_color: str | None = None) -> None:
    save_fn = _get_callable(
        "core.project_store",
        [
            "save_current_config",
            "save_project_config",
            "save_config",
            "write_current_config",
        ],
    )

    if save_fn is not None:
        try:
            _call_flex(save_fn, config)
        except Exception:
            pass

    _write_json(PROJECT_ROOT / "active_config.json", config)
    _write_json(PROJECT_ROOT / "gateway_config.json", config)

    if active_color:
        _write_json(DEFAULT_BLUEGREEN_ROOT / active_color / "active_config.json", config)


def _should_skip_deploy(
    changed: bool,
    understood: bool,
    change_summary: list[str],
) -> bool:
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
    reliability = ReliabilityBrain()
    run_id = reliability.start_run(prompt, flow="update")
    stage = "load_active_config"
    active_color = "blue"
    active_config: dict[str, Any] | None = None
    change_summary: list[str] = []

    try:
        active_config, active_color = load_active_config()

        stage = "update_prompt_to_config"
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

        if _should_skip_deploy(changed, understood, change_summary):
            reliability.finish_run(
                run_id,
                status="success",
                metadata={
                    "skipped_deploy": True,
                    "reason": "no_effective_config_change",
                    "active_color": active_color,
                },
            )

            return {
                "success": True,
                "deployed": False,
                "skipped_deploy": True,
                "active_color": active_color,
                "live_url": LIVE_URL,
                "config": active_config,
                "change_summary": change_summary,
                "run_id": run_id,
            }

        stage = "security"
        secured_config = run_security(updated_config, prompt)

        stage = "config_preflight"
        run_config_preflight(secured_config)

        stage = "project_writer"
        project_dir = run_project_writer(secured_config)

        if not isinstance(project_dir, Path):
            project_dir = GENERATED_PROJECT_DIR

        stage = "container_files"
        run_container_files(secured_config)

        stage = "cargo_check"
        run_cargo_check(prompt, secured_config, project_dir)

        stage = "bluegreen_deploy"
        deploy_result = run_bluegreen_deploy(
            config=secured_config,
            project_dir=project_dir,
            active_color=active_color,
            run_id=run_id,
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

        success = bool(deploy_result.get("success", deployed))

        reliability.finish_run(
            run_id,
            status="success" if success else "failed",
            metadata={
                "deployed": deployed,
                "active_color": new_active_color if deployed else active_color,
                "live_url": deploy_result.get("live_url") or deploy_result.get("url") or LIVE_URL,
                "change_summary": change_summary,
            },
        )

        result = {
            "success": success,
            "deployed": deployed,
            "active_color": new_active_color if deployed else active_color,
            "live_url": deploy_result.get("live_url") or deploy_result.get("url") or LIVE_URL,
            "config": secured_config,
            "change_summary": change_summary,
            "run_id": run_id,
        }

        if deploy_result.get("error"):
            result["error"] = deploy_result["error"]

        if deploy_result.get("stdout"):
            result["stdout"] = deploy_result["stdout"]

        if deploy_result.get("stderr"):
            result["stderr"] = deploy_result["stderr"]

        return result

    except Exception as exc:
        failure_stage = _stage_for_exception(stage, exc)

        traffic_switched = False

        reliability_result = reliability.record_failure(
            run_id=run_id,
            stage=failure_stage,
            error=exc,
            evidence={
                "prompt": prompt,
                "active_color": active_color,
                "active_config": active_config or {},
                "change_summary": change_summary,
            },
            traffic_switched=traffic_switched,
            finish_run=True,
        )

        print("")
        print(reliability_result.report)

        if failure_stage == "config_preflight":
            print("")
            print("I could not continue because config-level preflight failed.")
            print("No project was built.")
            print("No deployment was attempted.")
            print("No traffic was switched.")
        elif failure_stage == "cargo_check":
            print("")
            print("I could not continue because generated Rust did not pass cargo check.")
            print("No deployment was attempted.")
            print("No traffic was switched.")
        elif failure_stage == "bluegreen_deploy":
            print("")
            print("I could not complete blue/green deployment.")
            print("The previous active color should remain live.")
        else:
            print("")
            print("I could not complete the update flow.")
            print("The reliability report above contains the root-cause classification.")

        return {
            "success": False,
            "deployed": False,
            "skipped_deploy": False,
            "active_color": active_color,
            "live_url": LIVE_URL,
            "config": active_config or {},
            "change_summary": change_summary or ["Update failed before deployment."],
            "error": str(exc),
            "failed_node": failure_stage,
            "reliability_report": reliability_result.report,
            "incident_id": reliability_result.incident_id,
            "root_cause": reliability_result.classification.root_cause,
            "run_id": run_id,
        }


def run_update_flow(prompt: str) -> dict[str, Any]:
    return run_update_graph(prompt)


def update_gateway_flow(prompt: str) -> dict[str, Any]:
    return run_update_graph(prompt)


def run(prompt: str) -> dict[str, Any]:
    return run_update_graph(prompt)


def main(prompt: str) -> dict[str, Any]:
    return run_update_graph(prompt)