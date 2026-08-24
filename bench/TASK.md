---

# Campaign questions

1. **Which repository and revision should this campaign use?**
   - Answer: `<repository URL>, <commit or tag>`
2. **Should this campaign port the C implementation to Rust, or create safe
   Rust wrappers?**
   - Answer: `<port | wrap>`
3. **Which subsystems should be handled as separate sub-campaigns?**
   - Answer: `<sub-campaign names>`

# Sub-campaign questions

Copy this block once for every answer to question 3.

## `<sub-campaign name>`

4. **Which implementation paths belong to this subsystem?**
   - Answer: `<paths>`
5. **Which headers define its public API?**
   - Answer: `<paths | derive from implementation paths>`
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: `<whole subsystem | named subset: ...>`
7. **Which backend and model should translate this sub-campaign?**
   - Answer: `<codex | claude>, <model>`

# Campaign execution questions

8. **Should the campaign use the default batching and parallelism settings, or
   customize them?**
   - Answer: `<defaults | max-types: N, max-syms: N, max-loc: N,
     parallel-max: N | orchestrator-selected>`

9. **Do you want agentic review? If so, at which campaign milestones, and
   which model should perform each review?**
   - Answer: `<none | milestone: model; ...>`

10. **After approval, should the orchestrator run autonomously or pause after
    each sub-campaign?**
    - Answer: `<autonomous | pause after each sub-campaign>`

11. **Should the campaign run the optional agentic UB pass? If so, which model
    should run it?**
    - Answer: `<no | yes with explicit approval, model>`

# Benchmark recording questions

12. **Which backend and model run the orchestrator?**
    - Answer: `<codex | claude>, <model>`
13. **Which billing mode should agentic stages use?**
    - Answer: `<api | subscription>`
14. **Has setup already been approved?**
    - Answer: `<yes | no>`
15. **Where and in what format should results be recorded?**
    - Answer: `<path>, <template>`

# Guidance

- Answer only questions whose values are not already fixed by the task.
- Every agentic stage names its model. Deterministic `crustify-audit unsafe`
  checks do not need one.
- Waves and steps are internal scheduler artifacts, not user-facing questions.
