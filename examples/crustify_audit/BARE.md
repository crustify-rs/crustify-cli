# Ablation: the bare backend

`CRUSTIFY_BARE=1` starts the container's backend CLI with the mounted
`TASK-bare.md.template` as its **entire** prompt. 

`TASK-bare.md.template` is deliberately minimal -- two lines, 21 words.

## Running it

    docker run --rm -it --name bare-git2 \
        -e OPENROUTER_API_KEY \
        -e CRUSTIFY_BARE=1 \
        -e CRUSTIFY_BACKEND=claude \
        -e CRUSTIFY_PROVIDER=openrouter \
        -e CRUSTIFY_MODEL=anthropic/claude-opus-5 \
        -e CRUSTIFY_BILLING=api \
        -e CRUSTIFY_HEADLESS=1 \
        -v /path/to/target:/target \
        -v /path/to/TASK-bare.md:/campaign/TASK.md:ro \
        -v bare-git2-work:/work \
        crustify-audit

`CRUSTIFY_TIMEOUT` has no effect in bare mode: nothing spawns auditors, so the
budget is whatever the single agent takes. Cap it from outside if you need one.
