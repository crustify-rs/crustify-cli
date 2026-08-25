---

Fill in as many answers as you want before starting the orchestrator. It will
ask only for campaign decisions that remain unresolved.

# Mandatory questions

## Campaign

1. **Which repository and revision should this campaign use?**
   - Answer: `<repository URL>, <commit or tag>`
2. **Should this campaign port the C implementation to Rust, or create safe
   Rust wrappers?**
   - Answer: `<port | wrap>`
3. **Where should this campaign start: one or two named subsystems, a named
   subset of functions or types, or the whole target? Should we define
   sub-campaigns now or brainstorm them during the live session?**
   - Answer: `<named subsystems | named functions/types | whole target |
     define sub-campaigns now | brainstorm during the live session |
     orchestrator-selected>`

4. **Which backend and model should translate each sub-campaign? Should they all
   use the same model?**
   - Answer: `<backend, model for all | sub-campaign: backend, model; ... |
     orchestrator-selected>`

5. **Do you want agentic review after translated work lands? If so, at which
   campaign milestones, and which model should perform each review?**
   - Answer: `<none | orchestrator-selected | milestone: model; ...>`

6. **Should the campaign run the optional agentic UB audit pass? If so, which
   model should run it?**
   - Answer: `<no | yes with explicit approval, model>`

7. **Should I run fully autonomously end to end?**
   - Answer: `<yes | no>`

8. **Which billing mode should agentic stages use?**
   - Answer: `<api | subscription>`

# Optional questions

Unanswered optional questions use their defaults.

## Campaign execution

9. **Should the campaign use the default batching and parallelism settings, or
   customize them?**
   - Answer: `<defaults | max-types: N, max-syms: N, max-loc: N,
     parallel-max: N | orchestrator-selected>`

10. **What batch caps should review agents use? We recommend 3x the translation
   caps so each reviewer sees more related units.**
   - Answer: `<recommended 3x | same as translation | max-types: N,
     max-syms: N, max-loc: N>`

## Autonomy (if question 7 is answered `no`)

Answer these approval-gate questions only if question 7 is answered `no`.

11. **Should I wait for your approval before starting the setup phase?**
    - Answer: `<yes | no>`
12. **Should I wait for your approval before starting the translation phase?**
    - Answer: `<yes | no>`
13. **Should I wait for your approval between sub-campaigns?**
    - Answer: `<yes | no | not applicable>`
14. **Should I wait for your approval before starting review passes?**
    - Answer: `<yes | no | not applicable>`
15. **Should I wait for your approval before starting UB audit passes?**
    - Answer: `<yes | no | not applicable>`

# Benchmark recording questions

16. **Where and in what format should results be recorded?**
    - Answer: `<results path>, <standard | custom template>`

# Guidance

- Answer only questions whose values are not already fixed by the task.
- Every agentic stage names its model. Deterministic `crustify-audit unsafe`
  checks do not need one.
- Waves and steps are internal scheduler artifacts, not user-facing questions.
