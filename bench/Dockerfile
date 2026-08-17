# Bootstraps a crustify ORCHESTRATOR. Nothing here is target-specific.
#
# Both commands run from the repo root:
#
#   docker build -t crustify bench/
#
#   docker run --rm -it --name crustify-libgit2 \
#       -e ANTHROPIC_API_KEY -e CRUSTIFY_BACKEND=claude \
#       -v "$PWD/bench/libgit2:/campaign:ro" \
#       -v crustify-work:/work \
#       crustify
#
# The image carries the harness — the toolchain from the playbook's Setup
# table, the two checkouts, and a rendered orchestrator prompt. The only
# run-time input is the campaign task. Everything else is the orchestrator's
# own work: it clones the target into /work, installs whatever libraries that
# target's build system needs, then runs Phase 1 of its playbook — authoring
# build.json, crates.json and scope-config.json, building the CodeQL
# database, scaffold, bindgen — before any wave.
#
# The container is NAMED after its campaign, one name per target: `docker
# attach crustify-libgit2` reaches a detached run, `docker exec -it
# crustify-libgit2 bash` opens a second shell beside the orchestrator to
# inspect /work while it works, and a stale name is the signal that a
# campaign is still up. `--rm` frees the name on exit.
#
# Both agent backends are installed; CRUSTIFY_BACKEND picks the one that
# drives the orchestrator (`claude`, the default, or `codex` with
# OPENAI_API_KEY in place of the key above).
#
# The session is INTERACTIVE — hence `-it` — so the approval gates in the
# prompt and in TASK.md have someone to ask. To leave a campaign running and
# detach, start it with `-d` instead and attach by name later, or run tmux
# inside the container.
#
# /campaign/TASK.md, when mounted, is appended to the rendered prompt. Without
# it the orchestrator starts from the prompt alone and asks what to port.
# /work/wrappers-results.md is seeded from the image on first run; the campaign
# fills it in place.
#
# The named volume is what survives `--rm`: the clone, the CodeQL database,
# the emitted Rust tree, the session branches and the per-agent logs are all
# under /work, and the campaign mount is read-only by design. Drop the volume
# and the run is throwaway.

FROM debian:12-slim

ARG CRUSTIFY_CLI_REF=ref
ARG FFIBOX_REF=ref
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

# 1. A C toolchain. Generic, not a target's dependency set: every target is
#    built at least twice here — once for its own baseline, once under
#    `codeql database create` — and a target's own libraries are the
#    orchestrator's problem, installed once it has read the build system.
#
#    clang is pinned to 19 by package name; bookworm's unversioned `clang`
#    metapackage is 14. The versioned packages ship only versioned binaries —
#    no /usr/bin/clang, no /usr/bin/ld.lld — so the names a build system
#    actually invokes are linked by hand. lld is versioned to match: mixing
#    lld 14 with clang 19 is the failure mode `-fuse-ld=lld` walks into.
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

# 4. CodeQL CLI. The cpp query pack is installed from crustify's own qlpack
#    once the checkout lands, below.
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

# 6. Quick Setup steps 1 and 2. Under /opt, NOT /work: /work is the
#    orchestrator's work area and the documented volume mount point, and a
#    volume there would shadow anything the image had put underneath it.
#
#    ffibox MUST be a sibling of the crustify checkout: scaffold
#    resolves it as <crustify>/../ffibox and writes that absolute path
#    into every generated Cargo.toml. cli-config.json cannot override it.
RUN git clone https://github.com/crustify-rs/crustify-cli.git  /opt/crustify-cli \
    && git clone https://github.com/crustify-rs/ffibox.git /opt/ffibox \
    && git -C /opt/crustify-cli  checkout "${CRUSTIFY_CLI_REF}" \
    && git -C /opt/ffibox checkout "${FFIBOX_REF}" \
    && uv pip install --python /opt/venv -e /opt/crustify-cli \
    && cd /opt/crustify-cli/utils/codeql && codeql pack install

# 7. Quick Setup step 4. Rendered from source at build time, never
#    hand-copied: the prompt inlines the skill index, so a stale copy is
#    exactly the drift the generator exists to avoid.
RUN /opt/crustify-cli/utils/build-orchestrator-prompt.sh /opt/ffibox \
      -o /opt/orchestrator.md

# The results table, from the one copy in the build context. Baked at /opt and
# seeded into /work by the entrypoint: /campaign is a read-only mount point,
# so a copy placed there would be both shadowed and unwritable.
COPY wrappers-results.md /opt/wrappers-results.md

# The absolute paths the orchestrator needs to author a target's
# cli-config.json, which is machine-local and cannot come from any clone.
ENV CRUSTIFY_DEP_CRUSTIFY=/opt/crustify-cli \
    CRUSTIFY_DEP_FFIBOX=/opt/ffibox \
    CRUSTIFY_BIN_CLI=/opt/venv/bin/crustify-cli \
    CRUSTIFY_BIN_ORACLE=/opt/venv/bin/crustify-oracle

# Which backend drives the ORCHESTRATOR. It is not what drives the translate
# agents a wave spawns — that is `crustify-cli --model`, chosen per wave.
ENV CRUSTIFY_BACKEND=claude

# Interactive by default, because the orchestrator prompt is built around
# approval gates: it asks what to port before Phase 1, and reports a wave's
# --dry-run plan before spending on it. Headless answers neither. Set to 1
# only for an unattended run whose task description pre-answers both.
ENV CRUSTIFY_HEADLESS=0

RUN printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' \
      'prompt=/opt/orchestrator.md' \
      'if [ -f /campaign/TASK.md ]; then' \
      '    cat /opt/orchestrator.md /campaign/TASK.md > /tmp/prompt.md' \
      '    prompt=/tmp/prompt.md' \
      'fi' \
      '# Seeded, never overwritten: a resumed run keeps the table it filled.' \
      'if [ ! -f /work/wrappers-results.md ]; then' \
      '    cp /opt/wrappers-results.md /work/wrappers-results.md' \
      'fi' \
      'text="$(cat "$prompt")"' \
      'skip=--dangerously-skip-permissions' \
      'bypass=--dangerously-bypass-approvals-and-sandbox' \
      'case "${CRUSTIFY_BACKEND}:${CRUSTIFY_HEADLESS}" in' \
      '    claude:0) exec claude "$skip" "$text" "$@" ;;' \
      '    claude:1) exec claude "$skip" -p "$text" "$@" ;;' \
      '    codex:0)  exec codex "$bypass" "$text" "$@" ;;' \
      '    codex:1)  exec codex exec "$bypass" "$text" "$@" ;;' \
      '    *) echo "bad CRUSTIFY_BACKEND/CRUSTIFY_HEADLESS:" \' \
      '            "${CRUSTIFY_BACKEND}/${CRUSTIFY_HEADLESS}" >&2; exit 2 ;;' \
      'esac' \
      > /usr/local/bin/crustify-orchestrator \
    && chmod +x /usr/local/bin/crustify-orchestrator

# The orchestrator's work area: it clones the target here. Left empty so a
# volume mounted over it shadows nothing.
WORKDIR /work
CMD ["crustify-orchestrator"]
