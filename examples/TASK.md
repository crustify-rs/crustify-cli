---

Fill in as many answers as you want before starting the orchestrator. It will
ask only for campaign decisions that remain unresolved.

# Campaign questions

1. **Which repository and revision should this campaign use?**
   - Answer: `<repository URL>, <commit or tag>`
2. **Should this campaign port the C implementation to Rust, or create safe
   Rust wrappers?**
   - Answer: `<port | wrap>`
3. **Where should this campaign start: one or two subsystems, a named subset of
   functions or types, or the whole target? Should sub-campaigns be defined now
   or brainstormed during the live session?**
   - Answer: `<starting scope>; <define sub-campaigns now | brainstorm live | orchestrator selected>`

# Campaign execution questions

8. **Should the campaign use the default batching and parallelism settings, or
   customize them?**
   - Answer: `<defaults | max-types: N, max-syms: N, max-loc: N,
     parallel-max: N | orchestrator-selected>`

9. **Do you want agentic review? If so, at which campaign milestones, and
   which model should perform each review?**
   - Answer: `<none | orchestrator-selected | milestone: model; ...>`

10. **Should the campaign run the optional agentic UB pass? If so, which model
    should run it?**
    - Answer: `<no | yes with explicit approval, model>`

# Autonomy questions

A1. **Should I run fully autonomously end to end?**
    - Answer: `<yes | no>`
A2. **If no, should I wait for your approval before starting the setup phase?**
    - Answer: `<yes | no | not applicable>`
A3. **Should I wait for your approval before starting the translation phase?**
    - Answer: `<yes | no>`
A4. **Should I wait for your approval in between sub-campaigns?**
    - Answer: `<yes | no | not applicable>`
A5. **Should I wait for your approval before starting review passes?**
    - Answer: `<yes | no | not applicable>`
A6. **Should I wait for your approval before starting UB audit passes?**
    - Answer: `<yes | no | not applicable>`

# Benchmark recording questions

11. **Which billing mode should agentic stages use?**
    - Answer: `<api | subscription>`
12. **Where and in what format should results be recorded?**
    - Answer: `wrappers-results.md | <custom-template>`

# Guidance

- Answer only questions whose values are not already fixed by the task.
- Every agentic stage names its model. Deterministic `crustify-audit unsafe`
  checks do not need one.
- Waves and steps are internal scheduler artifacts, not user-facing questions.
