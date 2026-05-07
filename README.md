# AI Pingora Gateway Generator

![Tests](https://github.com/officialsandyk-lang/pingora-generator/actions/workflows/tests.yml/badge.svg)

An experimental AI-powered Pingora application gateway and load balancer generator.

This project turns natural-language infrastructure instructions into a working Rust/Pingora reverse proxy with route generation, blue/green deployment, live updates, security policy enforcement, runtime repair, deployment repair, and optional LangSmith tracing.

> Status: Developer preview. Generated infrastructure should be reviewed before production use.

---

## Overview

AI Pingora Gateway Generator lets you describe a gateway in natural language:

```bash
python main.py "create proxy on port 8088 with / to backend 3000, /users to backend 3000, /orders to backend 5000, /billing to backend 6000, /reports to backend 7000, and /admin to backend 8000. Block /private and /internal, only allow GET and POST, set rate limit to 120 requests per minute, max request body to 1048576 bytes, max connections to 1000, and upstream timeout to 30 seconds"
```

The system generates a Pingora-based application gateway, validates it, builds it, and deploys it with blue/green switching.

The current product positioning is:

> AI-powered Pingora application gateway and load balancer generator.

The long-term direction is:

> AI-powered ADN / application delivery platform.

---

## Current capabilities

- Natural-language gateway creation
- Natural-language live updates
- Pingora Rust project generation
- Route-based reverse proxy/load balancing
- Demo backend HTML page generation
- Clickable route index at `/`
- Stable public edge URL
- Blue/green deployment
- Active/inactive color switching
- Config repair
- Runtime repair for generated Pingora/Rust issues
- Security policy extraction
- Blocked path enforcement
- Allowed method enforcement
- Rate limit configuration
- Max request body configuration
- Max connection configuration
- Upstream timeout configuration
- Safe destructive update handling
- Duplicate/no-op update summaries
- LangSmith tracing for agent and LLM visibility

---

## Architecture

The project uses a small multi-agent control plane to generate, repair, validate, and deploy a Pingora gateway.

```text
User prompt
   |
   v
main.py / update.py
   |
   v
orchestration graph
   |
   +--> config update / prompt-to-config agent
   +--> config repair agent
   +--> security agent
   +--> project writer
   +--> runtime agent
   +--> deployment repair agent
   +--> blue/green deploy
   |
   v
generated Pingora gateway
   |
   v
http://127.0.0.1:8088
```

---

## Project structure

```text
pingora-generator/
├── agents/
│   ├── __init__.py
│   ├── config_repair_agent.py
│   ├── config_update_agent.py
│   ├── control_plane_repair_agent.py
│   ├── debug_agent.py
│   ├── deployment_repair_agent.py
│   ├── reliability_agent.py
│   ├── runtime_agent.py
│   └── security_agent.py
├── ai/
├── core/
│   ├── project_writer.py
│   └── safety.py
├── orchestration/
│   ├── graph.py
│   └── update_graph.py
├── scripts/
├── main.py
├── update.py
├── rollback.py
├── test_langsmith.py
├── test_langsmith_client.py
├── .env.example
├── .gitignore
└── README.md
```

Generated/runtime folders may appear locally:

```text
generated-pingora-proxy/
generated-projects/
backups/
reports/
```

These are usually ignored by Git.

---

## Agents

### `config_update_agent.py`

Parses natural-language update prompts and applies route/security changes.

Examples:

```text
add /inventory to backend 9001
remove /admin
block /secret and /debug
only allow GET and POST
set rate limit to 90 requests per minute
```

It also detects no-op and duplicate updates.

Example summary:

```text
Duplicate route ignored: /inventory already exists -> 127.0.0.1:9001
Route already absent: /admin
```

### `config_repair_agent.py`

Repairs invalid or incomplete gateway configs before project generation.

### `security_agent.py`

Extracts and enforces security rules such as:

- blocked paths
- allowed methods
- default dangerous blocked paths
- max request body
- max connections
- upstream timeout

### `runtime_agent.py`

Repairs generated Pingora/Rust runtime issues, especially upstream parsing and `HttpPeer` host:port failures.

### `control_plane_repair_agent.py`

Repairs Python orchestration/control-plane issues.

Example:

```text
update.py expected run_update_flow
update_graph.py actually has run_update_graph
```

Instead of crashing, it introspects the update graph and selects the valid entrypoint.

### `deployment_repair_agent.py`

Detects deployment-time problems such as transient Docker registry/network failures.

Example:

```text
failed to resolve source metadata for docker.io/library/rust:1-bookworm
```

The deployment repair agent can classify this as an infrastructure/network issue rather than a prompt/config issue.

### `debug_agent.py`

Helps inspect and explain failures during generation or deployment.

### `reliability_agent.py`

Traces reliability-related checks and future policy logic.

---

## Requirements

- Python 3.12+
- Rust toolchain
- Docker
- Docker Compose
- OpenAI API key
- Optional LangSmith API key

---

## Setup

Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

Install required Python packages.

If your project has `requirements.txt`:

```bash
pip install -r requirements.txt
```

If not, install the core packages manually:

```bash
pip install python-dotenv requests langsmith langchain langchain-openai
```

Create your local `.env` file from the example:

```bash
cp .env.example .env
```

Edit `.env` and add your own keys.

Do not commit `.env`.

---

## Environment variables

Create `.env.example` with placeholder values like this:

```env
OPENAI_API_KEY=your-openai-api-key-here
OPENAI_MODEL=gpt-4.1-mini

LANGSMITH_TRACING=true
LANGSMITH_PROJECT=ai-pingora-generator
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=your-langsmith-api-key-here

ENABLE_LANGCHAIN_UPDATE_AGENT=true
```

Your real `.env` should contain real keys:

```env
OPENAI_API_KEY=your-openai-api-key-here
LANGSMITH_API_KEY=your-langsmith-api-key-here
```

Never commit real API keys.

---

## LangSmith notes

This project supports LangSmith tracing.

For the default US LangSmith region:

```env
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
```

For EU LangSmith, the API key must be created in the EU LangSmith workspace:

```env
LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com
```

A region mismatch can produce:

```text
403 Forbidden
```

Test LangSmith auth:

```bash
python test_langsmith_client.py
```

Expected result:

```text
Auth works
Projects:
- ai-pingora-generator
```

When tracing works, LangSmith should show traces such as:

```text
main_create_gateway_flow
update_gateway_flow
config_update_agent
config_update_agent_langchain_parse
security_agent_enforce_security
runtime_agent
control_plane_repair_agent
deployment_repair_agent
```

---

## Create a gateway

Run:

```bash
python main.py "create proxy on port 8088 with / to backend 3000, /users to backend 3000, /orders to backend 5000, /billing to backend 6000, /reports to backend 7000, and /admin to backend 8000. Block /private and /internal, only allow GET and POST, set rate limit to 120 requests per minute, max request body to 1048576 bytes, max connections to 1000, and upstream timeout to 30 seconds"
```

Expected output includes:

```text
Project generated successfully
cargo check passed
Update deployed with blue/green switching
Live URL: http://127.0.0.1:8088
```

The stable local gateway URL is:

```text
http://127.0.0.1:8088
```

---

## Test the generated gateway

Test the homepage:

```bash
curl -i http://127.0.0.1:8088/
```

Test generated routes:

```bash
curl -i http://127.0.0.1:8088/users/
curl -i http://127.0.0.1:8088/orders/
curl -i http://127.0.0.1:8088/billing/
curl -i http://127.0.0.1:8088/reports/
```

Test blocked paths:

```bash
curl -i http://127.0.0.1:8088/private
curl -i http://127.0.0.1:8088/internal
```

Expected:

```text
403 Forbidden
```

Test disallowed method:

```bash
curl -i -X PUT http://127.0.0.1:8088/users/
```

Expected:

```text
405 Method Not Allowed
```

---

## Apply a live update

Example:

```bash
python update.py "add /inventory to backend 9001, add /payments to backend 9100, add /support to backend 9200, remove /admin, block /secret and /debug, keep /private and /internal blocked, only allow GET and POST, set rate limit to 90 requests per minute, max request body to 524288 bytes, max connections to 750, and upstream timeout to 20 seconds"
```

Expected summary examples:

```text
Added route: /inventory -> 127.0.0.1:9001
Added route: /payments -> 127.0.0.1:9100
Added route: /support -> 127.0.0.1:9200
Removed route: /admin
Security changed: blocked paths updated, allowed methods updated
```

The update flow uses the active blue/green config as the source of truth.

---

## Duplicate/no-op update behavior

If a route already exists with the same backend, the update flow should not silently deploy.

Example:

```bash
python update.py "add /inventory to backend 9001"
```

Expected if the route already exists:

```text
Duplicate route ignored: /inventory already exists -> 127.0.0.1:9001
```

For no-op updates, the system should skip project generation, cargo check, Docker build, and blue/green switching.

---

## Remove a route

Safe route removal:

```bash
python update.py "remove /billing"
```

If the route exists:

```text
Removed route: /billing
```

If the route does not exist:

```text
Route already absent: /billing
```

Route removal means the route is removed from gateway config. It does not delete backend data.

---

## Safe destructive command behavior

The system treats destructive words carefully.

This command is blocked by default:

```bash
python update.py delete analytics
```

Expected:

```text
Destructive command detected
No changes were applied.
```

To continue, confirm explicitly:

```bash
python update.py delete analytics --confirm
```

Expected:

```text
Safety backup created: backups/destructive-update-...
Removed route: /analytics
```

The gateway operation does not delete backend data, but it can stop traffic from reaching a route.

Safer alternatives:

```bash
python update.py "remove /analytics"
python update.py "block /analytics"
```

---

## Blue/green deployment

The gateway keeps a stable public edge URL:

```text
http://127.0.0.1:8088
```

Updates are deployed through active/inactive color switching.

Example output:

```text
Update deployed with blue/green switching
Active color: green
Live URL: http://127.0.0.1:8088
```

If deployment fails, traffic should not switch.

---

## Deployment repair

Docker registry or network failures are not gateway config failures.

Example failure:

```text
failed to resolve source metadata for docker.io/library/rust:1-bookworm
```

This usually means Docker could not fetch the Rust base image.

Manual retry:

```bash
docker pull rust:1-bookworm
```

Then rerun the update command.

The deployment repair agent is intended to classify these failures and avoid misleading prompt/config suggestions.

---

## Useful development commands

Compile-check Python files:

```bash
python -m py_compile update.py
python -m py_compile orchestration/update_graph.py
python -m py_compile agents/config_update_agent.py
```

Run LangSmith auth test:

```bash
python test_langsmith_client.py
```

Run create flow:

```bash
python main.py "create proxy on port 8088 with / to backend 3000 and /users to backend 3000"
```

Run update flow:

```bash
python update.py "add /inventory to backend 9001"
```

Run duplicate test:

```bash
python update.py "add /inventory to backend 9001"
```

Run safe delete test:

```bash
python update.py delete analytics
```

Run confirmed delete:

```bash
python update.py delete analytics --confirm
```

---

## Git hygiene

Do not commit secrets, local runtime state, generated build artifacts, or logs.

Recommended `.gitignore` entries:

```gitignore
# Secrets
.env
.env.*
!.env.example

# Python
.venv/
__pycache__/
*.pyc
.pytest_cache/

# Rust/build output
target/
**/target/

# Generated/runtime gateway output
generated-projects/
generated-pingora-proxy/

# Runtime state/config
active_config.json
gateway_config.json
bluegreen_state.json
gateway_state.json
.gateway_state.json

# Logs/reports/backups
logs.jsonl
*.jsonl
*.log
reports/
backups/

# Temp/save files
*.save

# IDE/OS
.idea/
.vscode/
.DS_Store
```

Before publishing or pushing, scan for secrets:

```bash
grep -R "OPENAI_API_KEY=" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=target
grep -R "LANGSMITH_API_KEY=" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=target
grep -R "GITHUB_TOKEN\|GITHUB_PAT" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=target
```

Safe files to commit:

```text
README.md
.env.example
.gitignore
main.py
update.py
rollback.py
agents/
ai/
core/
orchestration/
scripts/
test_langsmith.py
test_langsmith_client.py
```

Do not commit:

```text
.env
.venv/
active_config.json
gateway_config.json
logs.jsonl
generated-projects/
generated-pingora-proxy/
backups/
reports/
```

---

## Publishing recommendation

For early development, keep the repository private.

Recommended flow:

```text
Private repo first
Clean secrets and docs
Add tests
Review generated code
Publish limited developer preview later
```

If making public later, treat it as an experimental developer preview, not production-ready infrastructure.

---

## Known limitations

- This is not production-ready infrastructure.
- Generated code should be reviewed before use.
- Docker builds may fail because of transient Docker Hub/network issues.
- LangSmith keys must match the correct LangSmith region.
- Current demo backends are for local testing only.
- Advanced health checks, retries, circuit breakers, weighted routing, WAF rules, and full ADN features are future work.
- The AI update flow should be reviewed before applying to critical systems.

---

## Roadmap

Planned direction:

- Health checks
- Retry policies
- Circuit breakers
- Weighted routing
- Canary deployment
- Rollback command
- Policy diff preview
- Config validation report
- Better deployment repair
- Richer LangSmith traces
- UI control plane
- API control plane
- ADN/application delivery platform features

---

## Safety disclaimer

This project uses AI to generate and update infrastructure code.

Always review generated configs and code before using them in production environments. Do not commit secrets, production credentials, or private infrastructure configs.

---

## License

License not selected yet.

Before public release, add a license file such as:

```text


Apache License 2.0.
```

For private/internal use, this can remain undecided.