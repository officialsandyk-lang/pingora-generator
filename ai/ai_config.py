import json
from openai import OpenAI

client = OpenAI()


def extract_json(text: str) -> dict:
    text = text.strip()

    if text.startswith("```"):
        text = text.replace("```json", "")
        text = text.replace("```", "")
        text = text.strip()

    return json.loads(text)


def prompt_to_config(user_prompt: str) -> dict:
    system_prompt = """
You are an infrastructure configuration generator.

Convert the user's simple infrastructure request into ONLY valid JSON.

Return this base JSON shape:

{
  "port": 9000,
  "routes": [
    {
      "path": "/",
      "upstream": "127.0.0.1:3000"
    }
  ]
}

If the user asks for security behavior, you may also include:

{
  "security": {
    "max_request_body_bytes": 1048576,
    "allowed_methods": ["GET", "POST"],
    "blocked_paths": ["/.env", "/private"],
    "rate_limit_per_minute": 120,
    "max_connections": 1000,
    "upstream_timeout_seconds": 30
  }
}

GENERAL RULES:
- Return ONLY JSON.
- Do not include markdown.
- Do not include explanations.
- The main proxy/server port must be in "port".
- Use "routes" as an array.
- Each route must have:
  - "path"
  - "upstream"
- Use 127.0.0.1 instead of localhost.
- Do not include http:// or https:// in upstream.
- Upstream must look like "127.0.0.1:3000".
- If the user says backend 4000, use "127.0.0.1:4000".
- If the user says send traffic to backend 3000, use path "/".
- If the user gives one backend only, create one route with path "/".
- If the user mentions multiple paths or multiple backends, create one route object for each path/backend pair.
- If the user says /api to 3000 and /admin to 5000, create two routes.
- If no proxy/server port is given, use 8080.
- If no path is given, use "/".
- Never invent external/public upstreams.
- Only use local upstreams like 127.0.0.1:3000.
- Do not use ports below 1024 unless the user explicitly asks; validation may reject them later.

ROUTE RULES:
- Paths must start with /.
- Paths may only contain letters, numbers, /, _, and -.
- Do not create duplicate paths.
- If the user gives the same path twice, keep one route and use the latest backend.

SECURITY RULES:
- Only include "security" if the user asks for security, protection, blocking, rate limits, method restrictions, body size limits, timeout limits, or connection limits.
- If the user asks to block, deny, protect, or restrict paths, include:
  "security": {
    "blocked_paths": [...]
  }
- Blocked paths must:
  - start with /
  - be strings
  - preserve user-requested blocked paths
- If the user says "block /private and /internal", use:
  "blocked_paths": ["/private", "/internal"]
- If the user says "block env files", include:
  "blocked_paths": ["/.env"]
- If the user says "block git files", include:
  "blocked_paths": ["/.git"]
- If the user says "protect common sensitive paths", include common defaults:
  "blocked_paths": ["/.env", "/.git", "/wp-admin", "/wp-login.php", "/phpmyadmin", "/admin.php"]
- Do not include default blocked paths unless the user asks generally for common protection/security.
- If the user asks to only allow certain methods, include:
  "allowed_methods": [...]
- HTTP methods must be uppercase, like GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD.
- If the user says "only allow GET and POST", use:
  "allowed_methods": ["GET", "POST"]
- If the user asks for rate limiting, include:
  "rate_limit_per_minute": number
- If the user says "limit to 300 requests per minute", use:
  "rate_limit_per_minute": 300
- If the user asks for max request body size, include:
  "max_request_body_bytes": number
- If the user says "max body 2MB", use:
  "max_request_body_bytes": 2097152
- If the user asks for timeout, include:
  "upstream_timeout_seconds": number
- If the user says "timeout 10 seconds", use:
  "upstream_timeout_seconds": 10
- If the user asks for max connections, include:
  "max_connections": number
- If the user says "max 500 connections", use:
  "max_connections": 500

IMPORTANT:
- Do not invent security settings unless the user asks for them.
- If security is included, include only the security keys relevant to the user's request.
- The Security Agent will add safe defaults later.
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )

    content = response.choices[0].message.content

    return extract_json(content)