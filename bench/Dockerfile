# Bootstraps a crustify ORCHESTRATOR. Nothing here is target-specific.
#
# Both commands run from the repo root:
#
#   docker build -t crustify bench/
#
#   docker run --rm -it --name crustify-libgit2 \
#       -e ANTHROPIC_API_KEY -e CRUSTIFY_BACKEND=claude \
#       -e CRUSTIFY_CAMPAIGN=libgit2 \
#       -v "$(dirname "$PWD")/crustify-cli:/opt/crustify-cli" \
#       -v "$(dirname "$PWD")/crustify-oracle:/opt/crustify-oracle" \
#       -v "$(dirname "$PWD")/crustify-audit:/opt/crustify-audit" \
#       -v "$(dirname "$PWD")/ffibox:/opt/ffibox" \
#       -v crustify-work:/work \
#       crustify
#
# Source mounts and one persistent volume:
#
#   /opt/crustify-cli  Required read-write host checkout.
#   /opt/{crustify-oracle,crustify-audit,ffibox}
#                      Optional read-write host checkouts. When one is absent,
#                      the entrypoint clones its main branch into the container.
#                      Mounted trees always win.
#   /work              Named volume, the only thing that survives `--rm`:
#                      target clone, CodeQL database, emitted Rust tree,
#                      session branches, per-agent logs. Drop it and the run
#                      is throwaway. wrappers-results.md is seeded here once.
#
# THE CAMPAIGN IS NAMED: CRUSTIFY_CAMPAIGN picks a directory under the
# checkout's bench/, and its TASK.md arrives through the mount above. Edit
# that file on the host and the next run picks it up. Naming a campaign that
# does not exist exits 2 with the path it looked for. bench/ is tracked, so a
# name resolves from the mounted crustify-cli checkout.
#
# A task living outside the checkout mounts at /campaign, and wins when
# present.
#
# The campaign task is the only run-time input. Everything else is the
# orchestrator's own work: clone the target into /work, install its libraries,
# then Phase 1 of the playbook — build.json, crates.json, oracle-config.json,
# CodeQL database and compiling crate shells — before any wave.
#
# One container name per campaign: `docker attach` reaches a detached run,
# `docker exec -it <name> bash` opens a shell beside the orchestrator, and a
# stale name means a campaign is still up. `--rm` frees it.
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
        ca-certificates curl git unzip xz-utils pkg-config file \
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

# 4. CodeQL CLI. The cpp pack is installed from crustify-oracle's qlpack
#    by the entrypoint, once that checkout is resolved.
RUN curl -fsSL -o /tmp/codeql.zip \
      "https://github.com/github/codeql-cli-binaries/releases/download/${CODEQL_VERSION}/codeql-linux64.zip" \
    && unzip -q /tmp/codeql.zip -d /opt && rm /tmp/codeql.zip \
    && codeql version --format=terse

# 5. Agent backends.
RUN if [ "${INSTALL_CLAUDE}" = "1" ]; then \
        curl -fsSL https://claude.ai/install.sh | bash; \
    fi \
    && if [ "${INSTALL_CODEX}" = "1" ]; then \
        curl -fsSL https://chatgpt.com/codex/install.sh | sh; \
    fi

# 6. Quick Setup sources live under /opt. crustify-cli is a required runtime
#    mount; the entrypoint uses mounted oracle/audit/ffibox trees when present
#    and clones only the missing ones. It then installs the Python projects
#    editable and installs the oracle's CodeQL pack before rendering the prompt.

# The results table. Baked at /opt, seeded into /work by the entrypoint,
# filled in place by the campaign.
COPY wrappers-results.md /opt/wrappers-results.md

# Absolute paths for a target's cli-config.json, which is machine-local and
# cannot come from any clone. The orchestrator playbook's "Bootstrap
# crustify/" reads these by name: set here, copied verbatim into `deps` and
# `bins`. CRUSTIFY_DEP_CRUSTIFY doubles as the "this environment is already
# provisioned" flag that tells it not to re-clone the checkouts above.
ENV CRUSTIFY_DEP_CRUSTIFY=/opt/crustify-cli \
    CRUSTIFY_DEP_FFIBOX=/opt/ffibox \
    CRUSTIFY_DEP_ORACLE=/opt/crustify-oracle \
    CRUSTIFY_DEP_AUDIT=/opt/crustify-audit \
    CRUSTIFY_BIN_CLI=/opt/venv/bin/crustify-cli \
    CRUSTIFY_BIN_ORACLE=/opt/venv/bin/crustify-oracle \
    CRUSTIFY_BIN_AUDIT=/opt/venv/bin/crustify-audit

# Drives the ORCHESTRATOR only; a wave's translate agents come from
# `crustify-cli --model`, chosen per wave.
ENV CRUSTIFY_BACKEND=claude

# Interactive by default: the prompt asks what to port before Phase 1 and
# reports a wave's --dry-run plan before spending on it. Headless answers
# neither, so set 1 only when the task description pre-answers both.
ENV CRUSTIFY_HEADLESS=0

# Which campaign under the checkout's bench/ to run. Empty means none: the
# orchestrator starts on the prompt alone and asks what to port.
ENV CRUSTIFY_CAMPAIGN=

# Reasoning effort for a codex orchestrator. codex infers none and falls back
# to low, which suits neither planning a wave nor reading a build system. The
# translate agents a wave spawns get theirs from crustify's own per-model
# table; this is the orchestrator's. Empty leaves codex its default. Ignored
# by the claude backend.
ENV CRUSTIFY_EFFORT=high

# How the ORCHESTRATOR authenticates, the container's echo of crustify-cli's
# `--billing`. Neither CLI switches to API-key auth on the key alone -- claude
# keeps sending its stored OAuth token unless `--bare`, and codex's built-in
# openai provider reads auth.json and 401s on OPENAI_API_KEY unless the
# provider is redeclared with an env_key -- so a key in the environment does
# nothing until this says to use it. `api` (the default, since a fresh image
# holds no login) or `subscription` for a run with credentials mounted in. A
# wave's translate agents get theirs from the TASK.md config, through
# `crustify-cli --billing`.
ENV CRUSTIFY_BILLING=api

RUN printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' \
      '# crustify-cli supplies this entrypoint and campaign task, so it remains' \
      '# a required host mount. The three capability checkouts are optional.' \
      '[ -f /opt/crustify-cli/pyproject.toml ] || {' \
      '    echo "missing checkout mount: /opt/crustify-cli" >&2; exit 2; }' \
      'ensure_checkout() {' \
      '    path="$1"; marker="$2"; url="$3"' \
      '    if [ ! -f "$path/$marker" ]; then' \
      '        echo "no mounted checkout at $path; cloning $url" >&2' \
      '        git clone --depth 1 "$url" "$path"' \
      '    fi' \
      '    [ -f "$path/$marker" ] || {' \
      '        echo "invalid checkout at $path: missing $marker" >&2; exit 2; }' \
      '}' \
      'ensure_checkout /opt/crustify-oracle pyproject.toml https://github.com/crustify-rs/crustify-oracle.git' \
      'ensure_checkout /opt/crustify-audit pyproject.toml https://github.com/crustify-rs/crustify-audit.git' \
      'ensure_checkout /opt/ffibox Cargo.toml https://github.com/crustify-rs/ffibox.git' \
      'uv pip install --python /opt/venv -e /opt/crustify-cli' \
      'uv pip install --python /opt/venv -e /opt/crustify-oracle' \
      'uv pip install --python /opt/venv -e /opt/crustify-audit' \
      '(cd /opt/crustify-oracle && codeql pack install)' \
      '# /opt/crustify-cli is a mount point, so only a prompt generated now' \
      '# matches the checkout driving this run: the render inlines the skill' \
      '# index and resolves paths into that tree. Output goes to /tmp, outside' \
      '# every checkout.' \
      'crustify-orchestrator-prompt /opt/crustify-oracle /opt/crustify-audit -o /tmp/orchestrator.md' \
      'prompt=/tmp/orchestrator.md' \
      '# A mounted /campaign wins; otherwise CRUSTIFY_CAMPAIGN names one' \
      '# under the checkout. A named campaign that is absent is fatal, so a' \
      '# typo costs one exit rather than a whole run on a task-less prompt.' \
      'task=' \
      'if [ -f /campaign/TASK.md ]; then' \
      '    task=/campaign/TASK.md' \
      'elif [ -n "${CRUSTIFY_CAMPAIGN:-}" ]; then' \
      '    task=/opt/crustify-cli/bench/${CRUSTIFY_CAMPAIGN}/TASK.md' \
      '    [ -f "$task" ] || { echo "no campaign at $task" >&2; exit 2; }' \
      'fi' \
      'if [ -n "$task" ]; then' \
      '    cat /tmp/orchestrator.md "$task" > /tmp/prompt.md' \
      '    prompt=/tmp/prompt.md' \
      'fi' \
      '# Seeded, never overwritten: a resumed run keeps the table it filled.' \
      'if [ ! -f /work/wrappers-results.md ]; then' \
      '    cp /opt/wrappers-results.md /work/wrappers-results.md' \
      'fi' \
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

# The orchestrator clones the target here. Left empty so a volume mounted
# over it shadows nothing.
WORKDIR /work
CMD ["crustify-orchestrator"]
