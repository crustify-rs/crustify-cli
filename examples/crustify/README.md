# Examples

One campaign example per directory. Each holds the `TASK.md` a run is given,
plus whatever Phase 1 inputs are authored ahead of time. Derived artifacts such
as `subsystems.json` are emitted against the live oracle inventory.

Copy [`TASK-template.md`](TASK-template.md) to start a campaign request. It is the canonical
user-facing template; campaign directories contain filled instances. Campaign
reports use [`results-template.md`](results-template.md).

`Dockerfile` bootstraps the orchestrator that runs one.

Both commands from the repo root:

```sh
docker build -f examples/crustify/Dockerfile -t crustify .

docker run -it --name crustify-libgit2 \
    -e ANTHROPIC_API_KEY -e CRUSTIFY_BACKEND=claude \
    -v "$(dirname "$PWD")/crustify:/opt/crustify" \
    -v "$(dirname "$PWD")/wavefront:/opt/wavefront" \
    -v "$(dirname "$PWD")/ffibox:/opt/ffibox" \
    -v /absolute/path/to/your/target-fork:/target \
    -v "$PWD/examples/crustify/libgit2/src-port/TASK.md:/campaign/TASK.md:ro" \
    crustify
```

`/target` must be an existing Git checkout mounted read-write. The orchestrator
uses its checked-out revision and existing `crustify/` state directly, making
this suitable for resuming a partial translation on a personal fork. It does
not clone or replace the target checkout.

The example deliberately omits `--rm`. Restart its stopped container with
`docker start -ai crustify-libgit2`; its writable root filesystem preserves all
tools, caches, source-built dependencies, and `apt` packages installed after
boot. Removing the container loses those changes, so generally useful packages
should be added to the Dockerfile.

## Run-time environment

Set with `-e` on `docker run`.

| var | values | default | effect |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | key | — | required for `CRUSTIFY_BACKEND=claude` |
| `OPENAI_API_KEY` | key | — | required for `CRUSTIFY_BACKEND=codex` |
| `CRUSTIFY_BACKEND` | `claude`, `codex` | `claude` | orchestrator only; wave agents come from `crustify --model` |
| `CRUSTIFY_BILLING` | `api`, `subscription` | `api` | orchestrator only; `api` adds `--bare` (claude) or an env-key provider block (codex) — neither CLI uses the key in the environment without it; key missing → exit 2 |
| `CRUSTIFY_HEADLESS` | `0`, `1` | `0` | `1` answers no approval gate — use only where `TASK.md` pre-answers them |
| `CRUSTIFY_EFFORT` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra`, empty | `high` | codex orchestrator only, ignored by claude; empty leaves codex its default; anything else → exit 2 |

## Build environment

Set with `--build-arg` on `docker build`.

| arg | default | effect |
|---|---|---|
| `CODEQL_VERSION` | `v2.26.3` | CodeQL CLI release |
| `PYTHON_VERSION` | `3.13` | interpreter for `/opt/venv` |
| `INSTALL_CLAUDE` | `1` | install the claude backend |
| `INSTALL_CODEX` | `1` | install the codex backend |

## Mounts

| path | mode | lifetime |
|---|---|---|
| `/opt/crustify` | read-write | host checkout; agents commit to it, so give them a reviewable branch |
| `/opt/wavefront` | read-write, optional | mounted checkout wins; otherwise cloned from GitHub, then installed editable |
| `/opt/ffibox` | read-write, optional | mounted checkout wins; otherwise cloned from GitHub and used by generated Cargo manifests |
| `/target` | read-write, required | existing target checkout; its partial translation, CodeQL data, branches, and logs remain on the host |
| `/campaign/TASK.md` | read-only, optional | pre-filled campaign task; without it the orchestrator asks for unresolved details interactively |

Results are tracked in `results.md`. The Dockerfile header documents
what each flag buys. The two optional source mounts may be omitted from
`docker run`; their `main` branches are then cloned into the campaign container.

Historical aggregate measurements for the libgit2 and OpenSSL campaigns are
preserved in [`libgit2-openssl-results.md`](libgit2-openssl-results.md).
