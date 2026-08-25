---

# Campaign questions

1. **Which repository and revision should this campaign use?**
   - Answer: `https://github.com/libgit2/libgit2`, latest revision
2. **Should this campaign port the C implementation to Rust, or create safe Rust wrappers?**
   - Answer: port the C implementation to Rust
3. **Where should this campaign start: one or two subsystems, a named subset of functions or types, or the whole target? Should sub-campaigns be defined now or brainstormed during the live session?**
   - Answer: until we reach 10K LoC of C ported to native Rust

# Sub-campaign questions

## `src`

4. **Which implementation paths belong to this subsystem?**
   - Answer: `src/`
5. **Which headers define its public API?**
   - Answer: derive from `src/`
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: until we reach 10K LoC of C ported to native Rust, you propose sub-campaigns, then wait for my approval to go
7. **Which backend and model should translate this sub-campaign?**
   - Answer: `codex`, `gpt-5.6-sol`

# Campaign execution questions

8. **Use default workload settings, or customize them?**
   - Answer: parallelism is orchestrator-selected
9. **Do you want agentic review? At which milestones and with which model?**
   - Answer: after raw lifetime discovery and at each sub-campaign end, using `claude-opus-5`
10. **What batch caps should review agents use? We recommend 3x the translation caps.**
    - Answer: recommended 3x
11. **Run the optional agentic UB pass? If so, with which model?**
    - Answer: only with explicit approval, using `claude-opus-5`, at campaign end

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
    - Answer: `api` for codex, `subscription` for claude
13. **Where and in what format should results be recorded?**
    - Answer: `<repo-checkout>/crustify/results.md`, standard template

The campaign is in flight, but has been interrupted. Assess the current state and proceed with the instructions.
