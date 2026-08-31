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
   - Answer: defaults except `max-types: 2`; parallelism is orchestrator's choice
9. **Do you want agentic review? At which milestones and with which model?**
   - Answer: ask the user for both milestones and model
10. **What batch caps should review agents use? We recommend 3x the translation caps.**
    - Answer: ask the user; recommend 3x
11. **Run the optional agentic UB pass? If so, with which model?**
    - Answer: only with explicit user approval; ask for the model if enabled

# Autonomy questions

A1. **Should I run fully autonomously end to end?**
    - Answer: ask the user
A2. **If no, should I wait for your approval before starting the setup phase?**
    - Answer: ask the user if autonomy is declined
A3. **Should I wait for your approval before starting the translation phase?**
    - Answer: ask the user
A4. **Should I wait for your approval in between sub-campaigns?**
    - Answer: ask the user
A5. **Should I wait for your approval before starting review passes?**
    - Answer: ask the user if review is enabled
A6. **Should I wait for your approval before starting UB audit passes?**
    - Answer: yes, if the UB audit is enabled

# Benchmark recording questions

12. **Which billing mode should agentic stages use?**
    - Answer: `api`
13. **Where and in what format should results be recorded?**
    - Answer: `<repo-checkout>/crustify/results.md`, standard template

# Notes

The normal deterministic `crustify-audit unsafe` checks remain enabled
independently of agentic review.
