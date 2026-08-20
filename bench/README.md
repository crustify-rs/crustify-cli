# Bench

One target per directory. Each holds the `TASK.md` a run is given, plus
whatever Phase 1 artifacts are authored ahead of time.

`Dockerfile` bootstraps the orchestrator that runs one.

Both commands from the repo root:

```sh
docker build -t crustify bench/

docker run --rm -it --name crustify-libgit2 \
    -e ANTHROPIC_API_KEY -e CRUSTIFY_BACKEND=claude \
    -e CRUSTIFY_CAMPAIGN=libgit2 \
    -v "$PWD:/opt/crustify-cli" \
    -v crustify-work:/work \
    crustify
```

## Run-time environment

Set with `-e` on `docker run`.

| var | values | default | effect |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | key | — | required for `CRUSTIFY_BACKEND=claude` |
| `OPENAI_API_KEY` | key | — | required for `CRUSTIFY_BACKEND=codex` |
| `CRUSTIFY_BACKEND` | `claude`, `codex` | `claude` | orchestrator only; wave agents come from `crustify-cli --model` |
| `CRUSTIFY_CAMPAIGN` | directory name under `bench/` | empty | empty → orchestrator asks what to port; name that does not resolve → exit 2; a mounted `/campaign/TASK.md` wins over it |
| `CRUSTIFY_HEADLESS` | `0`, `1` | `0` | `1` answers no approval gate — use only where `TASK.md` pre-answers them |
| `CRUSTIFY_EFFORT` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra`, empty | `high` | codex orchestrator only, ignored by claude; empty leaves codex its default; anything else → exit 2 |

## Build environment

Set with `--build-arg` on `docker build`.

| arg | default | effect |
|---|---|---|
| `CRUSTIFY_CLI_REF` | `main` | ref checked out in the image's crustify-cli clone |
| `FFIBOX_REF` | `main` | ref checked out at `/opt/ffibox` |
| `CODEQL_VERSION` | `v2.26.3` | CodeQL CLI release |
| `PYTHON_VERSION` | `3.13` | interpreter for `/opt/venv` |
| `INSTALL_CLAUDE` | `1` | install the claude backend |
| `INSTALL_CODEX` | `1` | install the codex backend |

## Mounts

| path | mode | lifetime |
|---|---|---|
| `/opt/crustify-cli` | read-write | host checkout; agents commit to it, so give them a reviewable branch |
| `/work` | named volume | target clone, CodeQL database, emitted crates, session branches; survives `--rm` |
| `/campaign` | read-only, optional | a `TASK.md` outside the checkout; wins over `CRUSTIFY_CAMPAIGN` |

Results are tracked in `wrappers-results.md`. The Dockerfile header documents
what each flag buys.
