from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from openai import OpenAI
from langsmith import traceable
from langsmith.wrappers import wrap_openai


client = wrap_openai(OpenAI())

VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}


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
        return json.load(f)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def find_config_path(project_dir: Path) -> Path:
    candidates = [
        project_dir / "config.json",
        project_dir.parent / "config.json",
        Path("generated-pingora-proxy") / "config.json",
        Path("generated-projects") / "default-project" / "current_config.json",
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return project_dir / "config.json"


def find_main_rs_path(project_dir: Path) -> Path:
    candidates = [
        project_dir / "src" / "main.rs",
        project_dir.parent / "src" / "main.rs",
        Path("generated-pingora-proxy") / "src" / "main.rs",
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
    path = str(path or "/").strip()

    if not path.startswith("/"):
        path = "/" + path

    lower = path.lower()

    if any(bad in lower for bad in ["cargo", "pingora-core", "typenum", "build output"]):
        return "/"

    path = re.sub(r"[^a-zA-Z0-9/_-]", "", path)

    if not path:
        return "/"

    if not path.startswith("/"):
        path = "/" + path

    return path


def normalize_upstream(upstream: Any) -> str:
    upstream = str(upstream or "127.0.0.1:3000").strip()

    upstream = upstream.replace("http://", "")
    upstream = upstream.replace("https://", "")
    upstream = upstream.replace("localhost", "127.0.0.1")
    upstream = upstream.rstrip("/")

    if "/" in upstream:
        upstream = upstream.split("/", 1)[0]

    if ":" not in upstream:
        return "127.0.0.1:3000"

    host, port_text = upstream.rsplit(":", 1)

    host = host.strip()
    port = safe_port(port_text, default=3000)

    if host not in {"127.0.0.1", "0.0.0.0"}:
        host = "127.0.0.1"

    return f"{host}:{port}"


def normalize_security(security: Any) -> dict[str, Any]:
    if not isinstance(security, dict):
        return {}

    fixed: dict[str, Any] = {}

    blocked_paths = security.get("blocked_paths")

    if isinstance(blocked_paths, list):
        clean_blocked = []

        for item in blocked_paths:
            if isinstance(item, str):
                path = normalize_path(item)

                if path not in clean_blocked:
                    clean_blocked.append(path)

        if clean_blocked:
            fixed["blocked_paths"] = clean_blocked

    allowed_methods = security.get("allowed_methods")

    if isinstance(allowed_methods, list):
        clean_methods = []

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
    fixed = dict(config)

    fixed["port"] = safe_port(
        fixed.get("port") or fixed.get("listen_port") or fixed.get("proxy_port"),
        default=9000,
    )

    routes = fixed.get("routes")
    clean_routes_by_path: dict[str, dict[str, str]] = {}

    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue

            path = normalize_path(route.get("path") or route.get("prefix") or route.get("route"))
            upstream = normalize_upstream(route.get("upstream"))

            clean_routes_by_path[path] = {
                "path": path,
                "upstream": upstream,
            }

    if not clean_routes_by_path:
        clean_routes_by_path["/"] = {
            "path": "/",
            "upstream": "127.0.0.1:3000",
        }

    fixed["routes"] = list(clean_routes_by_path.values())

    if "security" in fixed:
        security = normalize_security(fixed.get("security"))

        if security:
            fixed["security"] = security
        else:
            fixed.pop("security", None)

    return fixed


@traceable(name="runtime_agent_fix_config_with_ai", run_type="chain")
def fix_runtime_config_with_ai(
    config: dict[str, Any],
    runtime_error: str,
    main_rs: str,
) -> dict[str, Any]:
    system_prompt = """
You are an advanced Pingora runtime debugging agent.

The Rust project builds successfully, but runtime validation failed.

This means:
- cargo check passed
- Docker build passed
- container started
- health check failed OR proxy crashed OR upstream connection failed

Your job:
Fix ONLY the JSON config.

Return ONLY valid JSON.
Do not explain.
Do not use markdown.
Do not return anything except raw JSON.

Required JSON shape:

{
  "port": 8080,
  "routes": [
    {
      "path": "/api",
      "upstream": "127.0.0.1:3000"
    }
  ]
}

Rules:
- Keep the existing port unless it is invalid.
- Keep port between 1024 and 65535.
- routes must be an array.
- Each route must have path and upstream.
- path must start with /.
- upstream must be local only.
- upstream must look like 127.0.0.1:3000.
- never use localhost.
- never use http:// or https://.
- never use external domains or public IPs.
- never remove all routes.

Important Pingora runtime note:
If the error says "failed to lookup address information" and upstream is already like "127.0.0.1:3000", the config is probably valid.
Keep that upstream format. The generated Rust must split host and port correctly.

Return corrected JSON config only.
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
        model="gpt-4.1-mini",
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

    original = main_rs_path.read_text(encoding="utf-8", errors="replace")

    system_prompt = """
You are a senior Rust and Pingora runtime repair agent.

You must repair src/main.rs.

Return ONLY the complete corrected Rust file.
Do not explain.
Do not use markdown.
Do not wrap in code fences.

Problem class:
The generated Pingora proxy may panic at runtime with:

failed to lookup address information: Name or service not known

This usually happens when generated Rust passes a full upstream string like:
127.0.0.1:3000

as the hostname to HttpPeer.

Correct behavior:
- Parse upstream into host and port.
- Remove http:// and https:// if present.
- Remove trailing slash.
- Split host:port safely.
- Use host and port separately when creating HttpPeer.
- Do not pass "127.0.0.1:3000" as the hostname.

Expected helper:

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

Then use:
let (host, port) = parse_upstream(upstream);
HttpPeer::new((host.as_str(), port), false, String::new())

Keep existing security logic, routes, structs, imports, async_trait usage, and Pingora service structure.
Only repair runtime-safe upstream handling.
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
            model="gpt-4.1-mini",
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
    Deterministic repair for this runtime panic:

    failed to lookup address information: Name or service not known

    The common bad generated shape is:

    HttpPeer::new((upstream.as_str(), 80), false, String::new())

    while upstream contains "127.0.0.1:3000".

    This patches the generated source to parse host and port separately.
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
            f"        let (host, port) = parse_upstream({parse_arg});\n"
            f"        HttpPeer::new((host.as_str(), port), false, {sni})\n"
            "    }"
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
            f"        let (host, port) = parse_upstream({parse_arg});\n"
            f"        HttpPeer::new((host.as_str(), port), false, {sni})\n"
            "    }"
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
    Main entry point called by core/bluegreen.py.

    Responsibilities:
    - runtime health failure
    - Pingora runtime panic
    - upstream resolution failure
    - route/backend runtime failure

    It patches only the inactive color project directory passed by bluegreen.py.
    It must not stop or modify the active color.
    """
    runtime_error = error or output or ""
    project_path = Path(project_dir or "generated-pingora-proxy").resolve()

    config_path = find_config_path(project_path)
    main_rs_path = find_main_rs_path(project_path)

    actions: list[str] = []

    if config_path.exists():
        try:
            current_config = normalize_config(load_json(config_path))
        except Exception:
            current_config = {
                "port": 9000,
                "routes": [
                    {
                        "path": "/",
                        "upstream": "127.0.0.1:3000",
                    }
                ],
            }

        main_rs = read_text_if_exists(main_rs_path)

        try:
            ai_config = fix_runtime_config_with_ai(current_config, runtime_error, main_rs)
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