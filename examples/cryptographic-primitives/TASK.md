---

# Campaign questions

1. **Which repository and revision should this campaign use?**
   - Answer: `https://github.com/intel/cryptography-primitives`, `9d397ba62e2369b63171bc995e9c1179aaa5c0dc`
2. **Should this campaign port the C implementation to Rust, or create safe Rust wrappers?**
   - Answer: create safe Rust wrappers
3. **Where should this campaign start: one or two subsystems, a named subset of functions or types, or the whole target? Should sub-campaigns be defined now or brainstormed during the live session?**
   - Answer: the whole public API; define the `public-api` sub-campaign now

# Sub-campaign questions

## `public-api`

4. **Which implementation paths belong to this subsystem?**
   - Answer: derive during setup
5. **Which headers define its public API?**
   - Answer: derive from the implementation paths
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: whole public API, excluding the imported closure
7. **Which backend and model should translate this sub-campaign?**
   - Answer: `codex`, `gpt-5.6-sol`

# Campaign execution questions

8. **Use default workload settings, or customize them?**
   - Answer: defaults except `max-types: 2`; parallelism is orchestrator-selected
9. **Do you want agentic review? At which milestones and with which model?**
   - Answer: at campaign end over all translated output, using `gpt-5.6-sol`
10. **What batch caps should review agents use? We recommend 3x the translation caps.**
    - Answer: recommended 3x
11. **Run the optional agentic UB pass? If so, with which model?**
    - Answer: no

# Autonomy questions

A1. **Should I run fully autonomously end to end?**
    - Answer: yes
A2. **If no, should I wait for your approval before starting the setup phase?**
    - Answer: not applicable
A3. **Should I wait for your approval before starting the translation phase?**
    - Answer: no
A4. **Should I wait for your approval in between sub-campaigns?**
    - Answer: no
A5. **Should I wait for your approval before starting review passes?**
    - Answer: no
A6. **Should I wait for your approval before starting UB audit passes?**
    - Answer: not applicable

# Benchmark recording questions

12. **Which billing mode should agentic stages use?**
    - Answer: `api`
13. **Where and in what format should results be recorded?**
    - Answer: `<repo-checkout>/crustify/results.md`, standard template, git tracked

# Notes

Phase 1 and Phase 2 are approved to run autonomously end to end.
