---

# Campaign questions

1. **Which repository and revision should this campaign use?**
   - Answer: `https://github.com/ffmpeg/ffmpeg`, latest revision
2. **Should this campaign port the C implementation to Rust, or create safe Rust wrappers?**
   - Answer: create safe Rust wrappers
3. **Where should this campaign start: one or two subsystems, a named subset of functions or types, or the whole target? Should sub-campaigns be defined now or brainstormed during the live session?**
   - Answer: 50% of the whole public API of the target

# Sub-campaign questions

## `libavutil`

4. **Which implementation paths belong to this subsystem?**
   - Answer: `libavutil/`
5. **Which headers define its public API?**
   - Answer: derive from `libavutil/`
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: 50% of the whole public API of the target
7. **Which backend and model should translate this sub-campaign?**
   - Answer: `codex`, `gpt-5.6-sol`

# Campaign execution questions

8. **Use default workload settings, or customize them?**
   - Answer: defaults; parallelism is orchestrator's choice
9. **Do you want agentic review? At which milestones and with which model?**
   - Answer: at campaign end, using `claude-opus-5`
10. **What batch caps should review agents use? We recommend 3x the translation caps.**
    - Answer: recommended 3x
11. **Run the optional agentic UB pass? If so, with which model?**
    - Answer: at campaign end with explicit approval, using `claude-opus-5`

# Autonomy questions

A1. **Should I run fully autonomously end to end?**
    - Answer: yes
A2. **If no, should I wait for your approval before starting the setup phase?**
    - Answer: no
A3. **Should I wait for your approval before starting the translation phase?**
    - Answer: yes, after setup identifies the selected surface
A4. **Should I wait for your approval in between sub-campaigns?**
    - Answer: no
A5. **Should I wait for your approval before starting review passes?**
    - Answer: no
A6. **Should I wait for your approval before starting UB audit passes?**
    - Answer: yes

# Benchmark recording questions

12. **Which billing mode should agentic stages use?**
    - Answer: `subscription` for claude, `api` for codex
13. **Where and in what format should results be recorded?**
    - Answer: `<repo-checkout>/crustify/results.md`, match the exact layout

# Notes

Exclude every FFmpeg library other than libavutil.
