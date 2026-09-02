# Bootstraps a crustify-audit ORCHESTRATOR. Nothing here is target-specific.
#
# The container starts an agent that resolves the audit plan and then spawns
# `crustify-audit ... ub` itself. Run it INTERACTIVELY (`-it`) to answer any
# mandatory questions not already filled in TASK.md. A headless run needs all
# mandatory TASK answers.
#
#   docker build -t crustify-audit -f examples/crustify_audit/Dockerfile .
#
#   docker run --rm -it --name audit-ippcp \
#       -e ANTHROPIC_API_KEY \
#       -v /path/to/target-repo:/target \
#       -v /host/campaign/TASK.md:/campaign/TASK.md:ro \
#       -v audit-ippcp-work:/work \
#       crustify-audit
#
#   /target              The repository to audit, read-write, on whatever branch
#                        the host has checked out — there is no ref to pass and
#                        no clone. The agent's `crustify/audit/` artifacts land
#                        in that working tree, and it runs with permissions
#                        bypassed, so point it at a checkout you can review.
#                        A repository, not a bare workspace: the crate is the
#                        root or `crustify/rust`, and `ub` needs the C sources
#                        beside it to build a reproduction that links the crate.
#   /opt/crustify-audit  The harness is baked into the image. A checkout may be
#                        mounted here to test live harness changes instead.
#   /work                Named volume, and `HOME`: the C library the agent
#                        builds, the cargo registry. Not the audit artifacts —
#                        those are in the target checkout. Nothing installed at
#                        image BUILD time needs it; that is in the image layer.
#
# Target-specific dependencies are the agent's, all of them — the C library, a
# campaign tree's `ffibox`. The image carries a generic toolchain so the agent
# does not start by building one, and stops there: what a subject needs is not
# something this file can know, and pinning it dates the image to one target.

# Pinned by DIGEST, not `:latest`. This image's whole premise is that rustc,
# bsan and clang agree on one LLVM version; a moving base silently shifts
# bsan's LLVM out from under the matched clang compiler-rt below and there is
# no error, only instruments that quietly stop finding things. `:latest` moved
# once already mid-campaign. Bump this deliberately, and re-run the self-tests.
FROM ghcr.io/borrowsanitizer/bsan@sha256:69440e3b6359f3b51ae351998dc4d408777cde23d72a6adc05ed649570cfdce0

ARG PYTHON_VERSION=3.13
# Both backends by default; either can be dropped from the image.
ARG INSTALL_CLAUDE=1
ARG INSTALL_CODEX=1

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/bsan-shim:/opt/venv/bin:/root/.cargo/bin:/root/.local/bin:$PATH \
    CARGO_TERM_COLOR=never \
    # Claude Code refuses --dangerously-skip-permissions as root without this.
    IS_SANDBOX=1

# BorrowSanitizer supplies its matched Rust/Clang/LLVM toolchain. Add the
# remaining generic build tools; `nasm`/`yasm` avoid a source build before a
# crypto audit can start. libclang-dev supports ordinary bindgen builds; bsan
# selects its own matched libclang when it instruments a build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates unzip xz-utils file lld libclang-dev \
        nasm yasm autoconf automake libtool m4 python3 \
    && rm -rf /var/lib/apt/lists/*

# MSan needs a C compiler whose compiler-rt MATCHES rustc's LLVM, because it is
# the one sanitizer that tolerates no uninstrumented code: anything built
# without it leaves shadow memory poisoned and correct reads report as
# uninitialized. The base image has clang 20 while both Rust toolchains are
# LLVM 23, and the bsan toolchain's own clang-23 is a rust-lang/llvm-project
# build carrying NO compiler-rt at all -- `-fsanitize=memory` there dies on a
# missing libclang_rt.msan.a. So install a stock LLVM 23 for its runtimes.
# The /usr/bin/clang-23 symlink is deliberately removed: cargo-bsan resolves
# clang by the active rustc's LLVM version, and a second clang-23 on PATH is
# exactly how bsan ends up silently instrumenting with the wrong compiler.
# Reach this one by its absolute path only.
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key \
         -o /etc/apt/keyrings/llvm.asc \
    && echo "deb [signed-by=/etc/apt/keyrings/llvm.asc] \
https://apt.llvm.org/plucky/ llvm-toolchain-plucky-23 main" \
       > /etc/apt/sources.list.d/llvm-23.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends clang-23 libclang-rt-23-dev \
    && rm -f /usr/bin/clang-23 \
    && rm -rf /var/lib/apt/lists/*
ENV CRUSTIFY_MSAN_CC=/usr/lib/llvm-23/bin/clang

# Keep the harness Python independent of whichever Python the base image uses.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv python install "${PYTHON_VERSION}" \
    && uv venv --python "${PYTHON_VERSION}" /opt/venv

# Preserve the base image's `bsan` toolchain, and install our ordinary Rust
# toolchains alongside it. Stable remains the default so unqualified Cargo
# commands do not accidentally compile targets with the custom bsan compiler;
# BorrowSanitizer is invoked explicitly as `cargo +bsan bsan ...`.
# `rustup toolchain install nightly` would SYNC the channel: once upstream
# nightly moves past the base image's, it replaces the base's toolchain, and
# that rename crosses overlayfs layers and dies with EXDEV. It is also
# redundant -- the base ships every component below already. Add them instead,
# which is a no-op that still fails loudly if a future base drops one, and
# leaves rustc on the LLVM the bsan toolchain and clang-23 are matched to.
RUN rustup toolchain install stable --component clippy \
    && rustup component add --toolchain nightly \
         rustc-dev llvm-tools rust-src miri \
    && rustup default stable \
    && cargo +nightly miri --version

# MemorySanitizer and ThreadSanitizer are `-Zsanitizer=` builds, not separate
# toolchains, so nothing extra is installed for them -- but they are only usable
# if two things hold, and both fail late and confusingly when they do not.
# First, the target must support them: check the target spec rather than
# discovering it from a failed link at the end of a long build. Second, MSan
# needs the standard library rebuilt with instrumentation, which is `-Zbuild-std`
# over the `rust-src` component installed above; without it every std allocation
# reads as uninitialized and the run drowns in false positives.
RUN set -eu; \
    spec=$(rustc +nightly -Z unstable-options --print target-spec-json \
             --target "$(rustc -vV | sed -n 's/^host: //p')"); \
    for s in memory thread; do \
        echo "$spec" | grep -q "\"$s\"" \
          || { echo "target does not support -Zsanitizer=$s" >&2; exit 1; }; \
    done; \
    test -d "$(rustc +nightly --print sysroot)/lib/rustlib/src/rust/library/std" \
      || { echo "rust-src missing: -Zbuild-std cannot instrument std" >&2; exit 1; }

# Prove the C half of an MSan build links and REPORTS, the same way the bsan
# self-test does. A version string cannot distinguish a clang that merely exists
# from one whose compiler-rt is actually present, and that difference only
# surfaces at the final link of a long instrumented build -- or worse, as a
# campaign that records msan as "found nothing".
RUN set -eu; \
    printf '%s\n' '#include <stdlib.h>' 'int main(void) {' \
        '    int *a = malloc(sizeof(int) * 10);' \
        '    a[5] = 0;' \
        '    if (a[0]) return 1;' \
        '    return 0; }' > /tmp/msan-selftest.c; \
    "$CRUSTIFY_MSAN_CC" -fsanitize=memory -fno-omit-frame-pointer -O1 \
        /tmp/msan-selftest.c -o /tmp/msan-selftest \
      || { echo "msan: C link failed -- compiler-rt missing" >&2; exit 1; }; \
    /tmp/msan-selftest 2>&1 | grep -q "use-of-uninitialized-value" \
      || { echo "msan: built but did not report a known uninit read" >&2; exit 1; }; \
    rm -f /tmp/msan-selftest /tmp/msan-selftest.c

# `cargo bsan` (no `+bsan`) is the form anyone writes by reflex, and it fails in
# the most misleading way available. cargo-bsan picks its clang from the ACTIVE
# rustc's LLVM version, so under the default stable toolchain (LLVM 22) it hunts
# for `clang-22` -- which nobody ships -- and dies with `failed to find
# clang-22`. That reads as "BorrowSanitizer is not installed", and the version
# probe still exits 0, so nothing warns you earlier. Only the bsan toolchain
# carries the matching clang. Cargo also exports `CARGO` as its resolved stable
# binary to subcommands, so override both that path and the toolchain in a shim
# placed ahead of the rustup binary on PATH. Then both `cargo bsan` and
# `cargo +bsan bsan` work.
# The real binary is in the image layer at /root/.cargo/bin -- NOT under
# CARGO_HOME, which is redirected to the /work volume further down.
RUN mkdir -p /opt/bsan-shim \
    && printf '%s\n' '#!/usr/bin/env bash' \
         'exec env RUSTUP_TOOLCHAIN=bsan CARGO=/root/.cargo/bin/cargo /root/.cargo/bin/cargo-bsan "$@"' \
       > /opt/bsan-shim/cargo-bsan \
    && chmod +x /opt/bsan-shim/cargo-bsan

# Verify BorrowSanitizer by INVOCATION, not by version string. `cargo bsan
# --version` answers from the binary alone: it exits 0 on an image whose
# toolchain cannot instrument anything, so it cannot tell a working image from a
# broken one. Build and RUN a known Tree Borrows violation instead, and require
# the diagnostic. A silently inert bsan is worse than an absent one -- an
# auditor that cannot run it records its entire bug class as UNTESTED, and a
# whole campaign's aliasing coverage is lost without a single failed command.
RUN set -eu; \
    mkdir -p /tmp/bsan-selftest/src; \
    printf '%s\n' '[package]' 'name = "bsan_selftest"' 'version = "0.0.0"' \
        'edition = "2021"' > /tmp/bsan-selftest/Cargo.toml; \
    printf '%s\n' 'fn main() {' \
        '    let mut x = 42u8;' \
        '    let p = &mut x as *mut u8;' \
        '    let r = &mut x;' \
        '    *r = 1;' \
        '    unsafe { *p = 7; }' \
        '    println!("{}", x);' \
        '}' > /tmp/bsan-selftest/src/main.rs; \
    cd /tmp/bsan-selftest; \
    cargo bsan run > /tmp/bsan-selftest.log 2>&1 || true; \
    grep -q 'Undefined Behavior' /tmp/bsan-selftest.log \
      || { echo "BSan did not fire on a known Tree Borrows violation:" >&2; \
           cat /tmp/bsan-selftest.log >&2; exit 1; }; \
    cd /; rm -rf /tmp/bsan-selftest /tmp/bsan-selftest.log

RUN if [ "${INSTALL_CLAUDE}" = "1" ]; then \
        curl -fsSL https://claude.ai/install.sh | bash; \
    fi \
    && if [ "${INSTALL_CODEX}" = "1" ]; then \
        curl -fsSL https://chatgpt.com/codex/install.sh | sh; \
    fi

# Bake in a runnable harness -- the WHOLE repository, not just `src`. The
# audit package imports `crustify.core`, and the orchestrator is told to read
# templates out of `examples/`, so a partial copy works only until something
# reaches past it. Nothing expensive follows this layer (the sanitizer
# self-tests are far above), so a cache miss here costs seconds.
# A bind mount at this path replaces these sources,
# and PYTHONPATH intentionally makes that mounted checkout win over the wheel
# installed in the virtual environment. The metrics driver remains a run-time
# build because it must match the nightly compiler driving that run.
COPY . /opt/crustify-audit
RUN uv pip install --python /opt/venv/bin/python --no-deps /opt/crustify-audit
ENV PYTHONPATH=/opt/crustify-audit/src

# `orchestrate` starts the interviewing agent, which spawns the auditors.
# `unsafe` is the deterministic scan: no agent, no spend, useful on its own.
ENV CRUSTIFY_VERB=orchestrate

# The CLI and model used by the ORCHESTRATOR. The model string is passed to the
# selected CLI verbatim.
ENV CRUSTIFY_BACKEND=claude
ENV CRUSTIFY_MODEL=claude-opus-5

# Billing service used by that CLI. Empty selects anthropic for Claude and
# openai for Codex. Set openrouter to drive its models through the Codex CLI.
ENV CRUSTIFY_PROVIDER=

# Headless mode cannot resolve missing mandatory TASK answers interactively.
ENV CRUSTIFY_HEADLESS=0

# Ablation: 1 drops the orchestrator prompt entirely and hands the backend the
# mounted TASK.md verbatim. Everything else -- image, toolchain, sanitizers,
# target mount, auth -- stays identical, so a run measures the prompt's
# contribution and nothing else. See examples/crustify_audit/TASK-bare.md.template.
ENV CRUSTIFY_BARE=0

# Codex reasoning effort for both the orchestrator and every auditor it spawns.
# The CLI accepts any string locally and rejects unsupported values at the API.
ENV CRUSTIFY_EFFORT=high

# Not decoration: neither CLI uses an environment key on its own, so `api` adds
# `--bare` (claude) or an env-key provider block (codex) -- a key in the
# environment does nothing until this says to use it. `api` is the default
# since a fresh image holds no login; `subscription` spends the session the
# CLI finds under HOME, which is the named volume, so a `claude` or `codex`
# login done once inside the container survives every later run against it.
ENV CRUSTIFY_BILLING=api

# Wall-clock BUDGET in minutes PER AUDITOR, not a leash: agents are spawned one
# after another until it is reached and none is killed, so a run overshoots by
# however long the last one takes. 0 runs one agent. With N auditors at once,
# spend is roughly N times this -- the orchestrator is told to say that number
# out loud before you agree to it.
ENV CRUSTIFY_TIMEOUT=60

# HOME is the volume, so what the agent fetches or builds survives `--rm`.
# RUSTUP_HOME is pinned back out of it: rustup resolves from HOME, and against
# an empty volume `cargo +nightly` finds no toolchain and silently downloads a
# fresh nightly WITHOUT this image's components — losing miri unannounced.
# CARGO_HOME does belong on the volume, for the registry cache.
ENV HOME=/work \
    RUSTUP_HOME=/root/.rustup \
    CARGO_HOME=/work/.cargo
RUN mkdir -p /work /target

# /target is a bind mount owned by the HOST user while the container runs as
# root, so git rejects it for "dubious ownership" -- which breaks recording the
# audited revision and every worktree the patch objective creates. --system,
# not --global: HOME is the named volume, and a gitconfig written under it at
# build time is masked whenever that volume already exists.
RUN git config --system --add safe.directory '*'

# `crustify-audit` is the CLI the orchestrator spawns auditors with;
# `crustify-audit-orchestrator` is what the container runs, off the CRUSTIFY_*
# env.
RUN printf '%s\n' '#!/usr/bin/env bash' \
      'exec python3 -m crustify_audit.cli "$@"' \
      > /usr/local/bin/crustify-audit \
    && chmod +x /usr/local/bin/crustify-audit

RUN printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' \
      '[ -d /opt/crustify-audit/src/crustify_audit ] || {' \
      '    echo "no crustify-audit harness at /opt/crustify-audit" >&2' \
      '    exit 2; }' \
      'target=/target' \
      '# The deterministic scan needs no agent and no auth: run it and stop.' \
      'if [ "${CRUSTIFY_VERB}" = "unsafe" ]; then' \
      '    exec crustify-audit "$target" unsafe "$@"' \
      'fi' \
      '[ "${CRUSTIFY_VERB}" = "orchestrate" ] || {' \
      '    echo "bad CRUSTIFY_VERB: ${CRUSTIFY_VERB} (orchestrate|unsafe)" >&2' \
      '    exit 2; }' \
      'case "${CRUSTIFY_BACKEND:-}" in' \
      '    claude) backend=claude ;;' \
      '    codex)  backend=codex ;;' \
      '    *) echo "bad CRUSTIFY_BACKEND: ${CRUSTIFY_BACKEND:-<empty>} (claude|codex)" >&2' \
      '       exit 2 ;;' \
      'esac' \
      'case "$backend/${CRUSTIFY_PROVIDER:-}" in' \
      '    claude/|claude/anthropic) provider=anthropic ;;' \
      '    codex/|codex/openai)      provider=openai ;;' \
      '    codex/openrouter)         provider=openrouter ;;' \
      '    *) echo "bad CRUSTIFY_PROVIDER for $backend: ${CRUSTIFY_PROVIDER:-<empty>}" >&2' \
      '       exit 2 ;;' \
      'esac' \
      'auditor_provider=$provider' \
      '[ -n "${CRUSTIFY_MODEL:-}" ] || {' \
      '    echo "CRUSTIFY_MODEL must name the orchestrator model" >&2; exit 2; }' \
      '# Checked here: an agent with no usable auth issues no request and exits' \
      '# 0, which reads as a hunt that found nothing.' \
      'auth=()' \
      'case "${CRUSTIFY_BILLING:-}" in' \
      '    subscription)' \
      '        [ "$provider" != openrouter ] || {' \
      '            echo "openrouter requires CRUSTIFY_BILLING=api" >&2; exit 2; } ;;' \
      '    api|"")' \
      '        case "$provider" in' \
      '        anthropic) [ -n "${ANTHROPIC_API_KEY:-}" ] || {' \
      '            echo "CRUSTIFY_BILLING=api needs ANTHROPIC_API_KEY" >&2; exit 2; }' \
      '            auth=(--bare) ;;' \
      '        openai) [ -n "${OPENAI_API_KEY:-}" ] || {' \
      '            echo "CRUSTIFY_BILLING=api needs OPENAI_API_KEY" >&2; exit 2; }' \
      '            auth=(-c "model_provider=openai_apikey"' \
      '                  -c "model_providers.openai_apikey.name=\"OpenAI\""' \
      '                  -c "model_providers.openai_apikey.base_url=\"https://api.openai.com/v1\""' \
      '                  -c "model_providers.openai_apikey.env_key=\"OPENAI_API_KEY\""' \
      '                  -c "model_providers.openai_apikey.wire_api=\"responses\"") ;;' \
      '        openrouter) [ -n "${OPENROUTER_API_KEY:-}" ] || {' \
      '            echo "CRUSTIFY_BILLING=api needs OPENROUTER_API_KEY" >&2; exit 2; }' \
      '            auth=(-c "model_provider=openrouter_apikey"' \
      '                  -c "model_providers.openrouter_apikey.name=\"OpenRouter\""' \
      '                  -c "model_providers.openrouter_apikey.base_url=\"https://openrouter.ai/api/v1\""' \
      '                  -c "model_providers.openrouter_apikey.env_key=\"OPENROUTER_API_KEY\""' \
      '                  -c "model_providers.openrouter_apikey.wire_api=\"responses\"") ;;' \
      '        esac ;;' \
      '    *) echo "bad CRUSTIFY_BILLING: ${CRUSTIFY_BILLING}" >&2; exit 2 ;;' \
      'esac' \
      'effort=()' \
      'case "${CRUSTIFY_EFFORT:-}" in' \
      '    "") ;;' \
      '    low|medium|high|xhigh|max|ultra)' \
      '        effort=(-c "model_reasoning_effort=\"${CRUSTIFY_EFFORT}\"") ;;' \
      '    *) echo "bad CRUSTIFY_EFFORT: ${CRUSTIFY_EFFORT}" >&2; exit 2 ;;' \
      'esac' \
      '# ABLATION SWITCH. With CRUSTIFY_BARE=1 the harness contributes NOTHING' \
      '# to the prompt: no orchestrator role, no container context, no workset' \
      '# or instrument guidance, no artifact layout. The TASK file IS the whole' \
      '# prompt. That is the point -- it measures what the bare model finds on' \
      '# its own, against a full crustify run on the same target and budget.' \
      'if [ "${CRUSTIFY_BARE:-0}" = 1 ]; then' \
      '    [ -f /campaign/TASK.md ] || {' \
      '        echo "CRUSTIFY_BARE=1 needs a task at /campaign/TASK.md" >&2' \
      '        exit 2; }' \
      '    echo "[crustify-audit] BARE: /campaign/TASK.md is the entire prompt"' \
      '    text="$(cat /campaign/TASK.md)"' \
      'else' \
      '# A development mount overrides the prompt baked into the image.' \
      'prompt=/opt/crustify-audit/src/crustify_audit/prompts/orchestrator.md' \
      '[ -f "$prompt" ] || { echo "no orchestrator prompt at $prompt" >&2; exit 2; }' \
      '# What only the container knows, appended rather than templated into the' \
      '# prompt: the file stays usable outside a container.' \
      'text="$(cat "$prompt")' \
      '' \
      '---' \
      '' \
      'Container context, for the questions you are about to ask:' \
      '' \
      '- The target checkout is mounted at /target. It is the only one' \
      '  available, so confirm it rather than asking where it is. Resolve' \
      '  and record the revision you audit yourself, with git in /target.' \
      '- Defaults you may propose: --model' \
      '  $auditor_provider/$CRUSTIFY_MODEL, --billing' \
      '  $CRUSTIFY_BILLING, --effort $CRUSTIFY_EFFORT, and --timeout' \
      '  $CRUSTIFY_TIMEOUT (minutes per auditor).' \
      '- Interaction mode: $CRUSTIFY_HEADLESS (0 is interactive, 1 is' \
      '  headless).' \
      '- The crustify-audit CLI is on PATH. Spawn auditors with it. Run the' \
      '  deterministic scan first -- crustify-audit /target unsafe -- for the' \
      '  data that tells you how to partition worksets."' \
      '# Append a task only when the conventional external mount is present.' \
      'task=' \
      'if [ -f /campaign/TASK.md ]; then' \
      '    task=/campaign/TASK.md' \
      'fi' \
      '# A pre-filled task answers part of the interview. Appended, not' \
      '# merged: the orchestrator reads it and resolves what is missing.' \
      'if [ -n "$task" ]; then' \
      '    echo "[crustify-audit] task $task"' \
      '    text="$text' \
      '' \
      '---' \
      '' \
      'The user pre-filled the task below. Concrete answers are decided. Ask' \
      'for mandatory answers that are missing or still in angle brackets;' \
      'choose unanswered optional values yourself. In headless mode, do not' \
      'launch while a mandatory answer is unresolved.' \
      '' \
      '$(cat "$task")"' \
      'fi' \
      'fi' \
      'skip=--dangerously-skip-permissions' \
      'bypass=--dangerously-bypass-approvals-and-sandbox' \
      'case "${backend}:${CRUSTIFY_HEADLESS}" in' \
      '    claude:0) exec claude "$skip" "${auth[@]}" --model "$CRUSTIFY_MODEL" "$text" "$@" ;;' \
      '    claude:1) exec claude "$skip" "${auth[@]}" --model "$CRUSTIFY_MODEL" -p "$text" "$@" ;;' \
      '    codex:0)  exec codex "$bypass" "${auth[@]}" "${effort[@]}" --model "$CRUSTIFY_MODEL" "$text" "$@" ;;' \
      '    codex:1)  exec codex exec "$bypass" "${auth[@]}" "${effort[@]}" --model "$CRUSTIFY_MODEL" "$text" "$@" ;;' \
      '    *) echo "bad CRUSTIFY_BACKEND/CRUSTIFY_HEADLESS:" \' \
      '            "${CRUSTIFY_BACKEND}/${CRUSTIFY_HEADLESS}" >&2; exit 2 ;;' \
      'esac' \
      > /usr/local/bin/crustify-audit-orchestrator \
    && chmod +x /usr/local/bin/crustify-audit-orchestrator

# The orchestrator starts in the repository it will have auditors work on.
WORKDIR /target
CMD ["crustify-audit-orchestrator"]
