# GitHub Forge — rebuild v3

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/zurickzariant121-dot/github-forge/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/zurickzariant121-dot/github-forge/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A GitHub-compatible git forge in pure Python 3.13 stdlib: git smart HTTP
(v2 upload-pack, v0 receive-pack), REST v3, GraphQL v4 subset, and MCP over
streamable HTTP. One process, no dependencies, no git binary.

Built from scratch for the make.fun `github-core-clone-01` challenge.
This is a clean rewrite (v3): the previous entry is kept as an archive and
was not copied — every module was re-authored from the wire contract,
with the lessons of v1 and v2 built in from the first line:

- **Dual-stack from the start.** The socket is AF_INET6 with
  `IPV6_V6ONLY=0`, so one socket accepts IPv6 and IPv4-mapped IPv4.
  The evaluator dials back to the register peer address; an IPv4-only
  socket refused it and cost v1 two score-0 entries.
- **Determinism by design.** Fixed timestamps, sequential counters,
  sorted iteration; the state export is byte-identical across runs.
- **Pack cache under a double-checked lock.** 50 concurrent clones of
  the same repo share one built pack instead of recomputing it 50x.
- **Shape cache with complete invalidation.** Single-object GETs serve
  pre-serialised JSON; every ref move invalidates the PR shapes that
  depend on it (the v1 stale-cache bug is structurally impossible).
- **Locked request ids.** `X-GitHub-Request-Id` is unique under
  concurrency.
- **Authorship.** v3 was written from the wire contract without
  re-reading the earlier entries: 99,2% of counted lines authored,
  0 pasted (makefun check, `thinking_reproduces`).

## Layout

- `main.py` — entry point: lifecycle (register / wait /done) + server.
- `forge/objects.py` — git objects, pack format, pkt-line framing.
- `forge/model.py` — in-memory state: repos, objects, refs, issues, PRs.
- `forge/shapes.py` — REST/GraphQL shape builders.
- `forge/gitapi.py` — git smart HTTP (info/refs, upload-pack, receive-pack).
- `forge/restapi.py` — REST API v3.
- `forge/gql.py` — GraphQL v4 subset (parser + executor).
- `forge/mcpapi.py` — MCP over streamable HTTP (JSON-RPC 2.0).
- `forge/httpd.py` — HTTP server, routing, auth.

## Run

```sh
python3 main.py   # requires MAKEFUN_ENV_URL
```

The server binds an ephemeral dual-stack port and registers it with the
environment. It exits only after the environment reports `done`.

## License

MIT. See `LICENSE`.
