# memsilo

A self-hostable, multi-user memory server (Mem0 + Qdrant) behind an MCP endpoint. Every
caller holds their own bearer token and is only ever shown their own memories (the
separation is logical, not cryptographic; see Security). It is built for
operators whose end customers reach an LLM through a backend the operator runs; that
backend holds each customer's token and calls memsilo on their behalf.

The [README](https://github.com/romagn/memsilo#readme) has the Docker quick start. This site
holds the reference. Read [Security](security.md) before putting real customers on it:
memsilo is **not a finished SaaS**, has **no TLS built in**, **no admin API**, **no
self-signup**, and gives **logical isolation only**.

What memsilo adds to Mem0, Qdrant and the `mcp` SDK, concern by concern, with the file or
test that shows each one (and what it does not add), is in the README section
[What memsilo adds to Mem0, Qdrant and mcp](https://github.com/romagn/memsilo#what-memsilo-adds-to-mem0-qdrant-and-mcp).

## Tools

All tools are scoped to the caller automatically; none takes a `user_id`. Which user you are
is decided by your access token (HTTP transports) or `MEMSILO_DEFAULT_USER_ID` (local
`stdio`).

- `search_memory(query, limit=10)`: semantic search over your own memories.
- `add_memory(text, metadata="{}")`: store a memory; Mem0 extracts facts with the LLM.
  In Mem0 2.0.20 (tested) it only adds facts: an older fact that a new one contradicts
  stays. If nothing was extracted the reply has `stored: 0` and a `warning`. `metadata` must
  be a JSON object without the keys `user_id`, `agent_id`, `run_id` or `actor_id`.
- `get_all_memories(limit=50)`: list your memories.
- `delete_memory(memory_id)`: delete one of your own memories and its history rows; another
  user's id, or a malformed one, gets `not found or not owned by you`.
- `memory_stats()`: total count and a recent sample.

`limit` is clamped to 1-200. Two resources, `memory://stats` and `memory://all`, return the
same data as `memory_stats` and `get_all_memories`.

## Transports

- `stdio` (the default): a local process launch, **unauthenticated**, acting as
  `MEMSILO_DEFAULT_USER_ID`. For a developer's own machine, never for customers.
- `streamable-http` (endpoint `/mcp`) and `sse` (endpoint `/sse`): remote, require
  `Authorization: Bearer <token>` resolving to a non-revoked user. `/health` is open.

A session belongs to the token that created it: a request naming a session the server did
not issue to that user is refused with `403`.

## Configuration

Precedence, highest first: environment variable, then `config.yaml`, then the built-in
default. Unknown keys, wrong types and empty values are errors (only `MEMSILO_DOTENV` and
the secrets may be empty). Secrets (`ANTHROPIC_API_KEY`,
`QDRANT_API_KEY`) are read from the environment only. The annotated file is
`config.example.yaml` in the repository.

| Key | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `server.host` | `MEMSILO_SERVER_HOST` | `127.0.0.1` | Bind address for the HTTP transports. Compose sets `0.0.0.0`. |
| `server.port` | `MEMSILO_SERVER_PORT` | `8100` | Port (1-65535). |
| `server.transport` | `MEMSILO_TRANSPORT` | `stdio` | `stdio`, `sse` or `streamable-http`. Compose sets `streamable-http`. |
| `qdrant.url` | `MEMSILO_QDRANT_URL` | `http://localhost:6333` | Qdrant address. Compose sets `http://qdrant:6333`. |
| `qdrant.collection` | `MEMSILO_COLLECTION` | `memsilo` | Collection that holds every customer's memories. |
| `llm.provider` | `MEMSILO_LLM_PROVIDER` | `anthropic` | Mem0 LLM provider. |
| `llm.model` | `MEMSILO_LLM_MODEL` | `claude-haiku-4-5-20251001` | Model used to extract facts. |
| `embedder.provider` | `MEMSILO_EMBEDDER_PROVIDER` | `fastembed` | Embedding provider (runs locally by default). |
| `embedder.model` | `MEMSILO_EMBEDDER_MODEL` | `BAAI/bge-small-en-v1.5` | Embedding model. |
| `embedder.dims` | `MEMSILO_EMBEDDER_DIMS` | `384` | Vector size; must match the model. |
| `storage.users_file` | `MEMSILO_USERS_FILE` | `data/users.json` | Token store: hashes of tokens, plus `display_name` and `created_at` in clear. |
| `storage.history_db` | `MEMSILO_HISTORY_DB` | `data/history.db` | Mem0's SQLite history database. Holds memory text. |
| `telemetry` | `MEMSILO_TELEMETRY` | `false` | Mem0's anonymous usage telemetry. Off unless you turn it on. |
| (env only) | `MEMSILO_DEFAULT_USER_ID` | `default` | The user the unauthenticated `stdio` transport acts as. Must be a valid user id. |
| (env only) | `MEMSILO_CONFIG` | `config.yaml` | Path of the config file. If set, the file must exist. |
| (env only) | `MEMSILO_DOTENV` | `./.env` | Which `.env` file to read: by default only `.env` in the current directory, never a parent. An empty value disables loading. |
| (secret) | `ANTHROPIC_API_KEY` | none | LLM key; required when `llm.provider` is `anthropic`. |
| (secret) | `QDRANT_API_KEY` | none | Qdrant API key. Required by the provided compose file, which sets the bundled Qdrant to require it. Empty means no key (only for a Qdrant of your own that needs none). |

## Managing users

Run the CLI inside the server's container, as its non-root user:

```
docker compose exec memsilo python scripts/manage_users.py --json add --display-name "cust 001"
docker compose exec memsilo python scripts/manage_users.py bulk-add /tmp/users.csv
docker compose exec memsilo python scripts/manage_users.py rotate <user_id>
docker compose exec memsilo python scripts/manage_users.py revoke <user_id>
docker compose exec memsilo python scripts/manage_users.py list
docker compose exec memsilo python scripts/manage_users.py export <user_id>
docker compose exec memsilo python scripts/manage_users.py erase <user_id> --yes --revoke
```

`--json` and `--users-file PATH` are global options and go before the subcommand. `erase`
and `export` need `ANTHROPIC_API_KEY` to be set but never call the LLM provider, so a
placeholder value works for them. Both refuse an id that is not a well-formed user id (such
as `*`) before touching anything. Tokens are shown once. `list` prints one line per user,
with no header: `user_id`, `display_name`, status (`active` or `revoked`) and `created_at`
(UTC); it never prints tokens. A `user_id` is unique for the life of the store; to
re-enable a revoked user, `rotate` them.

Without Docker, run the script directly with `poetry run python scripts/manage_users.py
...`. The repository's `justfile` has the same as a shortcut, `just users ...`, for those
who use the [just](https://github.com/casey/just) command runner.

See the README for the erase and export runbook and for what `erase` cannot reach.
