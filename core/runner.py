from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from agents.debug_agent import fix_rust_code
from core.project_writer import (
    PROJECT_DIR,
    collect_expected_upstream_addresses,
    write_project,
)
from core.validator import get_upstream_port, validate_config


def _run_cargo_check(project_dir: Path) -> tuple[bool, str]:
    result = subprocess.run(
        ["cargo", "check"],
        cwd=str(project_dir),
        text=True,
        capture_output=True,
    )

    output = "\n".join(
        part
        for part in [result.stdout, result.stderr]
        if part
    )

    return result.returncode == 0, output


def _read_main_rs(project_dir: Path) -> str:
    main_rs_path = project_dir / "src" / "main.rs"

    if not main_rs_path.exists():
        return ""

    return main_rs_path.read_text(encoding="utf-8")


def _assert_project_preserved_upstreams(
    *,
    config: Dict[str, Any],
    project_dir: Path,
) -> None:
    main_rs = _read_main_rs(project_dir)
    expected = collect_expected_upstream_addresses(config)

    missing = [
        upstream
        for upstream in expected
        if upstream not in main_rs
    ]

    if missing:
        raise RuntimeError(
            "cargo_check project regeneration dropped upstream(s). "
            f"Missing from src/main.rs: {missing}. "
            f"Expected upstreams: {expected}."
        )


def _write_project_safely(
    *,
    config: Dict[str, Any],
    project_dir: Path,
) -> None:
    safe_config = copy.deepcopy(config)
    write_project(safe_config, project_dir=project_dir)
    _assert_project_preserved_upstreams(
        config=safe_config,
        project_dir=project_dir,
    )


def cargo_check(
    prompt: str,
    config: Dict[str, Any],
    project_dir: Optional[str | Path] = None,
    max_attempts: int = 3,
) -> bool:
    """
    Generate/check Rust project.

    Production-safety rule:
    This function must not mutate the canonical graph config and must not drop
    load-balancer upstreams[].

    The previous behavior could regenerate src/main.rs after Docker/Compose
    upstream rewriting and collapse a load-balanced route to the first upstream.
    """

    project_path = Path(project_dir) if project_dir is not None else PROJECT_DIR
    project_path = project_path.resolve()

    safe_config = validate_config(copy.deepcopy(config))

    # Compatibility with old runner behavior. Some code imports this helper
    # to decide demo backend port. Do not use this to collapse upstreams.
    _ = get_upstream_port(safe_config)

    last_error = ""

    for attempt in range(1, max_attempts + 1):
        print(f"🔍 Running cargo check... attempt {attempt}/{max_attempts}")

        _write_project_safely(
            config=safe_config,
            project_dir=project_path,
        )

        ok, output = _run_cargo_check(project_path)

        if ok:
            print("✅ cargo check passed")
            return True

        last_error = output
        print("❌ cargo check failed")

        if attempt >= max_attempts:
            break

        try:
            fixed_code = fix_rust_code(
                prompt=prompt,
                config=safe_config,
                error=output,
                current_code=_read_main_rs(project_path),
            )

            if isinstance(fixed_code, str) and fixed_code.strip():
                main_rs_path = project_path / "src" / "main.rs"
                main_rs_path.write_text(fixed_code, encoding="utf-8")

                _assert_project_preserved_upstreams(
                    config=safe_config,
                    project_dir=project_path,
                )
            else:
                print("⚠️ Debug agent did not return replacement Rust code")
        except Exception as exc:
            print(f"⚠️ Debug repair skipped: {exc}")

    print("❌ cargo check failed after debug attempts")

    if last_error:
        print(last_error)

    return False
