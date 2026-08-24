---

# Campaign questions

1. **Which repository and revision should this campaign use?**
   - Answer: `https://github.com/openssl/openssl`, `2924476b5591e691e904c4baf57894c526c4b8de`
2. **Should this campaign port the C implementation to Rust, or create safe Rust wrappers?**
   - Answer: create safe Rust wrappers
3. **Where should this campaign start: one or two subsystems, a named subset of functions or types, or the whole target? Should sub-campaigns be defined now or brainstormed during the live session?**
   - Answer: a named subset of the libcrypto public API; define the `libcrypto-public-api` sub-campaign now

# Sub-campaign questions

## `libcrypto-public-api`

4. **Which implementation paths belong to this subsystem?**
   - Answer: derive from the libcrypto build definition
5. **Which headers define its public API?**
   - Answer: derive from the libcrypto public headers
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: a user-selected subset; exclude libssl
7. **Which backend and model should translate this sub-campaign?**
   - Answer: `codex`, `gpt-5.6-sol`

# Campaign execution questions

8. **Use default workload settings, or customize them?**
   - Answer: defaults except `max-types: 2`; parallelism is orchestrator-selected
9. **Do you want agentic review? At which milestones and with which model?**
   - Answer: ask the user for both milestones and model
10. **Run autonomously or pause after each sub-campaign?**
    - Answer: ask the user
11. **Run the optional agentic UB pass? If so, with which model?**
    - Answer: only with explicit user approval; ask for the model if enabled

# Benchmark recording questions

12. **Which backend and model run the orchestrator?**
    - Answer: `codex`, `gpt-5.6-sol`
13. **Which billing mode should agentic stages use?**
    - Answer: `api`
14. **Has setup already been approved?**
    - Answer: no
15. **Where and in what format should results be recorded?**
    - Answer: `<repo-checkout>/crustify/wrappers-results.md`, standard template

# Notes

The normal deterministic `crustify-audit unsafe` checks remain enabled
independently of agentic review.
