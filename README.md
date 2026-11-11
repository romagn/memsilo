# memsilo

memsilo is a self-hostable, multi-user memory server for LLM applications. It puts
[Mem0](https://github.com/mem0ai/mem0) and [Qdrant](https://qdrant.tech) behind an
[MCP](https://modelcontextprotocol.io) endpoint where **every caller holds their own
bearer token and is only ever shown their own memories**.

It is built for one situation: you run a service whose end customers are people (often
vulnerable, often non-technical) who reach an LLM through something like a text message.
Your backend talks to the model on their behalf and needs a long-term memory per
customer. Your backend holds one token per customer and passes it to memsilo; memsilo
stores, searches and deletes only that customer's memories. That separation is logical
(one shared Qdrant collection, filtered by user id), not cryptographic; see below and
[docs/security.md](docs/security.md).

## What memsilo is not

Read this before you rely on it.

- **Not a finished SaaS.** It is a small server you operate yourself. You own backups,
  upgrades, monitoring and incident response.
- **No TLS built in.** It speaks plain HTTP. Put a TLS-terminating reverse proxy in front
  of it (see [TLS and reverse proxy](#tls-and-reverse-proxy)).
- **No admin API and no self-signup.** Customers are created by an operator running a
  command on the server. There is no HTTP endpoint that creates, lists or reads other
  users.
- **Logical isolation only.** All customers share one Qdrant collection, separated by a
  `user_id` filter. There is no per-customer encryption or per-customer database.
- **Your backend is the single point of trust.** It holds every customer's token. If it
  is compromised, every customer's memories are exposed.
- **No rate limiting.** Do that in your proxy or backend.
- **No Helm chart or Kubernetes manifests in v1.** Docker Compose is the supported
  deployment, with two persistent volumes: Qdrant storage and `./data`.

The full threat model, and what the isolation does and does not protect against, is in
[docs/security.md](docs/security.md).

## What memsilo adds to Mem0, Qdrant and mcp

memsilo is a thin layer. [Mem0](https://github.com/mem0ai/mem0), Qdrant and the
[`mcp` Python SDK](https://github.com/modelcontextprotocol/python-sdk) do the real work:
fact extraction, embeddings, storage, search and the protocol. They are general-purpose
libraries, and a multi-tenant access layer is not their job; nothing below is a defect in
them. The table says what memsilo puts around them so that many customers can share one
deployment. Every statement about a library was checked against the versions pinned in
`poetry.lock` (mem0ai 2.0.20, mcp 2.1.1, qdrant-client 1.19.0, with the Qdrant 1.19.0
server image); other versions may behave differently.

| Concern | The libraries on their own (pinned versions) | What memsilo adds | Where to verify |
| --- | --- | --- | --- |
| Authentication | `mcp` has building blocks for OAuth bearer tokens (a token verifier interface and auth middleware) and leaves issuing and storing tokens to the application. Mem0 and Qdrant have no notion of a calling end user. | One bearer token per customer on every HTTP request; a missing, unknown or revoked token gets `401`. Only `/health` is open. | `mcp_server.py`, `tests/test_auth.py` |
| Identity binding | Mem0's `add`, `search`, `get_all` and `delete_all` take the user id from their caller, who chooses it. | No tool has a `user_id` parameter. The id comes from the token, and every Mem0 call is scoped to it. | `tests/test_isolation.py` |
| Session ownership (SSE and streamable HTTP) | `mcp` binds a session to the credential that created it and refuses any other, once the application puts an authenticated user into the request. | Puts the token's user there, so that check runs, and adds a second, independent check in its own middleware: `403` for any session id the server did not issue to this user. | `tests/test_session_hijack.py`, `tests/test_sse_e2e.py`, `tests/test_transport_e2e.py` |
| Delete ownership | Mem0's `Memory.delete(memory_id)` deletes by id; it does not check whose memory it is, and it keeps the deleted text in its history table. | `delete_memory` refuses anything that is not a well-formed memory id, reads the memory and deletes it only if it belongs to the caller, then purges that memory's history rows and checks that none is left. | `tests/test_delete_ownership.py`, `tests/test_isolation.py`, `tests/test_tool_hardening.py` |
| Identity keys in metadata | Mem0's `add` drops `user_id`, `agent_id`, `run_id` and `actor_id` from caller metadata and logs a warning. | `add_memory` refuses such metadata with an error the caller sees. | `tests/test_isolation.py` |
| Empty extraction | When the LLM's reply is empty (for example a reasoning model that spent its whole token budget thinking), Mem0 2.0.20 stores nothing and reports no error. | `add_memory` still answers `stored: 0`, but adds a `warning` and logs one on the server (without the text). | `tests/test_tool_hardening.py` |
| Result limits | Mem0's `search` and `get_all` return at most 20 results unless `top_k` is given, and `search` silently ignores keyword arguments it does not know, such as `limit=`. | Tools take `limit`, clamp it to 1-200 and pass it to Mem0 as `top_k`. | `tests/test_isolation.py` |
| Telemetry | Mem0 sends anonymous usage telemetry unless `MEM0_TELEMETRY` turns it off. onnxruntime 1.29, which fastembed uses for embeddings, starts Microsoft telemetry when it is imported. The Qdrant server reports telemetry unless it is disabled. | Mem0's off by default; onnxruntime's and Hugging Face Hub's always off, set before Mem0 (and through it onnxruntime) is imported, in the server and the CLI; the compose Qdrant runs with telemetry disabled. See [Network traffic](docs/security.md#network-traffic). | `tests/test_memory.py`, `tests/test_manage_users.py`, `tests/test_deploy.py` |
| Erasing a customer | Mem0's `delete_all(user_id=...)` removes the memories from Qdrant, but writes a history row holding each deleted text, and leaves the cached conversation messages in its SQLite database. Qdrant's own deletes are soft: the old payload bytes can stay in its storage files for a while. | `erase` deletes the memories (expired ones too), purges the history rows of the memory ids it collected and that customer's cached messages, then checks in the same run that none of them is left and fails loudly, listing the ids, if any is. It refuses malformed ids such as `*`, which Mem0's Qdrant store treats as "match any user". It does not, and cannot, scrub Qdrant's storage files: see [Erasing and exporting a customer](#erasing-and-exporting-a-customer). | `memory.py`, `tests/test_manage_users.py` |
| Export | Mem0's open-source `Memory` class lists memories with `get_all` and has no export command. (The same package's client for the hosted Mem0 platform, `MemoryClient`, has an export API; memsilo does not use it.) | `export <user_id>` prints one customer's current memories, expired ones included, as JSON. | `tests/test_manage_users.py` |
| User store | None. | Validated or generated opaque ids, tokens shown once and stored only as hashes, atomic writes under a file lock, `rotate` and `revoke`, bulk onboarding from CSV, `--json` output. | `users.py`, `tests/test_users.py` |
| Configuration | Mem0 takes a Python dict. | One precedence order (environment, `config.yaml`, default). Unknown keys, wrong types, out-of-range numbers and empty values stop the server at startup; secrets come from the environment only. | `config.py`, `tests/test_config.py` |
| Embedding size | Mem0's Qdrant store reuses an existing collection without comparing its vector size with the embedder's. | Refuses to start when the two differ. | `tests/test_memory.py` |
| Backend errors | Mem0 and Qdrant exceptions can carry internal addresses and response bodies. | Tools log the exception on the server and return only `internal error`. | `tests/test_tool_errors.py` |
| Docker | The Qdrant image runs without an API key unless you configure one. | Qdrant requires `QDRANT_API_KEY` (compose refuses to start without it) and is not published on the host; non-root image (uid 10001) with both embedding models baked in and no downloads at runtime; memsilo on loopback only; `./data` mode `700`. | `Dockerfile`, `docker-compose.yml`, `tests/test_deploy.py` |
| The repository itself | Not applicable. | Tests that the documentation matches the code (configuration tables, variables, links) and that no private strings are committed. | `tests/test_docs.py`, `tests/test_no_private_strings.py` |

### What memsilo does not add

- **Physical isolation.** Every customer's memories are in one Qdrant collection. Mem0's
  Qdrant store filters them by the `user_id` memsilo passes; nothing is encrypted or stored
  per customer.
- **An admin API, rate limiting or TLS.** See [What memsilo is not](#what-memsilo-is-not).
- **Anything about memory itself.** Fact extraction, search, storage, embeddings and the
  MCP protocol are Mem0, Qdrant, fastembed and `mcp`, unchanged, with their behaviour and
  limits (for example the 4 MiB request-body cap of the `mcp` transports). In our tests Mem0
  2.0.20 only added facts: "switched to insulin" was stored next to "takes metformin", not
  instead of it.

## Quick start (Docker)

Usually a few minutes, depending on your network. You need Docker with the Compose plugin
and an LLM: an Anthropic API key by default, or a local or OpenAI-compatible server (see
[Using a local or OpenAI-compatible LLM](#using-a-local-or-openai-compatible-llm)). Mem0
uses it to extract facts from what customers say.
Docker Compose is the supported deployment; it uses two persistent stores: the Qdrant
storage volume and the `./data` directory.

```sh
git clone https://github.com/romagn/memsilo.git
cd memsilo

cp .env.example .env              # then put your key in .env: ANTHROPIC_API_KEY=...
# A random key for the bundled Qdrant (it refuses requests without one):
echo "QDRANT_API_KEY=$(openssl rand -hex 32)" >> .env
# (no openssl? python3 -c "import secrets; print(secrets.token_hex(32))")
cp config.example.yaml config.yaml

docker compose pull               # the published image: a few hundred MB, no build needed
docker compose up -d
# wait until it is ready (up to 2 minutes)
for i in $(seq 60); do curl -fsS http://127.0.0.1:8100/health && break; [ "$i" = 60 ] && echo "not ready after 2 minutes: docker compose logs memsilo"; sleep 2; done
# ok
```

The image is `rmagnorecon/memsilo` on Docker Hub, built by this repository's CI from each
version tag (`0.1.0`, plus `latest`; `linux/amd64` only). The compose file pins the version
that matches the checkout, so `git pull` moves you to the matching image. To use another tag
or your own registry set `MEMSILO_IMAGE` (for example `MEMSILO_IMAGE=registry.example.com/memsilo:0.1.0`).
To build the image from your checkout instead (another platform, or local changes), skip
`docker compose pull` and run `docker compose up -d --build`: usually a few minutes, depending
on your network. That build is tagged with the same name, replacing a pulled copy.

`docker compose up -d` returns as soon as the containers have started, before the server
inside is listening: a `curl` straight away can fail with `Connection reset by peer`. Poll
`/health` as above (in our test it answered about three seconds later), in scripts too. If
it is not ready after two minutes the loop stops and says so; `docker compose logs memsilo`
shows why (for example a missing key or an embedding-size mismatch).

`config.yaml` must exist before `docker compose up`: the compose file mounts it read-only
and refuses to start without it (`bind source path does not exist`). `.env` is required
too: compose refuses to start without `QDRANT_API_KEY` (`required variable QDRANT_API_KEY is
missing a value`), and with the default `anthropic` provider the server refuses to start
without `ANTHROPIC_API_KEY`. `.env.example` leaves `QDRANT_API_KEY=` empty; the `echo` line
above appends a value, and the last one in the file wins.

Create a customer and get their token (shown once, never stored in clear):

```sh
docker compose exec memsilo python scripts/manage_users.py --json add --display-name "cust 001"
```

```json
{
  "user_id": "cust_0123456789abcdef",
  "display_name": "cust 001",
  "token": "<token>"
}
```

Check that authentication works. Without a token you get `401`; with one, the MCP
`initialize` handshake succeeds:

```sh
curl -sS -X POST http://127.0.0.1:8100/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

The compose file publishes memsilo on `127.0.0.1:8100` only. Qdrant is **not published** on
the host, but that does not make it unreachable: its address on the Docker bridge network
is routable from the host itself, so any local user or process, and other containers on the
same Docker host, can connect to it. **The API key is what protects it.** Without the key
Qdrant answers `401`; with it, it serves every customer's memories, so keep `.env` as secret
as the data. Nothing is reachable from other machines until you put a proxy in front of
memsilo.

If you need to look at Qdrant for debugging, add a mapping to the `qdrant` service
temporarily and remove it afterwards:

```yaml
    # ports:
    #   - "127.0.0.1:6333:6333"   # DEBUGGING ONLY
```

Every request still needs the key (`curl -H "api-key: <key>" ...`). While that port is open,
anyone on the host who has the key can read and change every customer's memories.

Stop with `docker compose down` (data survives; `docker compose down -v` also deletes the
Qdrant volume, that is, every memory; it does not touch `./data`).

### Upgrading

```sh
git pull
docker compose pull               # or: docker compose up -d --build, to build from source
docker compose up -d
for i in $(seq 60); do curl -fsS http://127.0.0.1:8100/health && break; [ "$i" = 60 ] && echo "not ready after 2 minutes: docker compose logs memsilo"; sleep 2; done
```

The Qdrant volume and `./data` persist across the upgrade: customers, tokens and memories
stay. Back up both first (see [Where the data lives](#where-the-data-lives)).

## Configuration

Settings come from three places. Highest wins: **environment variable, then
`config.yaml`, then the built-in default.** Unknown keys, wrong types, an out-of-range
`server.port` or `embedder.dims`, an unknown transport, and an empty or blank value for
any setting are errors that name the offending key (only `MEMSILO_DOTENV` and the two
secrets may be empty, with the meanings below). `config.yaml` is looked up in the working directory (`/app/config.yaml` in
the container) unless `MEMSILO_CONFIG` names another file. Every key, with its default, is
in [`config.example.yaml`](config.example.yaml).

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
| (env only) | `MEMSILO_DOTENV` | `./.env` | Which `.env` file to read (see below). An empty value disables loading. |
| (secret) | `ANTHROPIC_API_KEY` | none | LLM key; required when `llm.provider` is `anthropic`. |
| (secret) | `QDRANT_API_KEY` | none | Qdrant API key. Required by the provided compose file, which sets the bundled Qdrant to require it. Empty means no key (only for a Qdrant of your own that needs none). |

**`.env` loading.** On a bare-metal run, memsilo reads `.env` from the *current working
directory only*, never from a parent directory, and real environment variables always win
over it. Set `MEMSILO_DOTENV` to a path to read that file instead, or to an empty value to
read none. (With compose, `.env` is passed in by `env_file` and nothing is read inside the
container.)

**Secrets come from the environment only.** A key in `config.yaml` whose name looks like a
secret is rejected. With compose, put them in `.env`. In compose the variables set in
`docker-compose.yml` (`MEMSILO_QDRANT_URL`, `MEMSILO_TRANSPORT`, `MEMSILO_SERVER_HOST`)
override `config.yaml`, so changing `server.host`, `server.transport` or `qdrant.url` in
`config.yaml` has no effect until you edit the compose file. Changing `server.port` (or
`MEMSILO_SERVER_PORT`) also breaks the compose `8100:8100` port mapping; edit both.

**Changing the embedder needs a new collection.** At startup memsilo compares
`embedder.dims` with the existing collection's vector size and **refuses to start** if they
differ. Pick a new `qdrant.collection` name (old memories stay in the old collection and
are not migrated).

**Mem0 sends memory text to the configured LLM provider** (Anthropic by default) every time
a memory is added. Anything a customer tells your service can leave your server that way.
With the provided image that is the only outbound connection memsilo makes; see
[Network traffic](docs/security.md#network-traffic).

### Using a local or OpenAI-compatible LLM

Mem0's `openai` provider works with any server that speaks the OpenAI chat API, such as
Ollama. No code change is needed; set these in `.env`:

```sh
MEMSILO_LLM_PROVIDER=openai
MEMSILO_LLM_MODEL=<model>                     # for example a small instruction model
OPENAI_BASE_URL=http://ollama-host:11434/v1   # your server's OpenAI-compatible endpoint
OPENAI_API_KEY=unused                         # any non-empty value if the server needs none
ANTHROPIC_API_KEY=                            # not needed with this provider
```

(`ANTHROPIC_API_KEY` is only required when `llm.provider` is `anthropic`.) With compose the
memsilo container must be able to reach that address. Things to know:

- **Memory text goes to that endpoint**, the same as it would to Anthropic. Use plain HTTP
  only on a network you trust.
- **Disable reasoning ("thinking") on reasoning models.** If the model spends its token
  budget reasoning, the reply comes back empty and Mem0 stores nothing; memsilo then
  answers `stored: 0` with a `warning`. memsilo cannot ask Mem0 to turn reasoning down:
  Mem0 2.0.20 sends `reasoning_effort` only for models it recognises as reasoning models,
  and memsilo passes Mem0 only the model name and key. Turn reasoning off on the server
  side (for example in the model's settings), or use a non-reasoning model.
- **Extraction quality is the model's.** Small models paraphrase or pad facts.
- Tested once, with synthetic data: Ollama 0.33.2 serving `qwen3.5:4b`, about 6 to 9 seconds
  per `add_memory`.

## Adding customers

The CLI is `scripts/manage_users.py`. Run it **inside the memsilo container**
(`docker compose exec memsilo ...`), see [Where the data lives](#where-the-data-lives).
Global options (`--json`, `--users-file PATH`) go before the subcommand.

```sh
# Opaque, generated id (recommended): cust_ plus 16 random hex characters
docker compose exec memsilo python scripts/manage_users.py --json add --display-name "cust 001"

# Your own id: letters, digits, "_" and "-", starting with a letter or digit, up to 64 chars
docker compose exec memsilo python scripts/manage_users.py add alice --display-name "cust 002"

# Many at once from a CSV (header: user_id,display_name; leave user_id empty to generate one)
docker compose exec memsilo python scripts/manage_users.py --json bulk-add /tmp/users.csv

docker compose exec memsilo python scripts/manage_users.py list              # never prints tokens
docker compose exec memsilo python scripts/manage_users.py --json rotate <user_id>   # new token, old one stops working
docker compose exec memsilo python scripts/manage_users.py revoke <user_id>          # token stops working
```

A bulk-add file is UTF-8 CSV whose first line is exactly `user_id,display_name`. Leave
`user_id` empty to have one generated. Put a value that contains a comma in double quotes:

```csv
user_id,display_name
,cust 002
bob,"cust 003, pilot"
```

`list` prints one line per user, with no header: `user_id`, `display_name`, status
(`active` or `revoked`) and `created_at` (UTC). It never prints tokens.

`bulk-add` reads a file **inside the container**. Copy your CSV in with
`docker compose cp users.csv memsilo:/tmp/users.csv` and pass `/tmp/users.csv`. The whole
file is validated before anything is written. `--json` is honoured by `add`, `bulk-add`,
`rotate`, `erase` and (always) `export`; `list` and `revoke` print plain text.

Tokens look like `ms_<user_id>_<43 random characters>`. They are shown once; only a hash is
stored. A `user_id` is unique for the life of the store, revoked or not; to give a revoked
customer a working token again use `rotate`.

**Use opaque ids and a non-identifying `--display-name`.** Do not use phone numbers, names or email addresses as `user_id`. Ids
appear in the token itself, in `users.json` and in Qdrant. Omit the id and memsilo generates one;
keep the mapping from id to real person in your own backend. The same goes for
`--display-name`: use an internal customer number such as `cust 001`, never a real name or
phone number, because the label is stored in clear in `users.json` and survives `erase`
(see below).

## Connecting a client

Your backend (or any MCP client) connects to `/mcp` with the customer's token as a bearer
token. For the `sse` transport the endpoint is `/sse` instead. Through a proxy:

```sh
claude mcp add --transport http memory https://memory.example.com/mcp \
  --header "Authorization: Bearer <token>"
```

Tools exposed (all scoped to the token's user; none takes a `user_id`):

| Tool | What it does |
| --- | --- |
| `search_memory(query, limit=10)` | Semantic search. `limit` is clamped to 1-200. |
| `add_memory(text, metadata="{}")` | Mem0 extracts facts with the LLM and stores them. Mem0 2.0.20, in our tests, only adds: a new fact does not replace an older one it contradicts (both stay). When nothing was extracted the reply has `stored: 0` and a `warning`. `metadata` must be a JSON object and may not contain `user_id`, `agent_id`, `run_id` or `actor_id`. |
| `get_all_memories(limit=50)` | List memories. `limit` is clamped to 1-200. |
| `delete_memory(memory_id)` | Delete one of the caller's memories and purge its history rows from `history.db`. Another user's id, or anything that is not a memory id, gets `not found or not owned by you`. If the delete worked but the purge did not, the reply has `deleted: true` and an `error`, and the server log says why. |
| `memory_stats()` | Count and a short sample. |

`add_memory` calls the LLM, so it needs a working LLM provider.

## TLS and reverse proxy

memsilo does not terminate TLS. Run a proxy on the host in front of `127.0.0.1:8100`. With
[Caddy](https://caddyserver.com), which obtains certificates automatically:

```
memory.example.com {
    reverse_proxy 127.0.0.1:8100
}
```

Do not publish port 8100 to the internet directly, and do not expose Qdrant at all.

**Run it through the provided compose file, or rewrite the Host header.** If memsilo is
bound to its default `127.0.0.1` (for example when you run `python mcp_server.py` yourself on
the same machine as the proxy), the underlying `mcp` library enables DNS-rebinding
protection that only accepts loopback `Host` headers. A proxy that forwards the public host
name then gets **HTTP 421 `Invalid Host header`** (and a request carrying a non-loopback
`Origin` header gets 403). Running `python mcp_server.py` directly also needs
`MEMSILO_TRANSPORT=streamable-http`, since the default transport is `stdio`. We tested this against a loopback-bound
instance:

```
Host: 127.0.0.1:8101           -> 200
Host: localhost:8101           -> 200
Host: memory.example.com       -> 421 Invalid Host header
Host: memory.example.com:443   -> 421 Invalid Host header
```

The provided `docker-compose.yml` sets `MEMSILO_SERVER_HOST=0.0.0.0` inside the container,
which disables that check (a request with `Host: memory.example.com` to the compose
container returned 200), and publishes the port on the host's loopback only, so the proxy
works unchanged. If you bind to loopback yourself, make the proxy send a loopback Host
instead (in Caddy: `reverse_proxy 127.0.0.1:8100 { header_up Host 127.0.0.1:8100 }`).

## Where the data lives

- **Qdrant volume** (`qdrant-storage`): every customer's memory text and vectors.
- **`./data`** (mounted at `/app/data`): `users.json` (token hashes plus display names), `users.json.lock`, and
  `history.db` (Mem0's history database, which also contains memory text).

The container runs as a non-root user with the fixed uid/gid **10001**. `users.json` is
replaced atomically on every change, so its owner becomes whoever ran the command. A
one-shot `data-init` service in the compose file therefore sets `./data` to
`10001:10001` mode `700` on every `docker compose up`, with no `sudo` on the host. Two
consequences:

- **Always run the CLI as the container user:** `docker compose exec memsilo ...`, never
  `-u root`. We confirmed that a `users.json` written as root makes the server fail with
  `PermissionError` (HTTP 500) until ownership is repaired; `docker compose up -d` repairs it.
- Other users on the host cannot read `./data`, and neither can you without `sudo`. Read it
  through the container (`docker compose exec memsilo ls -l /app/data`).

### Starting over, and deleting the checkout

After the first `docker compose up`, `./data` on the host belongs to uid 10001 with mode
`700`. For your own user on the host that means:

- `git status` in the checkout prints `warning: could not open directory 'data/':
  Permission denied`. Git cannot look inside to check for untracked files. It is harmless:
  everything in `data/` except `.gitkeep` is git-ignored.
- `rm -rf data/*` and deleting the checkout fail with `Permission denied`, because your
  shell cannot even list `data/`. (`git clean` skips the git-ignored `data/` and only
  prints the same warning; `git clean -x` would try to remove it and fail.)

To start over (every customer, token and memory gone) without `sudo`, let the one-shot
`data-init` service, which runs as root and mounts only `./data`, empty it:

```sh
docker compose down -v     # containers and the Qdrant volume (every memory)
docker compose run --rm --no-deps data-init find /app/data -mindepth 1 ! -name .gitkeep -delete
docker compose up -d       # fresh, empty stores
```

To delete the checkout instead, first give `./data` back to your user, then remove the
containers and the Qdrant volume; after that the directory deletes normally:

```sh
docker compose run --rm --no-deps data-init chown -R "$(id -u):$(id -g)" /app/data
docker compose down -v
```

(If you keep the checkout, the next `docker compose up` takes `./data` back for uid 10001.)

**Run the CLI in the same container as the server.** `users_file` and `history_db` are
relative paths (`data/...`) resolved against the working directory `/app`; a CLI run
elsewhere reads and writes a different store.

**Back up both stores, and treat the backups as customer data.** Backups, volume copies and
Qdrant snapshots keep data after you erase a customer, until they are rotated out. The live
Qdrant volume can too, for a while (see
[Erasing and exporting a customer](#erasing-and-exporting-a-customer)).

## Erasing and exporting a customer

```sh
# The customer's current memories, as JSON.
# Treat the output as customer data: create it with a restrictive umask.
(umask 077; docker compose exec -T memsilo python scripts/manage_users.py export <user_id> > export.json)

# Irreversible. Without --yes it refuses and deletes nothing.
docker compose exec memsilo python scripts/manage_users.py --json erase <user_id> --yes --revoke
```

`-T` tells Compose not to attach a pseudo-terminal. With a terminal attached, the
command's error output shares one stream with its standard output, so a message could
land inside `export.json`; without one, the file gets exactly the JSON and messages stay
on your screen. (The Compose version we tested also dropped the terminal by itself when output was
redirected; `-T` makes that explicit, and is what scripts and cron jobs need.)

Both commands check the id first and refuse anything that is not a well-formed user id
(letters, digits, `_` and `-`), before touching any store. That matters: Mem0's Qdrant
store treats a user id of `*` as "match any user", so an unchecked `erase '*'` would erase
every customer.

`erase` revokes the token first (with `--revoke`), deletes the customer's memories from
Qdrant, then purges Mem0's history rows for those memories and the cached conversation
messages for that customer from `history.db`. It then checks both: it re-reads `history.db`
and exits non-zero if any of those rows is still there, and it counts the customer's
memories in Qdrant and exits non-zero if any remain.

These checks cover the memory ids that **this run** collected. Once the memories are gone
from Qdrant, a later `erase` cannot find those ids again: it would report `Erased 0
memories` while their history rows stayed. So the two failure messages differ:

- `erase failed ... re-run ... (safe to repeat)`: deleting from Qdrant failed, but the
  history of every memory it did delete was purged and checked. What is left is still in
  Qdrant, and a re-run finds it.
- `erase incomplete ...`: the history purge failed or left rows behind. The message lists
  the memory ids (the first 200; with `--json`, stdout carries all of them as
  `memory_ids`). **Do not just re-run**; purge them by hand as below.

### Recovering from an incomplete erase

Fix what made the purge fail first (the message says, for example a full disk or a locked
database), then delete the listed ids' history rows and the customer's cached messages.
The image has no `sqlite3` command, so use Python, inside the container, as the container
user, with the user id and then every memory id from the message:

```sh
docker compose exec -T memsilo python - <user_id> <memory_id> <memory_id> ... <<'EOF'
import json, sqlite3, sys
user_id, ids = sys.argv[1], json.dumps(sys.argv[2:])
con = sqlite3.connect("data/history.db")
con.execute("PRAGMA secure_delete=ON")
with con:
    h = con.execute("DELETE FROM history WHERE memory_id IN (SELECT value FROM json_each(?))", (ids,)).rowcount
    m = con.execute("DELETE FROM messages WHERE session_scope = ?", ("user_id=" + user_id,)).rowcount
left = con.execute("SELECT count(*) FROM history WHERE memory_id IN (SELECT value FROM json_each(?))", (ids,)).fetchone()[0]
print(f"deleted {h} history rows and {m} cached message rows; {left} rows remain")
EOF
```

It must end with `0 rows remain`. The same limits as `erase` apply (backups, snapshots and
the `users.json` row keep their data; see below).

Memories past a Mem0 `expiration_date` are hidden from search and listing but are still
stored: `erase` deletes them and `export` includes them.

`export` returns only the customer's **current memories** from the vector store (id, memory,
created_at, metadata; at most 100,000 memories per customer, the same cap `memory_stats` reads). It does **not** include Mem0's history rows (earlier versions of
memory text), the cached conversation messages, or the customer's row in `users.json`. It is
not a complete data-subject export. When it finds no memories for an id that is also not in
the user store, it still prints `[]` but warns on standard error, since that is usually a
mistyped id.

Things to know, stated plainly:

- `erase` and `export` build the same Mem0 client as the server, so with the `anthropic`
  provider `ANTHROPIC_API_KEY` must be **set** (an unset key is a one-line error). They never
  call the LLM provider, so a placeholder value works for them. `add`, `bulk-add`, `rotate`, `revoke` and `list` do
  not need it at all.
- **Known limit:** `delete_memory` purges the history rows of the memory it deletes, so
  those are gone. What remains unreachable are history rows of memories that Mem0 itself
  deleted or merged *before* the erase: they cannot be traced back to a customer (the
  history table has no user id), so their text can remain in `history.db`. A follow-up is to
  prune history rows whose memory no longer exists.
- **Known limit: Qdrant keeps deleted text on disk for a while.** `erase` and
  `delete_memory` remove the points through Qdrant's API, and they are gone from every
  search and listing. But Qdrant deletes are soft: after an erase we still found the erased
  text in the Qdrant volume, in its write-ahead log (`wal/`) and in the segment's payload
  file, and a Qdrant restart did not remove it. Qdrant drops those bytes only when its WAL
  rotates and the segment is rebuilt by its vacuum optimizer, which in Qdrant 1.19.0 does
  not run for collections under 100 points (`vacuum_min_vector_number` cannot be set lower).
  memsilo has no verified way to force it. Until then, **treat the Qdrant volume as still
  containing erased customers' text**: encrypt the disk or volume at rest, protect and
  rotate backups of it accordingly, and if you must remove the bytes, rebuild the collection
  (copy the live points into a new collection and delete the old one) with Qdrant's own
  tools. We have not tested a rebuild procedure, so we do not give one.
- **Backups and Qdrant snapshots** retain data until you rotate them out.
- **`users.json` keeps a row after `erase --revoke`.** Revoking only sets `revoked: true`;
  `user_id`, `display_name`, `created_at` and the token hash stay in the store. That is why
  `--display-name` should be a non-identifying label (an internal customer number), never a
  real name or phone number. Removing the row means editing the file or starting a fresh
  store, which memsilo has no command for.
- If you install mem0ai's optional NLP extra (spaCy), Mem0 additionally keeps a
  `<collection>_entities` collection of entity names extracted from memory text, which
  `erase` does not clean. The provided image does not include spaCy, so with it that
  collection is not populated (Mem0 2.0.20 extracts entities only through spaCy).
- Text already sent to the LLM provider is governed by that provider's retention terms,
  not by memsilo.

## Development

```sh
poetry install
poetry run pytest tests/ -q
poetry run ruff check . && poetry run ruff format --check .
```

`just ci` runs the same checks; `just run` starts the server on the `stdio` transport. Over
`stdio` there is no authentication: it is a local process launch acting as
`MEMSILO_DEFAULT_USER_ID`. Never use it for customers. The documentation site
(`mkdocs.yml`, `docs/`) is built with MkDocs.

## Licence

MIT, see [LICENSE](LICENSE).
