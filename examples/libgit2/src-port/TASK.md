---

# Campaign questions

1. **Which repository and revision should this campaign use?**
   - Answer: `https://github.com/libgit2/libgit2`, latest revision
2. **Should this campaign port the C implementation to Rust, or create safe Rust wrappers?**
   - Answer: port the C implementation to Rust
3. **Where should this campaign start: one or two subsystems, a named subset of functions or types, or the whole target? Should sub-campaigns be defined now or brainstormed during the live session?**
   - Answer: the whole `src` target; define the `src` sub-campaign now

# Sub-campaign questions

## `src`

4. **Which implementation paths belong to this subsystem?**
   - Answer: `src/`
5. **Which headers define its public API?**
   - Answer: derive from `src/`
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: a user-selected subset
7. **Which backend and model should translate this sub-campaign?**
   - Answer: `codex`, `gpt-5.6-sol`

# Campaign execution questions

8. **Use default workload settings, or customize them?**
   - Answer: defaults except `max-types: 2`; parallelism is orchestrator-selected
9. **Do you want agentic review? At which milestones and with which model?**
   - Answer: after raw lifetime discovery and at campaign end, using `gpt-5.6-sol`
10. **Run the optional agentic UB pass? If so, with which model?**
    - Answer: only with explicit approval, using `gpt-5.6-sol`

# Autonomy questions

A1. **Should I run fully autonomously end to end?**
    - Answer: no
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

11. **Which backend and model run the orchestrator?**
    - Answer: `codex`, `gpt-5.6-sol`
12. **Which billing mode should agentic stages use?**
    - Answer: `api`
13. **Where and in what format should results be recorded?**
    - Answer: `<repo-checkout>/crustify/results.md`, standard template
