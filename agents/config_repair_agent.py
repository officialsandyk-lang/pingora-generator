import json

from openai import OpenAI
from langsmith import traceable
from langsmith.wrappers import wrap_openai


client = wrap_openai(OpenAI())


def clean_json(text: str) -> dict:
    text = text.strip()

    if text.startswith("```"):
        text = text.replace("```json", "")
        text = text.replace("```", "")
        text = text.strip()

    return json.loads(text)


@traceable(name="config_repair_agent", run_type="chain")
def repair_config(raw_config: dict) -> dict:
    system_prompt = """
You are a Pingora config repair agent.

Your job:
Repair invalid or weak JSON config BEFORE strict validation happens.

Return ONLY valid JSON.
Do not explain.
Do not use markdown.
Do not return anything except raw JSON.

Required base JSON shape:

{
  "port": 8080,
  "routes": [
    {
      "path": "/",
      "upstream": "127.0.0.1:3000"
    }
  ]
}

Optional security JSON shape:

{
  "security": {
    "blocked_paths": ["/private", "/internal"],
    "allowed_methods": ["GET", "POST"],
    "rate_limit_per_minute": 120,
    "max_connections": 1000,
    "max_request_body_bytes": 1048576,
    "upstream_timeout_seconds": 30
  }
}

SECURITY RULES:
- If the input config contains "security", preserve it unless it is unsafe or malformed.
- Do not remove valid security settings.
- Do not remove security.blocked_paths.
- Do not remove security.allowed_methods.
- Do not remove security.rate_limit_per_minute.
- Do not remove security.max_connections.
- Do not remove security.max_request_body_bytes.
- Do not remove security.upstream_timeout_seconds.
- If security values are valid, keep them.
- If blocked_paths are missing "/" prefix, add it.
- blocked_paths must be strings.
- blocked_paths must start with /.
- If allowed_methods exist, uppercase them.
- allowed_methods must be a list of strings.
- Valid HTTP methods are GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD.
- Numeric security settings must stay numbers, not strings.
- If a security key is malformed, repair only that key.
- Never delete the whole security object just because one value needs repair.

PORT RULES:
- port must be between 1024 and 65535.
- if user gives very small port like 80, 400, 900:
  prefer safe likely correction like:
  8080, 4000, 9000.

ROUTE RULES:
- routes must be an array.
- every route must contain:
  - path
  - upstream.
- Do not create duplicate route paths.
- If duplicate paths exist, keep one and prefer the last mentioned upstream.

PATH RULES:
- must start with /.
- only letters, numbers, /, _, and -.
- if missing slash, add it.
- if corrupted path exists like:
  "/45: pingora-core"
  "/typenum"
  "/cargo output"
  simplify safely to:
  "/"
  or "/api".

UPSTREAM RULES:
- upstream must be local only.
- upstream must look like:
  127.0.0.1:3000.

NEVER use:
- localhost
- http://
- https://
- external domains
- public IPs.

Fix:
- localhost → 127.0.0.1
- remove http:// and https://

BACKEND PORT RULES:
- backend ports must be between 1024 and 65535.
- if backend port is too small like:
  300 → 3000
  400 → 4000
  900 → 9000.

Prefer:
minimal safe corrections.

Do not break already-valid config.
Do not remove valid routes unnecessarily.
Do not remove valid security settings unnecessarily.

Goal:
Make config pass validate_config() and security review safely.
"""

    user_prompt = f"""
Current raw config:

{json.dumps(raw_config, indent=2)}

Return repaired JSON config only.
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