# Bench

One target per directory. Each holds the `TASK.md` a run is given, plus
whatever Phase 1 artifacts are authored ahead of time.

`Dockerfile` bootstraps the orchestrator that runs one.
Replace with `OPENAI_API_KEY` and `CRUSTIFY_BACKEND=codex` for
running the orchestrator and translator agents with codex.
You can also do claude orchestrator and codex translators, or vice-versa.

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

The orchestrator is instructed to track translation results in `wrappers-results.md`.

`CRUSTIFY_CAMPAIGN` names a directory here, read through the checkout mount:
edit its `TASK.md` and the next run picks it up. That mount is read-write and
the agents commit to it, so give them a branch you can review. Everything under
`/work` — the clone, the CodeQL database, the emitted crates, the session
branches — is the orchestrator's own work and survives `--rm` in the named
volume. The Dockerfile header documents what each flag buys.
