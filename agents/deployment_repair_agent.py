from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Sequence

try:
    from langsmith import traceable
except Exception:
    def traceable(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


TRANSIENT_DOCKER_PATTERNS = [
    r"failed to do request",
    r"EOF",
    r"TLS handshake timeout",
    r"i/o timeout",
    r"connection reset",
    r"temporary failure",
    r"network is unreachable",
    r"failed to resolve source metadata",
    r"failed to copy",
    r"unexpected status",
    r"toomanyrequests",
]

BASE_IMAGE_RE = re.compile(r"FROM\s+([^\s]+)", re.IGNORECASE)


@dataclass
class CommandResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    attempts: int
    repaired: bool
    classification: str


def _run_command(command: Sequence[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("DOCKER_BUILDKIT", "1")

    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
    )


def _combined_output(result: subprocess.CompletedProcess) -> str:
    return f"{result.stdout or ''}\n{result.stderr or ''}".strip()


def _is_transient_docker_failure(output: str) -> bool:
    lowered = output.lower()

    return any(
        re.search(pattern, lowered, re.IGNORECASE)
        for pattern in TRANSIENT_DOCKER_PATTERNS
    )


def _extract_base_images_from_dockerfile(dockerfile_path: str) -> list[str]:
    try:
        content = open(dockerfile_path, "r", encoding="utf-8").read()
    except FileNotFoundError:
        return []

    images: list[str] = []

    for match in BASE_IMAGE_RE.finditer(content):
        image = match.group(1).strip()
        if image and image not in images:
            images.append(image)

    return images


@traceable(name="deployment_repair_pull_base_images", run_type="tool")
def pull_base_images(project_dir: str) -> list[str]:
    dockerfile_path = os.path.join(project_dir, "Dockerfile")
    images = _extract_base_images_from_dockerfile(dockerfile_path)

    pulled: list[str] = []

    for image in images:
        result = _run_command(["docker", "pull", image], cwd=project_dir)
        if result.returncode == 0:
            pulled.append(image)

    return pulled


@traceable(name="deployment_repair_agent", run_type="chain")
def run_docker_command_with_repair(
    command: Sequence[str],
    project_dir: str,
    max_attempts: int = 3,
    retry_delay_seconds: int = 5,
) -> CommandResult:
    repaired = False
    last_result: subprocess.CompletedProcess | None = None
    classification = "unknown"

    for attempt in range(1, max_attempts + 1):
        result = _run_command(command, cwd=project_dir)
        last_result = result

        if result.returncode == 0:
            return CommandResult(
                ok=True,
                returncode=0,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                attempts=attempt,
                repaired=repaired,
                classification="success",
            )

        output = _combined_output(result)

        if _is_transient_docker_failure(output):
            classification = "transient_docker_registry_or_network_failure"
            repaired = True

            print()
            print("🛠️ Deployment repair agent detected a transient Docker registry/network failure.")
            print(f"Retrying Docker build safely... attempt {attempt}/{max_attempts}")

            pulled = pull_base_images(project_dir)
            if pulled:
                print(f"🛠️ Pulled base image(s): {', '.join(pulled)}")

            if attempt < max_attempts:
                time.sleep(retry_delay_seconds)
                continue

        classification = "non_repairable_docker_failure"
        break

    assert last_result is not None

    return CommandResult(
        ok=False,
        returncode=last_result.returncode,
        stdout=last_result.stdout or "",
        stderr=last_result.stderr or "",
        attempts=max_attempts,
        repaired=repaired,
        classification=classification,
    )