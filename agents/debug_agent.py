import json

from openai import OpenAI
from langsmith import traceable
from langsmith.wrappers import wrap_openai


client = wrap_openai(OpenAI())


def clean_code(text: str) -> str:
    text = (text or "").strip()

    if text.startswith("```"):
        text = text.replace("```rust", "")
        text = text.replace("```", "")
        text = text.strip()

    return text


@traceable(name="debug_agent_fix_rust_code", run_type="chain")
def fix_rust_code(rust_code: str, cargo_toml: str, error_output: str) -> str:
    system_prompt = """
You are a Rust Pingora debugging agent.

Fix the Rust source code based on the cargo error output.

Rules:
- Return ONLY the corrected Rust code.
- Do not return markdown.
- Do not explain anything.
- Do not include ```rust fences.
- Keep the same project purpose: Pingora reverse proxy.
- Prefer minimal fixes.
- Do not change behavior unless required to fix the error.
- Assume Cargo.toml dependencies are already correct unless the Rust code must adapt to them.
"""

    user_prompt = f"""
Cargo.toml:

{cargo_toml}

Current src/main.rs:

{rust_code}

Cargo error output:

{error_output}

Return the corrected src/main.rs only.
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )

    fixed_code = response.choices[0].message.content or ""

    return clean_code(fixed_code)