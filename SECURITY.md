# Security policy

## Reporting a vulnerability

Please report security problems privately, by email, to **rodrigo@reconverge.ie**.
Do not open a public issue or pull request for a vulnerability.

Include what you found, the version or commit, and steps to reproduce it. Never
include real customer data or real tokens; synthetic examples are enough.

This is a small project maintained on a best-effort basis. There is no guaranteed
response time, but reports are read and taken seriously.

## What is in scope

memsilo is a multi-user memory server. Reports about any of these are welcome:

- one customer reading, changing or deleting another customer's memories
- authentication or session-ownership bypasses
- text surviving `erase` where the documentation says it is removed
- secrets or customer text leaking through logs, errors or network traffic

## Known limits

[docs/security.md](docs/security.md) lists what memsilo does and does not
protect, including limits that are already documented (logical rather than
physical isolation, no TLS or rate limiting, and erased text that can remain in
Qdrant's own files). Those are known, but a clearer way to close them is welcome.
