---

# Campaign questions

1. **Which repository and revision should this campaign use?**
   - Answer: `https://github.com/intel/cryptography-primitives`, `9d397ba62e2369b63171bc995e9c1179aaa5c0dc`
2. **Should this campaign port the C implementation to Rust, or create safe Rust wrappers?**
   - Answer: create safe Rust wrappers
3. **Which subsystems should be handled as separate sub-campaigns?**
   - Answer: `public-api`

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
10. **Run autonomously or pause after each sub-campaign?**
    - Answer: autonomous
11. **Run the optional agentic UB pass? If so, with which model?**
    - Answer: no

# Benchmark recording questions

12. **Which backend and model run the orchestrator?**
    - Answer: `codex`, `gpt-5.6-sol`
13. **Which billing mode should agentic stages use?**
    - Answer: `api`
14. **Has setup already been approved?**
    - Answer: yes; Phase 1 is pre-approved
15. **Where and in what format should results be recorded?**
    - Answer: `<repo-checkout>/crustify/wrappers-results.md`, standard template, git tracked

# Notes

Phase 1 and Phase 2 are approved to run autonomously end to end.
