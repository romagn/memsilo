# memsilo — multi-user memory MCP server

# Install all dependencies
install:
    poetry install

# Run the MCP server locally (stdio transport)
run:
    poetry run python mcp_server.py

# Run the same checks as CI (ruff lint + format + tests)
ci: lint fmt-check test

# Ruff lint
lint:
    poetry run ruff check .

# Ruff auto-format (writes)
fmt:
    poetry run ruff format .

# Ruff format check (no writes — CI behaviour)
fmt-check:
    poetry run ruff format --check .

# Run the test suite
test:
    poetry run pytest tests/ -v --tb=short

# Manage per-user access tokens (add/bulk-add/rotate/revoke/list/erase/export)
#   just users --json add --display-name "cust 001"      (id optional; generated if omitted)
#   just users bulk-add users.csv                    (header: user_id,display_name)
#   just users erase <user_id> --yes [--revoke]      (irreversible; LLM key must be set, not called)
#   just users export <user_id>                      (JSON; LLM key must be set, not called)
users *ARGS:
    poetry run python scripts/manage_users.py {{ARGS}}

# Build the Docker image locally
docker-build:
    docker build -t memsilo .

# Start the stack (qdrant + server); needs .env and config.yaml, see the README
docker-up:
    docker compose up -d
