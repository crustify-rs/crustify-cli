# Bootstraps a crustify ORCHESTRATOR. Nothing here is target-specific.
#
# Both commands run from the repo root:
#
#   docker build -f examples/crustify/Dockerfile -t crustify .
#
#   docker run -it --name crustify-libgit2 \
#       -e ANTHROPIC_API_KEY -e CRUSTIFY_BACKEND=claude \
#       -v "$(dirname "$PWD")/crustify:/opt/crustify" \
#       -v "$(dirname "$PWD")/wavefront:/opt/wavefront" \
#       -v "$(dirname "$PWD")/ffibox:/opt/ffibox" \
#       -v /absolute/path/to/your/target-fork:/target \
#       -v "$PWD/examples/crustify/libgit2/src-port/TASK.md:/campaign/TASK.md:ro" \
#       crustify
#
# Source mounts and the target checkout:
#
#   /opt/crustify  Required read-write host checkout.
#   /opt/{wavefront,ffibox}
#                      Optional read-write host checkouts. When one is absent,
#                      the entrypoint clones its main branch into the container.
#                      Mounted trees always win.
#   /target            Required read-write bind mount of the existing target
#                      checkout. Its current revision, branches, `crustify/`
#                      directory, CodeQL data, partial translation, and logs are
#                      used in place and remain on the host.
#
# THE CAMPAIGN TASK IS MOUNTED: bind its TASK.md at /campaign/TASK.md read-only,
# just like the crustify-audit image. Edit that file on the host and the next
# run picks it up. Without the mount, the orchestrator asks for unresolved
# campaign details interactively.
#
# The results template rides the same mount: the orchestrator reads
# examples/crustify/results-template.md from the checkout and writes the filled
# table to the path the campaign task names. Nothing is baked into the image or
# seeded into /target, so editing the template on the host changes the next run.
#
# A task mounts directly at /campaign/TASK.md.
#
# The campaign task and mounted target are the run-time inputs. The
# orchestrator works directly in /target: it assesses existing campaign state,
# installs target libraries as needed, and resumes at the first incomplete
# playbook stage. It must not clone, reset, or replace the mounted checkout.
#
# One container name per campaign: `docker attach` reaches a detached run,
# `docker exec -it <name> bash` opens a shell beside the orchestrator, and
# `docker start -ai <name>` resumes a stopped run with all post-boot installs
# intact in the container's writable root filesystem. Removing the container
# removes that state; bake generally useful packages into the image.
#
# `-it` because the prompt is built around approval gates. Detach with `-d`
# and attach later, or run tmux inside. Both backends ship; CRUSTIFY_BACKEND
# picks the one driving the orchestrator (`claude`, or `codex` with
# OPENAI_API_KEY in place of the key above).

FROM debian:12-slim

ARG CODEQL_VERSION=v2.26.3
ARG PYTHON_VERSION=3.13
# Both backends by default. The playbook says to ask which are wanted, so
# either can be dropped from the image; crustify drives them interchangeably.
ARG INSTALL_CLAUDE=1
ARG INSTALL_CODEX=1

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:/root/.cargo/bin:/root/.local/bin:/opt/codeql:$PATH \
    CARGO_TERM_COLOR=never \
    # bindgen dlopens libclang at run time, and the versioned package ships it
    # as libclang-19.so.1 rather than the libclang.so.1 clang-sys looks for.
    LIBCLANG_PATH=/usr/lib/llvm-19/lib \
    # Claude Code refuses --dangerously-skip-permissions as root without this.
    IS_SANDBOX=1

# 1. A generic C toolchain — a target's own libraries are the orchestrator's
#    problem, installed once it has read the build system.
#
#    clang is pinned to 19 by package name (bookworm's unversioned metapackage
#    is 14), and lld matches: lld 14 against clang 19 is what `-fuse-ld=lld`
#    trips on. Versioned packages ship only versioned binaries, so the names a
#    build system actually invokes are linked by hand.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git unzip xz-utils pkg-config file jq ripgrep \
        build-essential clang-19 lld-19 cmake ninja-build python3 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s clang-19   /usr/bin/clang \
    && ln -s clang++-19 /usr/bin/clang++ \
    && ln -s ld.lld-19  /usr/bin/ld.lld \
    && ln -s lld-19     /usr/bin/lld

# 2. Python. crustify needs >= 3.13; bookworm ships 3.11, so uv supplies it.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv python install "${PYTHON_VERSION}" \
    && uv venv --python "${PYTHON_VERSION}" /opt/venv

# 3. Rust: stable + clippy for the emitted crates, nightly for the audit's
#    rustc driver, bindgen-cli for the -sys crates.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --default-toolchain stable --component clippy \
    && rustup toolchain install nightly --component rustc-dev --component llvm-tools \
    && cargo install bindgen-cli --locked

# 4. CodeQL CLI. GitHub publishes this bundle for x86-64 Linux, but not native
#    Linux ARM64. ARM campaigns can still consume T1/T2 tables extracted on an
#    x86-64 machine; the entrypoint verifies those tables before starting. The
#    cpp pack is installed from wavefront's qlpack on hosts that have CodeQL.
RUN arch="$(dpkg --print-architecture)"; \
    if [ "$arch" = amd64 ]; then \
        curl -fsSL -o /tmp/codeql.zip \
          "https://github.com/github/codeql-cli-binaries/releases/download/${CODEQL_VERSION}/codeql-linux64.zip" \
        && unzip -q /tmp/codeql.zip -d /opt \
        && rm /tmp/codeql.zip \
        && codeql version --format=terse; \
    else \
        echo "CodeQL is unavailable natively on $arch; pre-extracted T1/T2 tables are required"; \
    fi

# 5. Agent backends.
RUN if [ "${INSTALL_CLAUDE}" = "1" ]; then \
        curl -fsSL https://claude.ai/install.sh | bash; \
    fi \
    && if [ "${INSTALL_CODEX}" = "1" ]; then \
        curl -fsSL https://chatgpt.com/codex/install.sh | sh; \
    fi

# 6. Quick Setup sources live under /opt. crustify is a required runtime
#    mount and includes crustify-audit; the entrypoint uses mounted
#    wavefront/ffibox trees when present and clones only the missing ones. It
#    then installs the Python projects editable and installs Wavefront's CodeQL
#    pack before rendering the prompt.

# Absolute paths for a target's cli-config.json, which is machine-local and
# cannot come from any clone. The orchestrator playbook's "Bootstrap
# crustify/" reads these by name: set here, copied verbatim into `deps` and
# `bins`. CRUSTIFY_DEP_CRUSTIFY doubles as the "this environment is already
# provisioned" flag that tells it not to re-clone the checkouts above.
ENV CRUSTIFY_DEP_CRUSTIFY=/opt/crustify \
    CRUSTIFY_DEP_FFIBOX=/opt/ffibox \
    CRUSTIFY_DEP_WAVEFRONT=/opt/wavefront \
    CRUSTIFY_DEP_AUDIT=/opt/crustify \
    CRUSTIFY_BIN_CLI=/opt/venv/bin/crustify \
    CRUSTIFY_BIN_WAVEFRONT=/opt/venv/bin/wavefront \
    CRUSTIFY_BIN_AUDIT=/opt/venv/bin/crustify-audit

# Drives the ORCHESTRATOR only; a wave's translate agents come from
# `crustify --model`, chosen per wave.
ENV CRUSTIFY_BACKEND=claude

# Interactive by default: the prompt asks what to port before Phase 1 and
# reports a wave's --dry-run plan before spending on it. Headless answers
# neither, so set 1 only when the task description pre-answers both.
ENV CRUSTIFY_HEADLESS=0

# Reasoning effort for a codex orchestrator. codex infers none and falls back
# to low, which suits neither planning a wave nor reading a build system. The
# translate agents a wave spawns get theirs from crustify's own per-model
# table; this is the orchestrator's. Empty leaves codex its default. Ignored
# by the claude backend.
ENV CRUSTIFY_EFFORT=high

# How the ORCHESTRATOR authenticates, the container's echo of crustify's
# `--billing`. Neither CLI switches to API-key auth on the key alone -- claude
# keeps sending its stored OAuth token unless `--bare`, and codex's built-in
# openai provider reads auth.json and 401s on OPENAI_API_KEY unless the
# provider is redeclared with an env_key -- so a key in the environment does
# nothing until this says to use it. `api` (the default, since a fresh image
# holds no login) or `subscription` for a run with credentials mounted in. A
# wave's translate agents get theirs from the TASK.md config, through
# `crustify --billing`.
ENV CRUSTIFY_BILLING=api

RUN printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' \
      '# crustify supplies this entrypoint and campaign task, so it remains' \
      '# a required host mount. The other two capability checkouts are optional.' \
      '[ -f /opt/crustify/pyproject.toml ] || {' \
      '    echo "missing checkout mount: /opt/crustify" >&2; exit 2; }' \
      '[ -e /target/.git ] || {' \
      '    echo "missing target checkout mount: bind an existing Git checkout at /target" >&2; exit 2; }' \
      'git config --global --add safe.directory /target' \
      'ensure_checkout() {' \
      '    path="$1"; marker="$2"; url="$3"' \
      '    if [ ! -f "$path/$marker" ]; then' \
      '        echo "no mounted checkout at $path; cloning $url" >&2' \
      '        git clone --depth 1 "$url" "$path"' \
      '    fi' \
      '    [ -f "$path/$marker" ] || {' \
      '        echo "invalid checkout at $path: missing $marker" >&2; exit 2; }' \
      '}' \
      'ensure_checkout /opt/wavefront pyproject.toml https://github.com/crustify-rs/wavefront.git' \
      'ensure_checkout /opt/ffibox Cargo.toml https://github.com/crustify-rs/ffibox.git' \
      'uv pip install --python /opt/venv -e /opt/crustify' \
      'uv pip install --python /opt/venv -e /opt/wavefront' \
      'if command -v codeql >/dev/null 2>&1; then' \
      '    qlpack=/opt/wavefront' \
      '    [ ! -f /opt/wavefront/src/qlpack.yml ] || qlpack=/opt/wavefront/src' \
      '    (cd "$qlpack" && codeql pack install)' \
      'else' \
      '    t1=/target/crustify/wavefront/codeql/t1/functions.csv' \
      '    t2=/target/crustify/wavefront/codeql/t2/function_calls.csv' \
      '    [ -s "$t1" ] && [ -s "$t2" ] || {' \
      '        echo "CodeQL is unavailable and the target has no reusable T1/T2 tables" >&2' \
      '        echo "expected non-empty $t1 and $t2" >&2' \
      '        exit 2; }' \
      '    echo "CodeQL unavailable; using pre-extracted target T1/T2 tables" >&2' \
      'fi' \
      '# Append a task only when the conventional external mount is present.' \
      'task=' \
      'if [ -f /campaign/TASK.md ]; then' \
      '    task=/campaign/TASK.md' \
      'fi' \
      '# /opt/crustify is a mount point, so only a prompt generated now' \
      '# matches the checkout driving this run. Passing the optional task to' \
      '# the renderer keeps container and direct CLI usage identical.' \
      'if [ -n "$task" ]; then' \
      '    crustify-orchestrator-prompt /opt/wavefront /opt/crustify/src/crustify_audit "$task" -o /tmp/orchestrator.md' \
      'else' \
      '    crustify-orchestrator-prompt /opt/wavefront /opt/crustify/src/crustify_audit -o /tmp/orchestrator.md' \
      'fi' \
      'prompt=/tmp/orchestrator.md' \
      'text="$(cat "$prompt")"' \
      'skip=--dangerously-skip-permissions' \
      'bypass=--dangerously-bypass-approvals-and-sandbox' \
      '# codex accepts any effort string locally and only rejects it at the' \
      '# API, so the whitelist is enforced here rather than a run in.' \
      'effort=()' \
      'case "${CRUSTIFY_EFFORT:-}" in' \
      '    "") ;;' \
      '    low|medium|high|xhigh|max|ultra)' \
      '        effort=(-c "model_reasoning_effort=\"${CRUSTIFY_EFFORT}\"") ;;' \
      '    *) echo "bad CRUSTIFY_EFFORT: ${CRUSTIFY_EFFORT}" >&2; exit 2 ;;' \
      'esac' \
      '# Checked here rather than left to the CLI: claude without a key it' \
      '# can use issues no request and exits 0 having done nothing, which' \
      '# reads as a campaign that ran and produced nothing.' \
      'auth=()' \
      'case "${CRUSTIFY_BILLING:-}" in' \
      '    subscription) ;;' \
      '    api|"")' \
      '        case "${CRUSTIFY_BACKEND}" in' \
      '        claude)' \
      '            [ -n "${ANTHROPIC_API_KEY:-}" ] || {' \
      '                echo "CRUSTIFY_BILLING=api needs ANTHROPIC_API_KEY" >&2' \
      '                exit 2; }' \
      '            auth=(--bare) ;;' \
      '        codex)' \
      '            [ -n "${OPENAI_API_KEY:-}" ] || {' \
      '                echo "CRUSTIFY_BILLING=api needs OPENAI_API_KEY" >&2' \
      '                exit 2; }' \
      '            auth=(-c "model_provider=openai_apikey"' \
      '                  -c "model_providers.openai_apikey.name=\"OpenAI\""' \
      '                  -c "model_providers.openai_apikey.base_url=\"https://api.openai.com/v1\""' \
      '                  -c "model_providers.openai_apikey.env_key=\"OPENAI_API_KEY\""' \
      '                  -c "model_providers.openai_apikey.wire_api=\"responses\"") ;;' \
      '        esac ;;' \
      '    *) echo "bad CRUSTIFY_BILLING: ${CRUSTIFY_BILLING}" >&2; exit 2 ;;' \
      'esac' \
      'case "${CRUSTIFY_BACKEND}:${CRUSTIFY_HEADLESS}" in' \
      '    claude:0) exec claude "$skip" "${auth[@]}" "$text" "$@" ;;' \
      '    claude:1) exec claude "$skip" "${auth[@]}" -p "$text" "$@" ;;' \
      '    codex:0)  exec codex "$bypass" "${auth[@]}" "${effort[@]}" "$text" "$@" ;;' \
      '    codex:1)  exec codex exec "$bypass" "${auth[@]}" "${effort[@]}" "$text" "$@" ;;' \
      '    *) echo "bad CRUSTIFY_BACKEND/CRUSTIFY_HEADLESS:" \' \
      '            "${CRUSTIFY_BACKEND}/${CRUSTIFY_HEADLESS}" >&2; exit 2 ;;' \
      'esac' \
      > /usr/local/bin/crustify-orchestrator \
    && chmod +x /usr/local/bin/crustify-orchestrator

# The required target bind mount shadows this empty image directory. Starting
# in it makes the existing checkout—and any partial crustify campaign—the
# orchestrator's unambiguous working repository.
WORKDIR /target
CMD ["crustify-orchestrator"]
