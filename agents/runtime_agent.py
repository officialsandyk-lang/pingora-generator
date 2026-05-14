from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


try:
    from langsmith import traceable
except Exception:
    def traceable(*args: Any, **kwargs: Any):
        def decorator(fn):
            return fn

        return decorator


try:
    from langsmith.wrappers import wrap_openai
    from openai import OpenAI
except Exception:
    wrap_openai = None
    OpenAI = None


VALID_METHODS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
}

SUPPORTED_RUNTIMES = {
    "local",
    "docker",
    "docker_host",
    "kubernetes",
    "k8s",
    "ecs",
    "nomad",
    "vm",
    "bare_metal",
    "baremetal",
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"


# ---------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------


def normalize_runtime(value: Any = None) -> str:
    text = str(value or "local").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")

    aliases = {
        "host": "local",
        "native": "local",
        "local_host": "local",
        "docker": "docker_host",
        "compose": "docker_host",
        "dockerhost": "docker_host",
        "docker_host": "docker_host",
        "k8s": "kubernetes",
        "kube": "kubernetes",
        "kubernetes": "kubernetes",
        "ecs": "ecs",
        "nomad": "nomad",
        "vm": "vm",
        "baremetal": "bare_metal",
        "bare_metal": "bare_metal",
    }

    return aliases.get(text, text)


def run_best_effort(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 20,
) -> str:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return result.stdout or ""
    except Exception as exc:
        return f"Failed to run {' '.join(cmd)}: {exc}"


def _get_openai_client():
    """
    Lazy OpenAI client.

    Keeps imports/tests working when OPENAI_API_KEY is not set.
    AI repair is only attempted when the client can be created.
    """

    if OpenAI is None:
        return None

    try:
        client = OpenAI()

        if wrap_openai is not None:
            client = wrap_openai(client)

        return client

    except Exception:
        return None


def clean_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()

    if text.startswith("```"):
        text = text.replace("```json", "")
        text = text.replace("```", "")
        text = text.strip()

    return json.loads(text)


def clean_rust_code(text: str) -> str:
    text = (text or "").strip()

    if text.startswith("```"):
        text = text.replace("```rust", "")
        text = text.replace("```rs", "")
        text = text.replace("```", "")
        text = text.strip()

    return text


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""

    return path.read_text(encoding="utf-8", errors="replace")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")

    return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def find_config_path(project_dir: Path) -> Path:
    candidates = [
        project_dir / "config.json",
        project_dir.parent / "config.json",
        DEFAULT_PROJECT_DIR / "config.json",
        PROJECT_ROOT / "generated-projects" / "default-project" / "current_config.json",
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return project_dir / "config.json"


def find_main_rs_path(project_dir: Path) -> Path:
    candidates = [
        project_dir / "src" / "main.rs",
        project_dir.parent / "src" / "main.rs",
        DEFAULT_PROJECT_DIR / "src" / "main.rs",
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return project_dir / "src" / "main.rs"


def safe_port(value: Any, default: int = 9000) -> int:
    try:
        port = int(value)
    except Exception:
        return default

    if 1024 <= port <= 65535:
        return port

    if port == 80:
        return 8080

    if 100 <= port <= 999:
        fixed = port * 10

        if 1024 <= fixed <= 65535:
            return fixed

    return default


def normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text.startswith("/"):
        text = "/" + text

    lower = text.lower()

    if any(
        bad in lower
        for bad in [
            "cargo",
            "pingora-core",
            "typenum",
            "build output",
            "traceback",
            "panic",
        ]
    ):
        return "/"

    text = re.sub(r"[^a-zA-Z0-9/_\-.]", "", text)

    if not text:
        return "/"

    if not text.startswith("/"):
        text = "/" + text

    if len(text) > 1:
        text = text.rstrip("/")

    return text


def normalize_upstream(upstream: Any) -> str:
    """
    Normalizes one upstream.

    Important:
    - Allows host.docker.internal for Docker runtime.
    - Allows Docker/Kubernetes service DNS names.
    - Does not force everything to 127.0.0.1.
    """

    text = str(upstream or "127.0.0.1:3000").strip()
    text = text.replace("http://", "")
    text = text.replace("https://", "")
    text = text.replace("localhost", "127.0.0.1")
    text = text.rstrip("/")

    if "/" in text:
        text = text.split("/", 1)[0]

    if text.isdigit():
        return f"127.0.0.1:{safe_port(text, default=3000)}"

    if ":" not in text:
        return "127.0.0.1:3000"

    host, port_text = text.rsplit(":", 1)
    host = host.strip() or "127.0.0.1"
    port = safe_port(port_text, default=3000)

    host = re.sub(r"[^a-zA-Z0-9_.-]", "", host)

    if not host:
        host = "127.0.0.1"

    return f"{host}:{port}"


def normalize_upstream_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        address = (
            item.get("address")
            or item.get("upstream")
            or item.get("backend")
            or item.get("target")
            or "127.0.0.1:3000"
        )

        fixed = dict(item)
        fixed["address"] = normalize_upstream(address)

        try:
            weight = int(fixed.get("weight", 1))
        except Exception:
            weight = 1

        fixed["weight"] = max(1, weight)
        return fixed

    return {
        "address": normalize_upstream(item),
        "weight": 1,
    }


def split_upstream_values(value: Any) -> list[Any]:
    if value is None:
        return []

    if isinstance(value, list):
        return value

    if isinstance(value, tuple):
        return list(value)

    text = str(value).strip()

    if not text:
        return []

    if "," in text:
        return [
            item.strip()
            for item in text.split(",")
            if item.strip()
        ]

    return [text]


def extract_route_upstreams(route: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Supports:
    - route["upstream"]
    - route["backend"]
    - route["target"]
    - route["upstreams"]
    - route["backends"]
    - route["backend_upstreams"]
    - route["load_balancer"]["upstreams"]
    - comma-separated upstream strings
    """

    items: list[Any] = []

    for key in ("upstreams", "backends", "backend_upstreams"):
        value = route.get(key)

        if isinstance(value, list):
            items.extend(value)

    load_balancer = route.get("load_balancer")

    if isinstance(load_balancer, dict):
        lb_upstreams = load_balancer.get("upstreams")

        if isinstance(lb_upstreams, list):
            items.extend(lb_upstreams)

    for key in ("upstream", "backend", "target"):
        items.extend(split_upstream_values(route.get(key)))

    output: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in items:
        if isinstance(item, str) and "," in item:
            parts = split_upstream_values(item)

            for part in parts:
                fixed = normalize_upstream_item(part)
                address = fixed["address"]

                if address not in seen:
                    seen.add(address)
                    output.append(fixed)

            continue

        fixed = normalize_upstream_item(item)
        address = fixed["address"]

        if address not in seen:
            seen.add(address)
            output.append(fixed)

    if not output:
        output.append(
            {
                "address": "127.0.0.1:3000",
                "weight": 1,
            }
        )

    return output


def normalize_balancing(value: Any, upstream_count: int = 1) -> str | None:
    text = str(value or "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")

    aliases = {
        "rr": "round_robin",
        "roundrobin": "round_robin",
        "round_robin": "round_robin",
        "rand": "random",
        "random": "random",
        "wrr": "weighted_round_robin",
        "weighted": "weighted_round_robin",
        "weighted_rr": "weighted_round_robin",
        "weighted_roundrobin": "weighted_round_robin",
        "weighted_round_robin": "weighted_round_robin",
        "lc": "least_connections",
        "least_conn": "least_connections",
        "leastconn": "least_connections",
        "least_connection": "least_connections",
        "least_connections": "least_connections",
        "iphash": "ip_hash",
        "ip_hash": "ip_hash",
        "source_ip_hash": "ip_hash",
        "sticky": "ip_hash",
        "sticky_session": "ip_hash",
    }

    normalized = aliases.get(text)

    if normalized:
        return normalized

    if upstream_count > 1:
        return "round_robin"

    return None


def normalize_security(security: Any) -> dict[str, Any]:
    if not isinstance(security, dict):
        return {}

    fixed: dict[str, Any] = {}

    blocked_paths = security.get("blocked_paths")

    if isinstance(blocked_paths, list):
        clean_blocked: list[str] = []

        for item in blocked_paths:
            if isinstance(item, str):
                path = normalize_path(item)

                if path not in clean_blocked:
                    clean_blocked.append(path)

        if clean_blocked:
            fixed["blocked_paths"] = clean_blocked

    allowed_methods = security.get("allowed_methods")

    if isinstance(allowed_methods, list):
        clean_methods: list[str] = []

        for method in allowed_methods:
            if isinstance(method, str):
                upper = method.strip().upper()

                if upper in VALID_METHODS and upper not in clean_methods:
                    clean_methods.append(upper)

        if clean_methods:
            fixed["allowed_methods"] = clean_methods

    numeric_defaults = {
        "rate_limit_per_minute": 120,
        "max_connections": 1000,
        "max_request_body_bytes": 1048576,
        "upstream_timeout_seconds": 30,
    }

    for key, default in numeric_defaults.items():
        value = security.get(key)

        if value is None:
            continue

        try:
            number = int(value)
        except Exception:
            number = default

        if number > 0:
            fixed[key] = number

    return fixed


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """
    Runtime-agent config normalizer.

    This is intentionally repair-friendly. It preserves multi-upstream routes
    and weighted upstreams instead of collapsing them to the first backend.
    """

    fixed = dict(config or {})

    fixed["port"] = safe_port(
        fixed.get("port")
        or fixed.get("listen_port")
        or fixed.get("proxy_port")
        or fixed.get("gateway_port"),
        default=8088,
    )

    routes = fixed.get("routes")
    routes_by_path: dict[str, dict[str, Any]] = {}
    upstreams_by_path: dict[str, list[dict[str, Any]]] = {}

    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue

            path = normalize_path(
                route.get("path")
                or route.get("prefix")
                or route.get("route")
                or "/"
            )

            if path not in routes_by_path:
                routes_by_path[path] = dict(route)
                upstreams_by_path[path] = []

            for upstream in extract_route_upstreams(route):
                address = upstream["address"]

                existing = next(
                    (
                        item
                        for item in upstreams_by_path[path]
                        if item["address"] == address
                    ),
                    None,
                )

                if existing is None:
                    upstreams_by_path[path].append(upstream)
                else:
                    existing["weight"] = max(
                        int(existing.get("weight", 1)),
                        int(upstream.get("weight", 1)),
                    )

    if not routes_by_path:
        routes_by_path["/"] = {
            "path": "/",
            "upstream": "127.0.0.1:3000",
            "backend": "127.0.0.1:3000",
        }
        upstreams_by_path["/"] = [
            {
                "address": "127.0.0.1:3000",
                "weight": 1,
            }
        ]

    clean_routes: list[dict[str, Any]] = []

    for path, route in routes_by_path.items():
        upstreams = upstreams_by_path.get(path) or [
            {
                "address": "127.0.0.1:3000",
                "weight": 1,
            }
        ]

        primary = upstreams[0]["address"]
        balancing = normalize_balancing(
            route.get("balancing")
            or route.get("algorithm")
            or route.get("lb_algorithm")
            or route.get("load_balancing"),
            upstream_count=len(upstreams),
        )

        cleaned = dict(route)
        cleaned["path"] = path
        cleaned["upstream"] = primary
        cleaned["backend"] = primary

        if len(upstreams) > 1:
            if any(int(item.get("weight", 1)) != 1 for item in upstreams):
                balancing = "weighted_round_robin"

            cleaned["balancing"] = balancing or "round_robin"

            if cleaned["balancing"] == "weighted_round_robin":
                cleaned["upstreams"] = [
                    {
                        "address": item["address"],
                        "weight": int(item.get("weight", 1)),
                    }
                    for item in upstreams
                ]
            else:
                cleaned["upstreams"] = [
                    item["address"]
                    for item in upstreams
                ]
        else:
            cleaned.pop("upstreams", None)
            cleaned.pop("balancing", None)

        cleaned.pop("algorithm", None)
        cleaned.pop("lb_algorithm", None)
        cleaned.pop("load_balancing", None)
        cleaned.pop("backends", None)
        cleaned.pop("backend_upstreams", None)
        cleaned.pop("target", None)
        cleaned.pop("url", None)

        clean_routes.append(cleaned)

    fixed["routes"] = clean_routes

    if "security" in fixed:
        security = normalize_security(fixed.get("security"))

        if security:
            fixed["security"] = security
        else:
            fixed.pop("security", None)

    return fixed


# ---------------------------------------------------------------------
# Cargo check
# ---------------------------------------------------------------------


@traceable(name="runtime_agent_cargo_check", run_type="tool")
def run_cargo_check(
    project_dir: str | Path,
    attempts: int = 3,
) -> dict[str, Any]:
    project_path = Path(project_dir).resolve()

    if not (project_path / "Cargo.toml").exists():
        return {
            "success": False,
            "stage": "cargo_check",
            "error_type": "missing_cargo_project",
            "summary": f"Cargo.toml not found in {project_path}",
            "project_dir": str(project_path),
        }

    last_stdout = ""
    last_stderr = ""

    for attempt in range(1, max(1, attempts) + 1):
        print(f"🔍 Running cargo check... attempt {attempt}/{attempts}")

        result = subprocess.run(
            ["cargo", "check"],
            cwd=str(project_path),
            text=True,
            capture_output=True,
        )

        last_stdout = result.stdout or ""
        last_stderr = result.stderr or ""

        if result.returncode == 0:
            print("✅ cargo check passed")

            return {
                "success": True,
                "stage": "cargo_check",
                "attempt": attempt,
                "stdout": last_stdout,
                "stderr": last_stderr,
                "project_dir": str(project_path),
            }

        main_rs_path = find_main_rs_path(project_path)
        patched = patch_known_pingora_upstream_panic(main_rs_path)

        if patched:
            run_cargo_fmt(project_path)
            continue

    return {
        "success": False,
        "stage": "cargo_check",
        "error_type": "cargo_check_failure",
        "summary": "cargo check failed",
        "stdout": last_stdout,
        "stderr": last_stderr,
        "project_dir": str(project_path),
    }


def cargo_check(project_dir: str | Path, attempts: int = 3) -> dict[str, Any]:
    return run_cargo_check(project_dir, attempts=attempts)


def runtime_check(project_dir: str | Path, attempts: int = 3) -> dict[str, Any]:
    return run_cargo_check(project_dir, attempts=attempts)


def repair_and_check(project_dir: str | Path, attempts: int = 3) -> dict[str, Any]:
    return run_cargo_check(project_dir, attempts=attempts)


# ---------------------------------------------------------------------
# AI runtime config/source repair
# ---------------------------------------------------------------------


@traceable(name="runtime_agent_fix_config_with_ai", run_type="chain")
def fix_runtime_config_with_ai(
    config: dict[str, Any],
    runtime_error: str,
    main_rs: str,
) -> dict[str, Any]:
    client = _get_openai_client()

    if client is None:
        return normalize_config(config)

    system_prompt = """
You are an advanced Pingora runtime debugging agent.

The Rust project builds successfully, but runtime validation failed.

Your job:
Fix ONLY the JSON config.
Return ONLY valid JSON.
Do not explain.
Do not use markdown.

Rules:
- Keep the existing port unless it is invalid.
- routes must be an array.
- Each route must have path and upstream.
- Preserve route["upstreams"] when present.
- Preserve route["balancing"] when present.
- Preserve weighted upstream objects when present.
- Preserve host.docker.internal when runtime mode requires it.
- path must start with /.
- upstream must look like host:port.
- never use http:// or https://.
- never remove all routes.
"""

    user_prompt = f"""
Current config:
{json.dumps(config, indent=2)}

Generated src/main.rs:
{main_rs}

Runtime failure:
{runtime_error}

Return corrected JSON config only.
"""

    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        temperature=0,
    )

    content = response.choices[0].message.content or ""
    return clean_json(content)


@traceable(name="runtime_agent_patch_main_rs_with_ai", run_type="chain")
def patch_main_rs_with_ai(
    *,
    main_rs_path: Path,
    config: dict[str, Any],
    runtime_error: str,
) -> bool:
    if not main_rs_path.exists():
        return False

    client = _get_openai_client()

    if client is None:
        return False

    original = main_rs_path.read_text(encoding="utf-8", errors="replace")

    system_prompt = """
You are a senior Rust and Pingora runtime repair agent.

Return ONLY the complete corrected Rust file.
Do not explain.
Do not use markdown.
Do not wrap in code fences.

Repair only runtime-safe upstream handling.

Important:
- Parse upstream into host and port.
- Remove http:// and https:// if present.
- Remove trailing slash.
- Split host:port safely.
- Use host and port separately when creating HttpPeer.
- Do not pass "127.0.0.1:3000" as the hostname.
- Preserve security logic.
- Preserve route matching.
- Preserve load-balancer upstream pools.
- Preserve balancing algorithms.
- Preserve Pingora service structure.
"""

    user_prompt = f"""
Current config:
{json.dumps(config, indent=2)}

Runtime error:
{runtime_error}

Current src/main.rs:
{original}
"""

    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            temperature=0,
        )

        patched = clean_rust_code(response.choices[0].message.content or "")

    except Exception:
        return False

    if "HttpPeer" not in patched:
        return False

    if len(patched) < 200:
        return False

    main_rs_path.write_text(patched, encoding="utf-8")
    return patched != original


def insert_parse_upstream_helper(text: str) -> str:
    if "fn parse_upstream(" in text:
        return text

    helper = r'''
fn parse_upstream(upstream: &str) -> (String, u16) {
    let cleaned = upstream
        .trim()
        .trim_start_matches("http://")
        .trim_start_matches("https://")
        .trim_end_matches('/');

    let mut parts = cleaned.rsplitn(2, ':');
    let port_str = parts.next().unwrap_or("80");
    let host = parts.next().unwrap_or(cleaned);
    let port = port_str.parse::<u16>().unwrap_or(80);

    (host.to_string(), port)
}
'''

    use_matches = list(re.finditer(r"^use .+;\n", text, flags=re.MULTILINE))

    if use_matches:
        insert_at = use_matches[-1].end()
        return text[:insert_at] + "\n" + helper + text[insert_at:]

    for marker in ("#[async_trait]", "impl ProxyHttp", "struct "):
        index = text.find(marker)

        if index != -1:
            return text[:index] + helper + text[index:]

    return helper + text


def normalize_parse_arg(expr: str) -> str:
    expr = expr.strip()

    if expr.endswith(".clone()"):
        base = expr[: -len(".clone()")]
        return f"&{base}"

    if expr.endswith(".to_string()"):
        base = expr[: -len(".to_string()")]
        return base

    if expr.endswith(".as_str()"):
        return expr

    if expr.startswith("&"):
        return expr

    if expr.startswith('"') and expr.endswith('"'):
        return expr

    return f"&{expr}"


@traceable(name="runtime_agent_deterministic_upstream_patch", run_type="tool")
def patch_known_pingora_upstream_panic(main_rs_path: Path) -> bool:
    """
    Deterministic repair for common Pingora runtime panic:

    Bad:
      HttpPeer::new(("127.0.0.1:3000", 80), false, String::new())

    Good:
      let (host, port) = parse_upstream(upstream);
      HttpPeer::new((host.as_str(), port), false, String::new())
    """

    if not main_rs_path.exists():
        return False

    original = main_rs_path.read_text(encoding="utf-8", errors="replace")

    if "HttpPeer::new" not in original:
        return False

    text = insert_parse_upstream_helper(original)

    tuple_pattern = re.compile(
        r"HttpPeer::new\(\(\s*(?P<expr>[^,\n]+?)\s*,\s*(?P<port>80|443)\s*\),\s*false\s*,\s*(?P<sni>String::new\(\)|\"\"\.to_string\(\))\s*\)"
    )

    def tuple_replacement(match: re.Match[str]) -> str:
        expr = match.group("expr").strip()
        sni = match.group("sni").strip()
        parse_arg = normalize_parse_arg(expr)

        return (
            "{\n"
            f"    let (host, port) = parse_upstream({parse_arg});\n"
            f"    HttpPeer::new((host.as_str(), port), false, {sni})\n"
            "}"
        )

    text, tuple_count = tuple_pattern.subn(tuple_replacement, text)

    direct_pattern = re.compile(
        r"HttpPeer::new\(\s*(?P<expr>[a-zA-Z_][a-zA-Z0-9_\.]*(?:\.as_str\(\)|\.clone\(\))?)\s*,\s*false\s*,\s*(?P<sni>String::new\(\)|\"\"\.to_string\(\))\s*\)"
    )

    def direct_replacement(match: re.Match[str]) -> str:
        expr = match.group("expr").strip()
        sni = match.group("sni").strip()
        parse_arg = normalize_parse_arg(expr)

        return (
            "{\n"
            f"    let (host, port) = parse_upstream({parse_arg});\n"
            f"    HttpPeer::new((host.as_str(), port), false, {sni})\n"
            "}"
        )

    text, direct_count = direct_pattern.subn(direct_replacement, text)

    changed = tuple_count > 0 or direct_count > 0

    if changed:
        main_rs_path.write_text(text, encoding="utf-8")

    return changed


def run_cargo_fmt(project_dir: Path) -> None:
    if not (project_dir / "Cargo.toml").exists():
        return

    try:
        subprocess.run(
            ["cargo", "fmt"],
            cwd=str(project_dir),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:
        pass


def write_runtime_report(project_dir: Path, report: str) -> None:
    try:
        path = project_dir / "runtime_agent_report.txt"
        path.write_text(report, encoding="utf-8")
    except Exception:
        pass


@traceable(name="runtime_agent_repair_runtime_error", run_type="chain")
def repair_runtime_error(
    stage: str | None = None,
    error: str | None = None,
    output: str | None = None,
    project_dir: str | None = None,
) -> str:
    """
    Main repair entrypoint for runtime, container, and Pingora failures.
    """

    runtime_error = error or output or ""
    project_path = Path(project_dir or DEFAULT_PROJECT_DIR).resolve()

    config_path = find_config_path(project_path)
    main_rs_path = find_main_rs_path(project_path)

    actions: list[str] = []

    if config_path.exists():
        try:
            current_config = normalize_config(load_json(config_path))
        except Exception:
            current_config = {
                "port": 8088,
                "routes": [
                    {
                        "path": "/",
                        "upstream": "127.0.0.1:3000",
                    }
                ],
            }

        main_rs = read_text_if_exists(main_rs_path)

        try:
            ai_config = fix_runtime_config_with_ai(
                current_config,
                runtime_error,
                main_rs,
            )
            fixed_config = normalize_config(ai_config)
            actions.append("AI config repair completed")
        except Exception as exc:
            fixed_config = normalize_config(current_config)
            actions.append(f"AI config repair skipped: {exc}")

        save_json(config_path, fixed_config)
        actions.append(f"Updated config: {config_path}")

    else:
        fixed_config = {}
        actions.append(f"Config not found near: {project_path}")

    patched_rust = False

    should_patch_rust = any(
        marker in runtime_error
        for marker in [
            "failed to lookup address information",
            "Name or service not known",
            "HttpPeer",
            "panicked at",
            "Pingora HTTP Proxy Service",
        ]
    )

    if should_patch_rust and main_rs_path.exists():
        patched_rust = patch_known_pingora_upstream_panic(main_rs_path)

        if patched_rust:
            actions.append("Deterministic Rust upstream parsing patch applied")
        else:
            actions.append("Deterministic Rust patch did not match generated source")

        ai_patched = patch_main_rs_with_ai(
            main_rs_path=main_rs_path,
            config=fixed_config,
            runtime_error=runtime_error,
        )

        if ai_patched:
            patched_rust = True
            actions.append("AI Rust runtime patch applied")
        else:
            actions.append("AI Rust runtime patch not applied")

    if patched_rust:
        run_cargo_fmt(project_path)

    report = (
        "Runtime Agent completed.\n"
        f"Stage: {stage}\n"
        f"Project dir: {project_path}\n"
        f"Config path: {config_path}\n"
        f"main.rs path: {main_rs_path}\n"
        f"Patched Rust: {patched_rust}\n"
        "\nActions:\n"
        + "\n".join(f"- {action}" for action in actions)
    )

    write_runtime_report(project_path, report)
    return report


# ---------------------------------------------------------------------
# Runtime probing helpers
# ---------------------------------------------------------------------


def _runtime_paths(project_dir: Path, port: int) -> tuple[Path, Path, Path]:
    runtime_dir = project_dir / ".runtime"
    log_dir = project_dir / "tmp" / "logs"

    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    pid_file = runtime_dir / f"local-pingora-{port}.pid"
    log_file = log_dir / f"local-pingora-{port}.log"

    return runtime_dir, pid_file, log_file


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _read_pid_file(pid_file: Path) -> int | None:
    try:
        if not pid_file.exists():
            return None

        text = pid_file.read_text(encoding="utf-8").strip()

        if not text:
            return None

        return int(text)

    except Exception:
        return None


def _kill_process_group(pid: int, timeout_seconds: int = 8) -> bool:
    if not _pid_alive(pid):
        return True

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if not _pid_alive(pid):
            return True

        time.sleep(0.25)

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass

    time.sleep(0.5)
    return not _pid_alive(pid)


def _tcp_port_open(host: str, port: int, timeout: float = 0.75) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _http_probe(
    url: str,
    timeout: float = 2.0,
) -> tuple[bool, int | None, str | None, str | None]:
    """
    Returns:
      ok, status, server_header, error
    """

    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ai-pingora-runtime-agent/1.0",
            },
        )

        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            server = response.headers.get("Server")
            return status < 500, status, server, None

    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        server = exc.headers.get("Server") if exc.headers else None

        # 403 / 404 / 405 prove a gateway answered.
        return status < 500, status, server, str(exc)

    except Exception as exc:
        return False, None, None, str(exc)


def _tail_runtime_log(log_file: Path, max_chars: int = 6000) -> str:
    try:
        if not log_file.exists():
            return ""

        return log_file.read_text(encoding="utf-8", errors="replace")[-max_chars:]

    except Exception:
        return ""


def _port_owner_summary(port: int) -> str:
    commands = [
        ["bash", "-lc", f"lsof -iTCP:{port} -sTCP:LISTEN -n -P || true"],
        ["bash", "-lc", f"ss -ltnp | grep ':{port} ' || true"],
    ]

    output_parts: list[str] = []

    for cmd in commands:
        output = run_best_effort(cmd, timeout=5)

        if output:
            output_parts.append(output.strip())

    return "\n".join(part for part in output_parts if part).strip()


def _docker_available() -> bool:
    result = subprocess.run(
        ["docker", "info"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return result.returncode == 0


def _docker_compose_available() -> bool:
    result = subprocess.run(
        ["docker", "compose", "version"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return result.returncode == 0


def _kubectl_available() -> bool:
    result = subprocess.run(
        ["kubectl", "version", "--client"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return result.returncode == 0


def _classify_local_start_failure(
    *,
    log_tail: str,
    process_returncode: int | None,
    port_open: bool,
) -> str:
    text = (log_tail or "").lower()

    if "address already in use" in text:
        return "local_gateway_port_conflict"

    if "os error 98" in text or "os error 10048" in text:
        return "local_gateway_port_conflict"

    if process_returncode is not None:
        return "local_runtime_startup_failure"

    if not port_open:
        return "local_readiness_timeout"

    return "local_runtime_startup_failure"


def classify_runtime_failure(
    *,
    runtime: str,
    error: str | None = None,
    output: str | None = None,
) -> str:
    runtime = normalize_runtime(runtime)
    text = f"{error or ''}\n{output or ''}".lower()

    if "address already in use" in text or "port is already in use" in text:
        return "local_gateway_port_conflict"

    if "docker: command not found" in text or "cannot connect to the docker daemon" in text:
        return "docker_unavailable"

    if "docker compose" in text and "not found" in text:
        return "docker_compose_unavailable"

    if "host.docker.internal" in text and (
        "name or service not known" in text
        or "failed to lookup" in text
        or "connection refused" in text
    ):
        return "host_docker_internal_unreachable"

    if "502" in text or "bad gateway" in text:
        return "health_probe_proxied_to_unavailable_upstream"

    if runtime == "local":
        return "local_runtime_startup_failure"

    if runtime == "docker_host":
        return "docker_runtime_startup_failure"

    if runtime == "kubernetes":
        return "kubernetes_runtime_not_ready"

    return "runtime_failure"


# ---------------------------------------------------------------------
# Local runtime
# ---------------------------------------------------------------------


def stop_local_gateway(
    port: int,
    project_dir: str | Path | None = None,
) -> dict[str, Any]:
    """
    Stops the managed local Pingora gateway for the given port.

    This only stops the PID written by run_local_gateway().
    It does not blindly kill arbitrary processes.
    """

    project_path = Path(project_dir or DEFAULT_PROJECT_DIR).resolve()
    _runtime_dir, pid_file, _log_file = _runtime_paths(project_path, int(port))

    pid = _read_pid_file(pid_file)

    if not pid:
        pid_file.unlink(missing_ok=True)

        return {
            "stopped": False,
            "status": "not_managed",
            "port": int(port),
            "pid_file": str(pid_file),
        }

    stopped = _kill_process_group(pid)

    if stopped:
        pid_file.unlink(missing_ok=True)

    return {
        "stopped": stopped,
        "status": "stopped" if stopped else "failed_to_stop",
        "port": int(port),
        "pid": pid,
        "pid_file": str(pid_file),
    }


@traceable(name="runtime_agent_run_local_gateway", run_type="chain")
def run_local_gateway(
    project_dir: str | Path,
    port: int,
    startup_timeout_seconds: int = 90,
    stop_existing: bool = True,
) -> dict[str, Any]:
    """
    Start generated Pingora locally with cargo run.

    Production-safe behavior:
    - Does NOT accept random already-running processes on the gateway port.
    - Stops only the previously tracked Pingora PID.
    - Fails clearly if the port is occupied by Python SimpleHTTP or anything else.
    - Treats TCP listener readiness as gateway runtime success.
    - Does not fail the gateway only because upstream backends return 502.
    """

    project_path = Path(project_dir).resolve()
    port = int(port)

    _runtime_dir, pid_file, log_file = _runtime_paths(project_path, port)
    live_url = f"http://127.0.0.1:{port}"

    if not (project_path / "Cargo.toml").exists():
        return {
            "success": False,
            "ok": False,
            "runtime_ok": False,
            "classification": "local_runtime_startup_failure",
            "error": f"Cargo.toml not found in {project_path}",
            "live_url": live_url,
            "pid_file": str(pid_file),
            "log_file": str(log_file),
        }

    old_pid = _read_pid_file(pid_file)

    if stop_existing and old_pid:
        _kill_process_group(old_pid)
        pid_file.unlink(missing_ok=True)
        time.sleep(0.5)

    # Critical safety check:
    # If port is still open after stopping the tracked PID, it is not our new
    # generated Pingora process. Do not accept it as success.
    if _tcp_port_open("127.0.0.1", port):
        return {
            "success": False,
            "ok": False,
            "runtime_ok": False,
            "classification": "local_gateway_port_conflict",
            "error": (
                f"Port {port} is already in use by an untracked process. "
                "Stop that process before starting the Pingora gateway."
            ),
            "live_url": live_url,
            "pid_file": str(pid_file),
            "log_file": str(log_file),
            "port_owner": _port_owner_summary(port),
        }

    env = os.environ.copy()
    env.setdefault("RUST_BACKTRACE", "1")

    with log_file.open("a", encoding="utf-8", errors="replace") as log:
        log.write("\n\n--- starting local Pingora gateway ---\n")
        log.write(f"project_dir={project_path}\n")
        log.write(f"port={port}\n")
        log.write(f"pid_file={pid_file}\n")

        process = subprocess.Popen(
            ["cargo", "run"],
            cwd=str(project_path),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            text=True,
            env=env,
            start_new_session=True,
        )

    pid_file.write_text(str(process.pid), encoding="utf-8")

    deadline = time.time() + max(int(startup_timeout_seconds or 90), 90)

    probe_paths = [
        "/__pingora_health",
        "/__edge_probe",
        "/",
    ]

    last_probe: dict[str, Any] | None = None

    while time.time() < deadline:
        returncode = process.poll()

        if returncode is not None:
            log_tail = _tail_runtime_log(log_file)

            classification = _classify_local_start_failure(
                log_tail=log_tail,
                process_returncode=returncode,
                port_open=False,
            )

            return {
                "success": False,
                "ok": False,
                "runtime_ok": False,
                "classification": classification,
                "error": f"Local Pingora process exited with code {returncode}.",
                "pid": process.pid,
                "live_url": live_url,
                "pid_file": str(pid_file),
                "log_file": str(log_file),
                "log_tail": log_tail,
            }

        port_open = _tcp_port_open("127.0.0.1", port)

        if port_open:
            for path in probe_paths:
                url = f"{live_url}{path}"
                ok, status, server, error = _http_probe(url)

                last_probe = {
                    "url": url,
                    "ok": ok,
                    "status": status,
                    "server": server,
                    "error": error,
                }

                if ok:
                    return {
                        "success": True,
                        "ok": True,
                        "runtime_ok": True,
                        "health_ok": True,
                        "readiness_ok": True,
                        "classification": "local_gateway_running",
                        "pid": process.pid,
                        "live_url": live_url,
                        "health_url": url,
                        "status": status,
                        "server": server,
                        "pid_file": str(pid_file),
                        "log_file": str(log_file),
                        "message": "Local Pingora gateway is running.",
                    }

            # TCP listener from the tracked Pingora process is enough.
            # HTTP may be 404 when "/" is not configured or 502 when upstreams are down.
            return {
                "success": True,
                "ok": True,
                "runtime_ok": True,
                "health_ok": False,
                "readiness_ok": True,
                "classification": "local_gateway_listener_ready",
                "pid": process.pid,
                "live_url": live_url,
                "health_url": last_probe.get("url") if last_probe else live_url,
                "status": last_probe.get("status") if last_probe else None,
                "server": last_probe.get("server") if last_probe else None,
                "pid_file": str(pid_file),
                "log_file": str(log_file),
                "warning": (
                    "Pingora is listening, but HTTP probe did not return a clean "
                    "application response. This can happen when '/' is not routed "
                    "or upstream backends are not running."
                ),
                "message": "Local Pingora gateway listener is ready.",
            }

        time.sleep(1)

    log_tail = _tail_runtime_log(log_file)
    returncode = process.poll()
    port_open = _tcp_port_open("127.0.0.1", port)

    classification = _classify_local_start_failure(
        log_tail=log_tail,
        process_returncode=returncode,
        port_open=port_open,
    )

    if port_open and process.poll() is None:
        return {
            "success": True,
            "ok": True,
            "runtime_ok": True,
            "health_ok": False,
            "readiness_ok": True,
            "classification": "local_gateway_running_after_grace_check",
            "pid": process.pid,
            "live_url": live_url,
            "pid_file": str(pid_file),
            "log_file": str(log_file),
            "warning": (
                "Pingora became TCP-ready during final grace check. "
                "HTTP application routes may still depend on backend servers."
            ),
            "message": "Local Pingora gateway is running.",
        }

    return {
        "success": False,
        "ok": False,
        "runtime_ok": False,
        "classification": classification,
        "error": (
            f"Local Pingora gateway did not become ready on {live_url} "
            f"within {max(int(startup_timeout_seconds or 90), 90)} seconds."
        ),
        "pid": process.pid,
        "live_url": live_url,
        "pid_file": str(pid_file),
        "log_file": str(log_file),
        "last_probe": last_probe,
        "port_owner": _port_owner_summary(port),
        "log_tail": log_tail,
    }


# ---------------------------------------------------------------------
# Docker / Compose / Kubernetes runtime checks
# ---------------------------------------------------------------------


@traceable(name="runtime_agent_check_docker_runtime", run_type="tool")
def check_docker_runtime(
    *,
    compose_file: str | Path | None = None,
    project_dir: str | Path | None = None,
    public_port: int | None = None,
    health_url: str | None = None,
) -> dict[str, Any]:
    if not _docker_available():
        return {
            "success": False,
            "ok": False,
            "runtime_ok": False,
            "classification": "docker_unavailable",
            "error": "Docker daemon is not available.",
        }

    if not _docker_compose_available():
        return {
            "success": False,
            "ok": False,
            "runtime_ok": False,
            "classification": "docker_compose_unavailable",
            "error": "docker compose is not available.",
        }

    evidence: dict[str, Any] = {
        "docker_ps": run_best_effort(["docker", "ps", "-a"], timeout=20),
        "docker_networks": run_best_effort(["docker", "network", "ls"], timeout=20),
    }

    if compose_file:
        compose_path = Path(compose_file)

        if not compose_path.exists():
            return {
                "success": False,
                "ok": False,
                "runtime_ok": False,
                "classification": "missing_bluegreen_compose_artifact",
                "error": f"Compose file does not exist: {compose_path}",
                "evidence": evidence,
            }

        cwd = Path(project_dir or compose_path.parent)

        evidence["compose_ps"] = run_best_effort(
            ["docker", "compose", "-f", str(compose_path), "ps", "-a"],
            cwd=cwd,
            timeout=20,
        )
        evidence["compose_logs"] = run_best_effort(
            ["docker", "compose", "-f", str(compose_path), "logs", "--tail", "120"],
            cwd=cwd,
            timeout=30,
        )

    if health_url:
        ok, status, server, error = _http_probe(health_url)

        return {
            "success": ok,
            "ok": ok,
            "runtime_ok": ok,
            "health_ok": ok,
            "classification": "docker_gateway_running" if ok else "docker_runtime_not_ready",
            "health_url": health_url,
            "status": status,
            "server": server,
            "error": error,
            "evidence": evidence,
        }

    if public_port is not None:
        tcp_open = _tcp_port_open("127.0.0.1", int(public_port))

        return {
            "success": tcp_open,
            "ok": tcp_open,
            "runtime_ok": tcp_open,
            "health_ok": False,
            "classification": "docker_gateway_listener_ready" if tcp_open else "docker_runtime_not_ready",
            "port": int(public_port),
            "evidence": evidence,
        }

    return {
        "success": True,
        "ok": True,
        "runtime_ok": True,
        "classification": "docker_runtime_available",
        "evidence": evidence,
    }


@traceable(name="runtime_agent_check_kubernetes_runtime", run_type="tool")
def check_kubernetes_runtime(
    *,
    namespace: str = "default",
    selector: str | None = None,
    health_url: str | None = None,
) -> dict[str, Any]:
    if not _kubectl_available():
        return {
            "success": False,
            "ok": False,
            "runtime_ok": False,
            "classification": "kubernetes_unavailable",
            "error": "kubectl is not available.",
        }

    cmd = ["kubectl", "get", "pods", "-n", namespace, "-o", "wide"]

    if selector:
        cmd.extend(["-l", selector])

    pods = run_best_effort(cmd, timeout=20)

    evidence = {
        "pods": pods,
        "services": run_best_effort(
            ["kubectl", "get", "svc", "-n", namespace, "-o", "wide"],
            timeout=20,
        ),
    }

    lowered = pods.lower()
    pods_ready = "running" in lowered and not any(
        bad in lowered
        for bad in ["crashloopbackoff", "imagepullbackoff", "errimagepull", "pending"]
    )

    if health_url:
        ok, status, server, error = _http_probe(health_url)

        return {
            "success": ok,
            "ok": ok,
            "runtime_ok": pods_ready and ok,
            "health_ok": ok,
            "classification": "kubernetes_gateway_running" if ok else "kubernetes_runtime_not_ready",
            "health_url": health_url,
            "status": status,
            "server": server,
            "error": error,
            "evidence": evidence,
        }

    return {
        "success": pods_ready,
        "ok": pods_ready,
        "runtime_ok": pods_ready,
        "classification": "kubernetes_runtime_available" if pods_ready else "kubernetes_runtime_not_ready",
        "evidence": evidence,
    }


@traceable(name="runtime_agent_run_gateway_runtime", run_type="chain")
def run_gateway_runtime(
    *,
    runtime: str,
    project_dir: str | Path,
    port: int,
    startup_timeout_seconds: int = 90,
    stop_existing: bool = True,
    compose_file: str | Path | None = None,
    namespace: str = "default",
    selector: str | None = None,
    health_url: str | None = None,
) -> dict[str, Any]:
    """
    Environment-aware runtime dispatcher.

    Implemented:
    - local: starts cargo run and validates tracked Pingora process.
    - docker/docker_host: checks Docker/Compose and optional health URL/port.
    - kubernetes: checks kubectl/pods and optional health URL.

    Future environments return a clear not-implemented classification instead
    of silently pretending success.
    """

    normalized = normalize_runtime(runtime)

    if normalized == "local":
        return run_local_gateway(
            project_dir=project_dir,
            port=port,
            startup_timeout_seconds=startup_timeout_seconds,
            stop_existing=stop_existing,
        )

    if normalized == "docker_host":
        return check_docker_runtime(
            compose_file=compose_file,
            project_dir=project_dir,
            public_port=port,
            health_url=health_url,
        )

    if normalized == "kubernetes":
        return check_kubernetes_runtime(
            namespace=namespace,
            selector=selector,
            health_url=health_url,
        )

    return {
        "success": False,
        "ok": False,
        "runtime_ok": False,
        "classification": f"{normalized}_runtime_not_implemented",
        "error": (
            f"Runtime '{normalized}' is recognized but not implemented in "
            "runtime_agent.py yet."
        ),
        "runtime": normalized,
        "project_dir": str(project_dir),
        "port": int(port),
    }


# ---------------------------------------------------------------------
# Compatibility aliases
# ---------------------------------------------------------------------


def handle_runtime_error(*args, **kwargs):
    return repair_runtime_error(*args, **kwargs)


def debug_runtime_error(*args, **kwargs):
    return repair_runtime_error(*args, **kwargs)


def analyze_runtime_error(*args, **kwargs):
    return repair_runtime_error(*args, **kwargs)


def runtime_repair(*args, **kwargs):
    return repair_runtime_error(*args, **kwargs)


def run_runtime_agent(*args, **kwargs):
    return repair_runtime_error(*args, **kwargs)


def heal_runtime(*args, **kwargs):
    return repair_runtime_error(*args, **kwargs)


__all__ = [
    "run_local_gateway",
    "stop_local_gateway",
    "run_gateway_runtime",
    "check_docker_runtime",
    "check_kubernetes_runtime",
    "run_cargo_check",
    "cargo_check",
    "runtime_check",
    "repair_and_check",
    "repair_runtime_error",
    "handle_runtime_error",
    "debug_runtime_error",
    "analyze_runtime_error",
    "runtime_repair",
    "run_runtime_agent",
    "heal_runtime",
    "classify_runtime_failure",
    "normalize_runtime",
    "normalize_config",
]