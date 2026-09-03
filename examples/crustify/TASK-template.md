
Fill in as many answers as you want before starting the orchestrator. It will
ask only for campaign decisions that remain unresolved.

# Mandatory questions

## Campaign

1. **Which repository and revision should this campaign use?**
   - Answer: `<repository URL>, <commit or tag>`

2. **Should this campaign port the C implementation to Rust, or create safe
   Rust wrappers?**
   - Answer: `<port | wrap>`

3. **What should this campaign target: a named subset of subsystems, or a named
   subset of functions and types, or the whole target repo? You can define them
   now or we can brainstorm them during the live session. You can also answer
   orchestrator's choice.**
   - Answer: `<named subsystems | named functions/types | whole target |
     orchestrator's choice>`

4. **Which agentic backend and model should do the translation work?**
   - Answer: `<backend, model | orchestrator's choice>`

5. **Do you want agentic review after translated work lands? If so, which
   backend and model should perform each review?**
   - Answer: `<no | backend, model ...>`

6. **Should the campaign run the optional agentic UB audit pass? If so, which
   backend and model should run it?**
   - Answer: `<no | backend, model>`

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
     parallel-max: N | orchestrator's choice>`

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
- Port campaigns default to one bottom-up sub-campaign per subsystem. Raw
  `void` and raw `string` lifetime discovery are separate initial
  sub-campaigns, each with its own `scope-config.json`.
- When review is allowed, the orchestrator prefers reviewing after each
  completed sub-campaign. When the UB pass is approved, it prefers running it
  once at campaign end.
- Waves and steps are internal scheduler artifacts, not user-facing questions.
