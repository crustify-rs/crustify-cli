# Evaluation

One target per directory. Each holds the `TASK.md` a run is given, plus
whatever Phase 1 artifacts are authored ahead of time.

`Dockerfile` bootstraps the orchestrator that runs one.
Replace with `OPENAI_API_KEY` and `CRUSTIFY_BACKEND=codex` for
running the orchestrator and translator agents with codex.
You can also do claude orchestrator and codex translators, or vice-versa.

Both commands from the repo root:

```sh
docker build -t crustify evaluation/

docker run --rm -it --name crustify-libgit2 \
    -e ANTHROPIC_API_KEY -e CRUSTIFY_BACKEND=claude \
    -v "$PWD/evaluation/libgit2:/campaign:ro" \
    -v crustify-work:/work \
    crustify
```

The orchestrator is instructed to track translation results in `wrappers-results.md`.

The campaign mount is the only target-specific input; everything under `/work`
— the clone, the CodeQL database, the emitted crates, the session branches —
is the orchestrator's own work and survives `--rm` in the named volume. The
Dockerfile header documents what each flag buys.
