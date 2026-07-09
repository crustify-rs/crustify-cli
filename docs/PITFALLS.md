# Pitfalls

Running log of mistakes the LLM agents have made on real runs, in
forensic-quality form. Each agent prompt carries a brief `Pitfalls`
section that cross-references the entries here for the long form;
this file is the canonical source.

Maintained for three reasons:

1. **Prompt authoring.** When a pitfall surfaces, the corresponding
   agent prompt grows a brief `Pitfalls` entry that points here, so
   the next run doesn't repeat the mistake.
2. **Manifest audit.** Humans reviewing a produced manifest can spot
   suspicious patterns (e.g. *"this wrap/syms.json is missing
   `CRYPTO_memcmp` — known pitfall §2026-05-31, was the agent's
   discovery query restructured?"*).
3. **Cross-agent learning.** A failure mode discovered for one agent
   often applies to siblings (e.g. a CodeQL anti-pattern caught in
   `CrustifySymbolAnalyzer` may bite `CrustifyTypeAnalyzer` too).

Entry conventions:

- Date the pitfall surfaced (ISO date), used as the entry anchor.
- Agent name.
- Brief title.
- **Symptom** — what was observed in the output.
- **Root cause** — what the agent did wrong, in enough detail that a
  reader can recognise the pattern in someone else's query.
- **Where it came from** — why the agent reached for the wrong
  approach (so we can spot the lure).
- **Fix** — how to restructure to avoid it.
- **Self-check** — a cheap test the agent can run before emitting to
  detect this class of bug going forward.

When backporting a fix into the prompt, add a brief in-prompt
`Pitfalls` entry that cross-references the date anchor here. Don't
duplicate the full analysis in the prompt — keep prompts focused;
this file holds the depth.

---

## 2026-05-31 — CrustifySymbolAnalyzer — `hasDefinition()` filter excludes declaration-only externs

**Symptom.** Functions called from port-scope code missing from
`wrap/syms.json`. Observed on
`openssl-crustify-statem/ssl/statem/`: `CRYPTO_memcmp` (3 distinct
port callers across `extensions_srvr.c`, `statem_lib.c`,
`extensions.c`) and `OPENSSL_cleanse` (6 distinct port callers
across `extensions.c`, `extensions_clnt.c`, `extensions_srvr.c`,
`statem_clnt.c`, `statem_srvr.c`) were both absent from the
produced manifest. The previous (3-kind) generation had correctly
listed them. `ripgrep` at the source level confirmed 4 and 11 call
sites respectively.

**Root cause.** The discovery query had `fn.hasDefinition()` as a
load-bearing filter on the outer `from` / `where`:

```ql
from Function fn, File def, ...
where
  fn.hasDefinition() and              ← LOAD-BEARING
  def = fn.getDefinitionLocation().getFile() and
  ...
  (
    portFile(def)
    or exists(Call c | c.getEnclosingFunction().getFile() is portFile
                       and c.getTarget() = fn)
    or exists(FunctionAccess fa | ...)
  )
```

Functions whose body isn't in the CodeQL database — i.e.
declaration-only externs — were rejected by the `hasDefinition()`
test BEFORE the reachability check ran. The OpenSSL CodeQL database
for this project is partial: it includes some `crypto/*.c` files but
not all, and excludes any file whose C body was replaced by an
assembly implementation during the build (`crypto/mem_clr.c`,
where `OPENSSL_cleanse`'s C fallback lives, is entirely missing —
6 functions from `mem_sec.c`, 9 from `mem.c`, 3 from `cryptlib.c`,
but nothing from `mem_clr.c`).

Direct CodeQL inspection confirms:

| Symbol | hasDefinition | def location | declarations |
|---|---|---|---|
| `CRYPTO_memcmp` | **no** | (none in DB) | 1 (`include/openssl/crypto.h:505`) |
| `OPENSSL_cleanse` | **no** | (none in DB) | 2 (`include/openssl/crypto.h:424` + forward decl in `crypto/provider_core.c:2309`) |
| `SSL_new` (control) | yes | `ssl/ssl_lib.c` | 2 |
| `WPACKET_put_bytes__` (control) | yes | `crypto/packet.c` | 2 |

A restructured query, starting from `Call` rather than `Function`,
recovers both symbols and lists 9 distinct call sites across 7
port-scope functions — matching the source-level `ripgrep` count.

**Where it came from.** `fn.hasDefinition()` is a standard CodeQL
idiom for filtering noise from header-declared-but-never-used
externs. Removing it without an alternative noise filter risks
emitting every prototype in every system header. The agent
inherited the idiom because it cleanly captures `defined_in` via
`fn.getDefinitionLocation()`, and never tested its query against a
known-reached external symbol to detect the gap.

**Fix.**

1. **Restructure the query to start from the call edge**, not the
   function entity:

   ```ql
   from Function fn, Call c
   where
     c.getTarget() = fn
     and c.getEnclosingFunction().getFile() is port-scope
   ```

   This catches every callee reachable from port code regardless of
   whether its body is in the database.

2. **Handle the missing-definition case in the emission step**:
   - When the function has a definition in the DB → `defined_in =
     <path>` as before.
   - When the function has only declarations → `defined_in = null`,
     `declared_in = <list of header paths>`.

3. **For noise control**, filter at the result-set level — e.g.
   require at least one declaration in a non-system header, or one
   call from a port-scope function — rather than at the query-time
   `hasDefinition` level.

**Self-check the agent should adopt.** Before writing the
manifests, spot-check a small set of known-frequent
cross-boundary callees. For OpenSSL-family projects: `ERR_raise`,
`OPENSSL_free`, `OPENSSL_cleanse`, `CRYPTO_memcmp`, `memcpy`. For
other projects, the analyzer should ask the porter for a
representative list at run-time, or use grep on port source to
build one. If any sample is missing from the produced manifest
despite appearing in port source, the discovery query has a
coverage gap and needs widening before emission.

---

## 2026-06-02a — CrustifySymbolAnalyzer — Defensive-default abuse on `ptr_args` / `ptr_ret` at scale

**Symptom.** A full e2e symbol run on the openssl-crustify-statem
statem partition (1,589 entries, 797 of them functions) emitted
schema-valid output for every field, all invariants honoured,
zero violations — but the per-entry ownership decisions were
dominated by the `(moved=false, borrowed=false)` undecidable
state:

| Field | Decided | Undecidable / defaulted |
|---|---:|---:|
| `ptr_ret.moved=true` | 20 | — |
| `ptr_ret.borrowed=true` | 10 (all `arg:<name>`, none `static` or `other`) | — |
| `ptr_ret` undecidable `(false, false)` | — | **90 (75%)** |
| `ptr_args.moved=true` | 30 / 1,435 (2.1%) | — |

Every decided case followed a single naming convention: `_free` /
`_pop_free` / `_clear_free` / `_release` for `ptr_args.moved`,
`_new` / `_dup` / `_strdup` / `_create` / `_d2i` / `_alloc` /
`_get1_*` for `ptr_ret.moved`, `_get0_*` / `_peek*` for
`ptr_ret.borrowed`. Functions whose names didn't match a
convention (`BIO_method_new`, `EVP_PKEY_param_check`,
`OPENSSL_LH_retrieve`, etc.) landed on the defensive default. Cost
was $1.63 — far below the per-entity-reasoning projection of
~$100.

**Root cause.** The agent wrote a 17,906-character Python finaliser
script (`/tmp/finalize_syms.py`) at step 19/28 that processes all
1,589 entries mechanically — library tag by path prefix, macro
kind by regex over body, ptr fields by function-name suffix
matching. The script's own header comment makes the decision
explicit: *"Conservative defaults — heuristics applied only when
name patterns are unambiguous. Most cases default to
`(moved=false, mutable=!const)` for args and `(moved=false,
borrowed=false)` for returns with a documenting note."* The agent
collapsed the prompt's three-signal triad (docs → body + callers
+ name → CodeQL when needed) to **name patterns only**, dropping
body inspection and CodeQL entirely.

Four pressures converged to produce this:

1. **The prompt's own escape hatch.** Step 5 includes the
   sentence *"Defensive default when undecidable: moved=false,
   borrowed=false, with note documenting the uncertainty."* The
   agent read this as permission to mass-default rather than as a
   last-resort fallback after evidence-gathering. The schema
   explicitly allows `(false, false)` as a valid state, so there
   was no invariant violation to force the harder work.
2. **Step / budget economics at 1,589-entry scale.** 100 steps
   total, ~80 left after discovery, ~16K tokens per "reasoning"
   step. Per-entity body inspection + CodeQL would have required
   ~1,500 steps. The agent computed implicitly that depth was
   incompatible with scale and chose breadth.
3. **No iteration scaffolding.** The prompt's Step 5 reads as a
   global instruction set ("for every `function_*` entry, fill
   these fields per the triad") with no per-entity step. The
   agent interpreted this as "produce a global solution", and a
   single Python script IS one.
4. **Signal collapse.** "Body" inspection requires reading source
   per entry (expensive). "Callers" requires reading caller
   bodies (expensive fan-out). "Name patterns" is cheap regex.
   At 1,500-entry scale the agent rationally dropped the
   expensive two of three.

The behaviour is not a bug — it's a rational response to the
prompt's permissiveness and the scale. But the resulting
manifest's ownership data is largely placeholder-quality and not
actionable for the wrapper-generation phase that consumes it.

**Where it came from.** Two prompt-design lures:

- **Schema-validity ≠ analytic-value.** Crustify's analyzer
  prompts validate schema invariants but don't measure coverage
  quality. The agent has every incentive to satisfy the
  invariants cheaply when the budget pressures it; "undecidable
  with a note" satisfies them.
- **Permissive escape hatches collapse depth.** The phrase
  "defensive default is permitted when undecidable" has the same
  shape as "this is a soft constraint, you may skip it". At
  small selection sizes (≤ 10 entries the agent body-inspects
  each), the escape hatch is rarely taken; at large selections
  it dominates.

**Fix.**

1. **Tighten the defensive-default license in the prompt.**
   Replace *"Defensive default when undecidable…"* with
   *"Defensive default is permitted ONLY when at least one
   body-inspection signal has been examined and remained
   inconclusive. Pure name-pattern reasoning without body
   inspection does NOT justify the defensive default; the `note`
   field on a `(false, false)` entry MUST attest 'body checked,
   no clear signal' or equivalent."*

2. **Add a coverage-quality self-check before emission.** *"If
   more than 30% of `ptr_ret` entries are `(false, false)` or
   more than 70% of `ptr_args` have `moved=false`, re-examine
   the undecided cases with body inspection before writing."*
   This sets a soft floor that forces the agent to do the
   expensive work at scale rather than skip it.

3. **Decompose phase 1b for large selections.** Phase 1b1 (cheap:
   library + macro kind + composer-filled fields) stays as one
   pass. Phase 1b2 (ownership analysis on `function_*` entries
   with `ptr_args` / `ptr_ret`) runs as a separate agent
   invocation with its own step budget and a focused prompt that
   mandates body inspection per entry. The cost decouples: ~$1.50
   for 1b1 across 1,500+ entries, separately budgeted ~$50–80 for
   1b2's ~800 functions.

4. **Pre-emit ownership-signal edges in the query pack** so the
   composer does the mechanical body inspection upstream and the
   agent's role collapses to confirm/reject. Candidate queries:
   `edges/arg_freed_in_body.ql` (which args flow into
   `<T>_free` / `OPENSSL_free` calls in the body),
   `edges/ret_from_allocator.ql` (which functions return a fresh
   `OPENSSL_malloc` / `<T>_new` result), `edges/ret_from_arg_field.ql`
   (which functions return `arg->field` or transitively), and a
   refcount-bump query. With these pre-emitted, the composer can
   pre-fill `moved` / `borrowed` candidates from facts; the agent
   confirms by signature shape. See §2026-06-02b for the
   architecturally-aligned story.

**Self-check the agent should adopt.** After writing the
manifests but before declaring done, compute the per-field
decision distribution:

```python
ptr_ret_decided  = sum(1 for e if ptr_ret and (moved or borrowed))
ptr_ret_default  = sum(1 for e if ptr_ret and not moved and not borrowed)
ptr_args_decided = sum(1 for a in all_args if a.moved is True)
ptr_args_total   = sum(1 for a in all_args)
```

If `ptr_ret_default / (ptr_ret_default + ptr_ret_decided) > 0.30`
OR `ptr_args_decided / ptr_args_total < 0.05`, the agent has
defaulted too aggressively for the selection's size. Body-inspect
a random 10% sample of defaulted entries; if any single one would
be decidable by reading the function body, the broader run should
be repeated (or escalated to a phase-1b2 ownership pass) before
emission.

---

## 2026-06-02b — All composer-using analyzers — Agent-authored CodeQL queries iterate to convergence, not single-shot correctness

**Symptom.** Across the runs of CrustifySymbolAnalyzer (and to a
lesser extent CrustifyTypeAnalyzer), the same pattern recurs:
the agent reaches for a CodeQL query to drive a manifest
decision, writes a plausible-looking first version, and emits
output that is **schema-valid but factually wrong** because the
query had a subtle anti-pattern. Examples we've already paid for:

| Date | Anti-pattern | Cost |
|---|---|---|
| §2026-05-31  | `hasDefinition()` filter on outer `from Function fn` — drops decl-only externs | `CRYPTO_memcmp`, `OPENSSL_cleanse` and 11 others missing from `wrap/syms.json` until the query was restructured to start from `Call`. |
| (Earlier run) | `not exists(mi.getParentInvocation())` on macro-invocation discovery — drops every arg-position macro the caller-macro expands | 269 distinct constants including 118 `SSL_R_*`, 26 `ERR_R_*` / `ERR_LIB_*`, 3 `BIO_CTRL_*` silently dropped from `wrap/syms.json` until the parent filter was removed. Spawned the entire committed-pack-replaces-agent-queries refactor. |
| §2026-06-02a flag | No `edges/macro_body_idents.ql` for macro-body identifier references; agent re-tokenized the carried body string with regex | Marginal accuracy gap on `depends_on.syms` / `depends_on.types` for macros: lexical scan over body misses identifiers behind token-paste, picks up false positives from `do { … } while (0)` keywords, and can't distinguish a real identifier reference from a string-literal substring. |
| (Across runs) | `TypedefType` not `instanceof DerivedType` in this cpp-all version → naive `unwrappedUserType` doesn't descend typedef chains; agent's container-type predicate misses anonymous-base typedefs | Caught during composer development for `fields.ql`'s `containsAggregateType`; would have shipped wrong if not caught manually. |
| (Across runs) | `"__%"` LIKE pattern intended as a `__`-prefix filter — `_` is a single-char LIKE wildcard, so `__%` matches every string of length ≥ 2 | Caught immediately when the resulting `types.csv` had 0 rows. Would have been a silent over-filter at 1+ rows. |

**Root cause.** CodeQL's cpp-all library has many subtle
behaviours the agent cannot infer from the schema alone:

- `TypedefType` is parallel to `DerivedType`, not a subclass —
  `getBaseType()` chains must be walked explicitly for typedef
  hops.
- `MacroInvocation.getParentInvocation()` reflects expansion
  nesting, not call-site nesting — filtering on its absence
  drops every arg-of-an-expanding-macro.
- `Function.hasDefinition()` is project-extraction-dependent;
  declarations-only externs are first-class entities the
  reachability pipeline must surface.
- `Type.toString()` lossy-renders `RoutineType` as `..(*)(..)` —
  function-pointer typedef signatures aren't recoverable without
  walking `getReturnType()` + `getParameter(i).getType()`.
- LIKE-pattern `_` semantics in `String.matches()` make naive
  `__%` filters silent over-filters.

The agent reaches for a plausible idiom (`hasDefinition`,
`getParentInvocation`, `..(*)(..)`, `__%`), the query compiles
and returns rows, and only DOWNSTREAM does the missing-data
symptom surface — at which point we restructure the query and
backport the fix. The pattern is:

  1. Agent authors first-pass query, looks reasonable.
  2. Query compiles and returns plausible-looking row count.
  3. Schema-validation of emitted manifest passes.
  4. Downstream consumer (or human audit) notices missing /
     wrong entries.
  5. We restructure the query and backport into the committed
     pack.

The work-per-iteration is real (each iteration costs an analyzer
run, often $1–$10 in tokens plus engineering time to diagnose
and fix). At project scale we've iterated **at least 5 times**
through this cycle, each time on a different cpp-all subtlety.

**Where it came from.** Two reinforcing dynamics:

- **Plausibility ≠ correctness in CodeQL.** The QL language is
  expressive and the cpp-all library is large; many queries that
  look right return wrong data because of one obscure
  predicate semantic. The agent has no way to know which
  predicates are project-extraction-sensitive without testing.
- **No reference-implementation comparison.** The agent doesn't
  have a way to cross-check its query result against a
  ground-truth count (e.g. ripgrep on source). When it emits N
  rows, it has no signal that N should be 10× higher.

**Fix.** The architectural fix is the one we've been executing:

**Commit the queries to the pack** under `utils/codeql/` (entities
+ edges + composer Python) so the agent NEVER authors them
freelance. The committed pack:

1. Encodes the lessons of every past iteration as docstring +
   load-bearing predicate structure.
2. Validates by ground-truth comparison (ripgrep, spot-check on
   known-frequent symbols) during composer development, not
   per-run.
3. Reduces the agent's role to consuming pre-computed facts —
   the composer's output IS the agent's input, removing the
   class of bug entirely for that fact type.

We've committed 13 queries + 4 composer modules to date,
covering: function entities, macro entities, global entities,
type entities, field entities, file entities, function-call
edges, function-address edges, global-access edges,
macro-expansion edges, field-access edges, signature-type-use
edges, local-type-use edges, field-type-use edges,
global-type-use edges, function-pointer-arg edges, function-
pointer-return edges. The agent's residual freelance-QL surface
is now:

- Body-level inspection for op classification (type analyzer)
- Body-level inspection for ownership analysis (symbol analyzer
  — see §2026-06-02a)
- One-off audits the agent flags as "reusable gaps"

**For the residual cases**, the cycle continues to be
"first-pass agent query → audit → backport to pack". The flag
the agent surfaces in its run summary ("I had to do X with
regex / freelance QL; consider committing Y") is the
backport-input signal. Treat every such flag as a queued
composer-pack-extension task; the next caller of the same
data benefits.

**Self-check the agent should adopt.** When authoring a freelance
CodeQL query (the residual case where the committed pack doesn't
cover the needed fact):

1. **Cross-check the row count against ripgrep** on source.
   E.g. for a macro-body-identifier scan, count distinct
   identifiers via `rg -o '\b[A-Z][A-Z0-9_]+\b' <header> | sort -u
   | wc -l` and compare. If the QL row count is < 50% of the rg
   count, suspect an anti-pattern.
2. **Spot-check known-frequent symbols.** A query targeting
   wrap-side function discovery MUST find `OPENSSL_free`,
   `CRYPTO_malloc`, `ERR_raise`, `memcpy` if those are in
   project scope. If they're missing, restructure.
3. **Document the freelance query** in the agent's run summary
   with the QL text and the row count. The next person reviewing
   the run (human or model) can spot anti-patterns and queue a
   composer backport.
4. **Flag committable gaps explicitly.** If the freelance query
   would be useful for multiple entries / runs, surface it in
   the summary as "candidate for `edges/<name>.ql` in the
   committed pack" so the backport task gets queued.

The meta-lesson: the composer-pack-replaces-agent-queries
direction is the only sustainable architecture. Every freelance
QL line the agent writes is a future restructure waiting to
happen. The cost of that iteration is borne by every downstream
analyzer run until the backport lands.

---

## 2026-06-02c — Summarized thinking is a projection, not a complete record of the agent's reasoning

**Symptom.** After patching kiss to opt into
`thinking.display: "summarized"` on Claude Opus 4.7 (the model's
encrypted-by-default extended-thinking mode renders zero
`thinking_delta` events; see §2026-06-02b context), an audit of a
two-entry symbol analyzer run captured eight visible thinking
blocks bracketing the model's deliberation. On the
`SSL_get_session` entry, thinking block 5 concluded with:

> *"I'm weighing whether to mark the returned session as mutable.
> The get0 convention suggests read-only borrowing, but OpenSSL
> doesn't technically forbid calling mutating APIs on it. I think
> mutable=true is the safer choice since const=false and callers
> might legitimately invoke functions like
> SSL_SESSION_set_timeout, with a note explaining the get0
> semantics."*

The emitted manifest had `ptr_ret.mutable: false` for that exact
entry — the **opposite** of the conclusion the visible thinking
stated. Thinking blocks 6, 7, 8 (the only later blocks captured)
discussed implementation mechanics, JSON formatting, and final
verification respectively; none revisited the mutability
decision. The reversal happened in the unseen full reasoning the
summarizer compressed out.

**Root cause.** Anthropic's summarized thinking is generated by a
separate model from the one performing the reasoning. The
extended-thinking docs are explicit:

> *"Summarization is processed by a different model than the one
> you target in your requests. The thinking model does not see
> the summarized output. ... As Anthropic seeks to improve the
> extended thinking feature, summarization behavior is subject
> to change."*

The summarizer compresses the full reasoning trace into a
shorter readable form, prioritizing the visible deliberation arc
— the *"I'm weighing X, and Y might apply, Z is the safer
choice"* narrative. Late-stage decision revisions, where the
model considers an option, commits to it in narrative form, but
then changes course in the continuation that the summarizer
collapses, may not survive the compression. The full encrypted
thinking that drives the next turn DOES contain the resolution
(it has to, for billing-correct continuation); the summary
displayed to the client does not.

**Implication for trajectory audit.** The visible summary is a
useful debugging affordance:

- It SHOWS which alternatives the model considered.
- It SHOWS the model's pre-decision deliberation arc.
- It DOES NOT GUARANTEE that the final decision matches the
  verbalized conclusion.

Discrepancies between the summary and the output are not
necessarily bugs in the agent — they are an artifact of
summarization compression. For trajectory audit, treat thinking
summaries as one input among others (output JSON, source
spot-checks, downstream consumer feedback). When the summary
ends mid-deliberation with a tentative pick, the actual final
pick may differ.

**Where this lands relative to §2026-06-02a.** §2026-06-02a
flagged the structural fix of mandatory escalation flags — the
agent surfacing concerns in plain output text the orchestrator
can react to. Summarized thinking partially closes the
visibility gap that motivated this fix, but **does not fully
close it**: cases where the agent shortcuts AND its summary
conceals or contradicts the shortcut are not detectable through
trajectory inspection alone. Mandatory escalation flags remain a
separate, stronger lever — they cannot be summarized away
because they are in the model's output text, not its hidden
reasoning.

**Self-check the auditor should adopt.** When reviewing a
summarized thinking trajectory:

1. Read the FINAL output JSON for each entry under audit.
2. If the output contradicts the visible summary, that is a
   CHANGE-OF-MIND signature — the model resolved differently
   than the summary suggests. This is not necessarily wrong;
   verify against external ground truth (source code, project
   docs, established convention).
3. If the output silently matches name-pattern heuristics
   (e.g. every `_get0_*` returning `mutable=false`), that is a
   SHORTCUT signature — the model may have applied a rule
   without per-entity reasoning, and the summary may have
   compressed the rule application out of view. Cross-check
   semantic correctness.
4. Do not treat the summary as the agent's contract. The agent's
   contract is its emitted output. Use summaries to understand
   HOW the agent arrived; use the output to know WHAT it
   actually decided.

The meta-lesson: even with the strongest in-trajectory
visibility currently available, the agent's final emitted
output is the ground truth for what it claims, not the
trajectory leading to that output. Auditing rigor needs to
treat them as separate sources of evidence with separate failure
modes.

---

## 2026-06-05 — CrustifyTypeAnalyzer — Non-deterministic op attribution for embedded-base type pairs, aggravated by per-dir workload overload

**Symptom.** For the embedded-base pair `ssl_st` (the polymorphic
public `SSL` handle) and `ssl_connection_st` (the TLS subtype that
embeds `struct ssl_st ssl;` as its first member), the agent produced
**opposite op assignments on two runs with the same inputs**:

- Full `--all` parallel e2e (the `ssl/ssl_local` dir agent, ~21 structs
  in one job): `ssl_st` got **206** ops, `ssl_connection_st` got **0**,
  with a `_comment_agent` rationalizing the empty list as "avoiding
  duplication with `ssl_st`'s surface."
- Scoped rerun (`--name ssl_st ssl_connection_st`, just the 2 structs):
  `ssl_st` got **25** ops (exactly the `SSL_*` public API),
  `ssl_connection_st` got **441** (the internal handshake machinery),
  **0 op-list overlap** — the correct signature-subject partition.

Same model, same composer footprints, same manifest dir — different
outcome. The op attribution for this pair is a coin flip.

**Root cause.** Two compounding factors:

1. **Embedded-base footprint overlap.** Because `ssl_st` is `ssl_connection_st`'s
   first member, the ~187 internal functions that take `SSL_CONNECTION *`
   and read `s->ssl.<base_field>` register as field-users of **both**
   types. The two footprints overlap by **208 functions (97% of
   `ssl_st`'s footprint)**. The prompt's *field-access dominance* signal
   is confounded by this: `s->ssl.X` counts as an `ssl_st` access, so
   SSL_CONNECTION-functions look partly like `ssl_st` ops. The correct
   disambiguator is the **signature subject pointer** (`SSL_CONNECTION *`
   → `ssl_connection_st`), which the prompt does not call out for the
   embedded-base case. Lacking that rule, the agent improvised — and in
   the bail run, read the overlap as "duplication to avoid" and emptied
   the type, dropping not just the 187 shared functions but also the
   **254 `ssl_connection_st`-exclusive** ops that never touch `ssl_st`.

2. **Workload overload (suspected aggravator).** The bail happened in
   the full e2e, where one agent was handed the entire `ssl/ssl_local`
   manifest dir — ~21 structs including the largest, most complex
   layouts in the codebase (`ssl_st`, `ssl_connection_st`,
   `ssl_ctx_st`, … `ssl_connection_st` alone has 82 fields and a
   462-function footprint). Under that load the agent took the
   low-effort shortcut (empty + narrative comment). The scoped 2-struct
   run, with bandwidth to actually reason about the boundary, produced
   the correct partition. We **suspect the per-dir workload is the
   trigger** that turns a hard-but-tractable disambiguation into a
   shortcut: the bigger and more layout-heavy the batch, the more
   likely the agent satisfices instead of partitioning.

**Where it came from.** The orchestrator's per-dir parallelization
(`_run_analyze_parallel`) gives **one agent the whole manifest dir**,
regardless of how many structs or how complex their layouts/footprints
are. `ssl/ssl_local` is a worst case: many god-objects in one file. No
workload cap means the agent's effort budget is spread thin exactly
where the analysis is hardest.

**Mitigations (in order of leverage).**

1. **Self-receiver attribution (adopted §2026-06-05).** The structural
   fix is to make op attribution a **per-type, self-contained**
   question — "would this function become a method on `impl T`, i.e.
   is `T` its `self` subject?" — rather than a footprint-dominance
   comparison that needs a global view. A function belongs to whichever
   type it takes as its principal (`self`) argument; one that reaches a
   type only through an **embedded base/member** (`t->inner.<field>`)
   is the *container's* op, not the inner type's, and a field-toucher
   whose real subject is another type is dropped (in the port it uses
   `T`'s accessors). Because each agent reasons only about its own
   type, **isolated per-entry agents partition consistently with no
   coordination** — this is what makes the per-entry orchestration
   (TODO.md §2026-06-05, now the default for types) sound. It also
   removes the "duplication anxiety" that produced the empty-ops bail.
   See `type_analyzer.md` §7.
2. **Per-entry isolation can over-claim without (1).** Worth recording:
   per-entry processing *removes* the overload bail (each type gets a
   focused agent) but, under the OLD footprint-dominance rule, *creates*
   the opposite failure — the embedded-base overlap gets **double-
   claimed** because neither isolated agent sees the other's
   assignments. Observed in the 2026-06-05 e2e: `ssl_st` and
   `ssl_connection_st` op-lists overlapped by 63 functions (the
   base-typed `SSL_*` API on both). Self-receiver attribution (1) is
   what collapses that overlap to zero.
3. **Workload-weighted batching (structural, deferred).** Cap per-agent
   workload (field count and/or footprint size) and split an
   over-budget manifest dir into **sequential** sub-batches. Orthogonal
   to (1); see TODO.md §2026-06-05.

**Amendment (2026-06-06) — state the rule as declared-subject, both
directions.** The "principal `self` argument" in (1) is the function's
**declared principal subject** (the handle type in its C signature),
NOT which fields the body happens to reach. This cuts both ways and is
the disambiguator for the embedded-base boundary:

  - subject is a **derived** type (`f(SSL_CONNECTION *)`) → op of the
    derived, even when it reads base fields through the embedded
    `s->ssl` (the direction recorded in (1) above);
  - subject is the **base** handle (`f(SSL *)`) → op of the **base**,
    even when it downcasts (`SSL_CONNECTION_FROM_SSL`) to read derived
    fields. In wrap scope the base handle is opaque (the caller cannot
    safely downcast), so the public `SSL_*(SSL *)` surface becomes
    methods on the base that discriminate the variant at runtime;
    `&self`-elision binds a returned borrow correctly because the base
    handle's borrow spans the whole shared allocation (base + derived
    are one object).

A transient prompt/schema edit attributed the
base-handle-downcasts-to-derived case to the *derived* (judging by
field-reach rather than declared subject); that contradicted (1) and is
corrected here and in `type_analyzer.md` §7 / `types.json`
`_comment_polymorphic`. Net partition for the `ssl_st` /
`ssl_connection_st` pair: the ~25 public `SSL_*(SSL *)` functions are
`ssl_st` ops; the `SSL_CONNECTION *`-subject handshake machinery is
`ssl_connection_st`'s — disjoint, no double-claim.

**Audit signal.** A struct with a large `non_opaque_in` footprint but
`ops: []` and a narrative `_comment_agent` explaining the emptiness is a
bail signature — cross-check whether its ops were silently absorbed
into an embedded-base sibling, and whether its footprint-exclusive
functions were dropped.

---

## 2026-06-06 — CrustifyTypeAnalyzer — Embedded-base subobject misclassified `heap_allocated` instead of `embeddable`-only

> **Superseded (2026-06-13):** the `heap_allocated`/`stack_allocatable`/`embeddable`
> placement booleans this entry discusses are **gone** — replaced by the
> `dtor: {storage, fields}` split (types.json `_comment_lifecycle`,
> type_analyzer.md §3). The lesson maps forward unchanged: a polymorphic **base**
> has **no `dtor.storage` of its own** (the derived leaf is the allocation/free
> unit), so it carries `dtor: {storage: null, fields: null}` even though its
> handle is reached through a heap object. The misclassification below ("base
> marked heap-allocated") is now "base wrongly given a `dtor.storage`".

**Symptom.** `ssl_st` — the polymorphic public `SSL` handle, a
7-field base (`type`, `ctx`, `defltmeth`, `method`, `references`,
`lock`, `ex_data`; `ssl/ssl_local.h:1258`) — was emitted with the
placement profile:

```json
"heap_allocated": true, "stack_allocatable": false, "embeddable": true
```

This was **stable across all three isolation reruns and the prior
`--all` baseline** — not a variance coin-flip but a consistent *wrong*
answer. The profile is self-contradictory for consumers: `ssl_st` is
**never** the unit of a heap allocation. It is the first member, **by
value**, of `ssl_connection_st` (`ssl/ssl_local.h:1269` —
`struct ssl_st ssl;`) and likewise of the QUIC connection types; the
sole "constructor" path, `ossl_ssl_connection_new_int`
(`ssl/ssl_lib.c:725`), does `OPENSSL_zalloc(sizeof(*s))` for the
**container** `SSL_CONNECTION` and then `ssl = &s->ssl; return ssl;` —
handing back an *interior* pointer. There is no `sizeof(struct ssl_st)`
allocation site anywhere. `heap_allocated` should be **false**;
`embeddable: true` alone is the correct placement.

**Root cause.** The agent reasoned from the public API surface —
`SSL *SSL_new(...)` returns a heap `SSL*` that callers later
`SSL_free` — and attributed the **container's** heap allocation to the
**embedded base**. The lure is the offset-0 polymorphic-base idiom:
because `ssl_st` sits at offset 0 of its container, the downcast
`SSL_CONNECTION_FROM_SSL` (`include/internal/ssl_unwrap.h`) is a bare
pointer reinterpret `(SSL_CONNECTION *)(ssl)`, so an interior
`&container->ssl` pointer is indistinguishable, at the pointer level,
from a standalone heap object. The agent had no rule separating *"a
pointer to T is observed pointing into the heap"* (true — but it points
**into** a container) from *"T is itself the allocation unit"* (false).
`heap_allocated` was filled on the former, weaker, evidence.

This is the **placement-axis analog** of the op-attribution failure on
the very same pair in §2026-06-05: there the embedded-base overlap
confounded *which type owns a function*; here it confounds *whether the
base is independently allocated*. Both stem from the absence of an
explicit embedded-base rule in the prompt.

**Where it came from.** `heap_allocated` reads as "do heap instances of
this type exist?", and the answer *feels* yes (every live `SSL` is on
the heap). But the field's load-bearing meaning for the Rust mapping is
"is T the **allocation unit** — does it get its own `Box`/`CBox` and an
independent `Drop`?". For an embedded base the answer is no: in Rust it
is a **by-value field** of its container (`SslConnection { base: Ssl,
… }`), reachable only through the container, never independently boxed
or freed. Marking it `heap_allocated` would mislead the wrapper phase
into generating a standalone owning wrapper + `Drop` for a type that
must never have one.

**Fix.** Make `heap_allocated` mean *allocation-unit*, not
*pointer-points-to-heap*, and add an embedded-base placement rule to
`type_analyzer.md` (placement step):

> A struct that is the embedded (by-value) member of another struct —
> especially a first-member polymorphic base reached by an offset-0
> downcast — and that is **never the direct `sizeof(...)` operand of an
> allocator** (its "constructor" allocates a *container* and returns
> `&container->base`) is **`embeddable: true`, `heap_allocated: false`,
> `stack_allocatable: false`**. The container, not the base, is the
> allocation unit. In Rust the base is a by-value field of the
> container; it gets no independent `Box`/`Drop`.

`heap_allocated: true` requires a site whose allocation unit is **this
type's own tag** — `sizeof(T)` / `OPENSSL_zalloc(sizeof(*p))` where `p`
is a `T*`, or a `_new` that mallocs a fresh standalone `T`.

**Self-check the agent should adopt.** Before setting
`heap_allocated: true` on a struct `T`:

1. Find at least one allocation site whose unit is `T` itself —
   `sizeof(T)` / `sizeof(*p)` with `p : T*`, or a ctor returning a
   freshly-malloc'd standalone `T`. Grep is enough:
   `rg 'zalloc|malloc|calloc' | rg 'sizeof'` near `T`'s ctors.
2. If every "`T*`" a constructor returns is actually
   `&container->member` (interior pointer; the `sizeof` is the
   *container's*), then `T` is embedded-only → `heap_allocated: false`,
   `embeddable: true`.
3. Sanity gate: `heap_allocated` and `embeddable` both `true` is a
   smell for a *base* type — confirm a standalone-`T` allocation exists,
   else it's embeddable-only. (A type can legitimately be both only if
   it is genuinely allocated standalone in some sites *and* embedded in
   others; a first-member polymorphic base is not such a case.)

**Audit signal.** A small base struct (few fields, embedded first
member of larger types) marked `heap_allocated: true` whose only ctor
returns an interior `&container->field` pointer. Cross-check the
allocator's `sizeof` operand: if it is the container's, the base is
mis-flagged.


## 2026-06-07 — CrustifyTypeAnalyzer — Parallel agents can't see each other's writes: polymorphic hierarchies break symmetry + op-uniqueness across chains

Running `analyze types --parallel` on libgit2's ODB backend hierarchy
surfaced two cross-type inconsistencies that no single agent could
detect, because a polymorphic base and its derived leaves **always**
live in different manifest dirs (base in a public header, derived in
separate `.c` files) and therefore **always** run as concurrent chains.
Each agent's per-write dup-ops self-check only sees already-written
entries, never concurrent ones.

- **Broken poly-symmetry.** `git_odb_backend` (base) correctly listed
  `[pack_backend, loose_backend, memory_packer_db]` in `derived`, but
  the `memory_packer_db` agent left `polymorphic: null` — it missed the
  first-field `git_odb_backend parent` embedding that the loose/pack
  agents both caught. The base claims a child the child doesn't
  acknowledge.
- **Broken op-uniqueness.** Three functions were double-claimed:
  `git_mempack_new` / `git_odb__backend_loose` (upcast-ctors: allocate
  the derived, return the base — claimed by both base and derived) and
  `pack_backend__read_header` (a vtable-slot method the base agent
  grabbed *and* `pack_backend` correctly claims). Upcast-ctor sharing
  is benign (a single function legitimately serves as both the
  derived's ctor and the base's), but the plain-method collision is a
  real error.

**Root cause.** The parallel orchestrator's write-safety invariant
(no two concurrent agents share a manifest *path*) does not extend to
*cross-type* invariants. Polymorphic hierarchies are the worst case
because they are guaranteed to span manifest dirs.

**Mitigation — a post-parallel reconciliation gate**, not an agent
fix: `utils/codeql/compose/check_types_consistency.py` walks the whole
tree after the run and flags (a) base/derived links that disagree in
either direction or dangle, and (b) **non-lifecycle** ops claimed by
>1 type. Lifecycle ops — the union of every entry's `ctors`, `dtor`,
`up_ref`, `clone`, `locking.{acquire,release}` — are exempted from
op-uniqueness, which auto-accepts the benign upcast-ctor sharing while
still flagging the genuine method collision. Detect-only (non-zero
exit gate); resolution stays the agent's job. The cleaner long-term
fix is composer-side deterministic pre-fill of `polymorphic`
(first-field-embedding + offset-0 downcast is CodeQL-able), which would
prevent the symmetry miss at the source the same way composer pre-fill
fixed `depends_on.types[*].fields` and `linked_in`.

## 2026-06-19 — CrustifyTypeWrapper — Invented a dependency-ordering excuse for a raw `ffi::` exposure that was actually avoidable

When wrapping `git_hashmap_oid` (a generic khash family, **dag layer
L2**), the agent emitted the key-slot accessor as

```rust
pub fn keys(&self) -> Option<&[*const ffi::git_oid]> { … }
```

with the justification comment *"git_oid is not yet wrapped, so slots
are exposed as raw `*const ffi::git_oid`."* That premise was **false**:
`git_oid`'s wrapper `GitOid` was produced at **L1** — one layer *below*
— committed, and therefore present on disk in the L2 worktree's base.
`GitOid` is `#[repr(transparent)]` over the C `git_oid`, so the slots
could have been typed `*const GitOid` at zero layout cost (the
`keys()` accessor had no callers; the only `.keys()` in the tree is the
unrelated `GitPackOffsetmap`'s).

**Root cause.** An isolated wrap/port worktree forks from
`snapshot_base`, which holds the **merged output of every lower layer**.
So a dependency that sits at a *lower* dag layer than the unit being
wrapped is **guaranteed already on disk** — a "not yet wrapped" excuse
is categorically invalid for it. The rationale is only ever defensible
for a *same-layer* concurrent unit or a cross-cycle FAS back-edge
(where the dep's wrapper genuinely isn't merged yet). The agent reached
for a layer-ordering story to license a raw shortcut **without checking
the on-disk tree** (`scaffold --name GitOid` / grep for its
`define_type!` would have refuted it in one command).

**Why it's worse than the raw pointer.** A wrong *rationale* comment is
more corrosive than the shortcut it defends: it asserts a constraint
that doesn't exist, and it erodes trust in the agent's *other* SAFETY /
ownership justifications (a reviewer now has to re-verify claims that
read as authoritative). The naked-`ffi::T` itself is a routine
idiomaticity miss; the fabricated dependency excuse is the real defect.

**Mitigation.** (a) Prompt rule (`type_wrapper.md` / `symbol_wrapper.md`
`Pitfalls`): a "not yet wrapped" / dependency-ordering justification for
a raw `ffi::T` is only valid after confirming `T` has no `define_type!`
on disk; for a **lower-layer** `T` it is never valid (the worktree base
already holds it), so use the wrapper. (b) Deterministic detection: the
audit's `naked` metric already lists every raw `ffi::T` outside the
sanctioned region — cross-referencing each against an on-disk
`define_type!(…, ffi::T)` would flag exactly the avoidable ones (wrapper
exists but was bypassed) vs. the genuinely-unwrapped fallbacks. (c) This
instance was back-filled: `keys()` now returns `Option<&[*const GitOid]>`
(layout-sound retype; still raw *pointers* since slot occupancy lives in
the `flags` bitmap, so a safe `&GitOid` borrow can't be formed at the
buffer level — the flags-aware iterator is the eventual idiomatic API).

## 2026-06-20 — CrustifyTypeAnalyzer (allocator-cluster elems) — Macro-expanded khash allocations missing from a synthetic cluster's `elems`, so no `CVec` alias was scaffolded and the port fell back to a raw pointer

When porting `git_cache_oidmap__resize` (khash hashmap rehash, **dag layer
L4**, `odb/cache.rs`), the agent allocated the bucket flags buffer as a raw
`*mut u32`:

```rust
let mut new_flags: *mut u32 = core::ptr::null_mut();
...
new_flags = unsafe {
    ffi::git__reallocarray(core::ptr::null_mut(), nwords, core::mem::size_of::<u32>())
}.cast::<u32>();
...
unsafe { ffi::git__free(new_flags.cast::<c_void>()) };   // x2, on the OOM error paths
```

The `git__reallocarray` strategy `GitMallocarrayArray` already exists, so
`CVec<u32, GitMallocarrayArray>` was expressible — it would have turned the two
manual `git__free` error-path calls into a single RAII drop and given a typed
`&mut [u32]`. But there was **no `u32` alias to reach for**, and nothing in the
analysis flagged `u32` as a member of that cluster.

**Root cause — the elems set is blind to macro-expanded allocations.** The
synthetic allocator cluster `git__mallocarray_array` records its element types
in `types.json.elems`: `void *`, `wchar_t`, `struct object_entry *`,
`struct thread_params`, `trie_node`, `struct reftable_log_record`. The khash
`khint32_t` (u32) flags buffer is **absent** — because libgit2's hashmaps are
generated by the `__KHASH_IMPL` macro, and the `git__reallocarray(NULL, nwords,
sizeof(khint32_t))` callsite lives inside the macro *expansion*, which the
CodeQL elems-extraction does not walk. So the cluster under-reports every
macro-generated element type (the khash `keys`/`vals` arrays are hidden the same
way). The cascade is: missing `elems` entry -> no scaffolded `CVec<u32, …>`
alias -> the port agent has no idiomatic owning-buffer type to use -> raw
`*mut u32` + hand-rolled `git__free` (error-path RAII lost).

**Why it slips through review too.** The audit cannot flag the raw pointer
here: `git_cache_oidmap__resize` is a TU-local function whose C re-export is
`crustify_…`-prefixed (not a bare `#[no_mangle]`), so the wrap/port inference
mislabels it **wrap**, and a wrap body's raw pointers are (correctly, for a real
wrapper) not a smell — see the inference-blind-spot note. The raw buffer is thus
invisible from both ends: the analyzer never suggested the alias, and the audit
never flagged its absence.

**Mitigation.** (a) Extend the elems-extraction query to resolve macro-expanded
allocator callsites (khash `kcalloc`/`krealloc`/`kmalloc` -> `git__*`) so
cluster `elems` capture u32 flags + the keys/vals element types. (b) Manual
back-fill for the landed L4 instance: add `u32` (with a khash-flags note) to the
`git__mallocarray_array` `elems`, provide the `CVec<u32, GitMallocarrayArray>`
alias, and convert `new_flags` to it (RAII drop replaces the two error-path
frees; `into_raw_parts()` transfers the buffer to `h->flags` on success).
(c) The wrap/port inference fix (treat a `crustify_`-prefixed re-export + a
native body as a *port*) would make this class of raw buffer visible to the
audit going forward.
---

## 2026-06-20 — CrustifyPort — Self-matching `pgrep -f "make test"` wait-loop hangs the validation matrix forever

**Symptom.** A `port --dag-layer 0` run on `openssl/ssl/statem` appeared
to hang for over an hour in the §5 two-variant validation matrix. The
port agents themselves had finished (4 batch worktrees + merge produced,
9 units), but the run never advanced to merge/finish. Process inspection
showed no `make`, `gcc`, `cc1`, or `perl` running — the actual `make test`
had completed ~90 min earlier (`/tmp/test-off.log` static, last write at
the build's end) — yet a shell wait-loop was still spinning:

```sh
while pgrep -f "make test" >/dev/null 2>&1; do sleep 30; done
```

Killing it only made the agent re-emit the same shape:

```sh
for i in $(seq 1 60); do pgrep -f "make test" >/dev/null 2>&1 || break; sleep 30; done
```

**Root cause.** `pgrep -f` matches its pattern against each process's
**full command line**. The wait-loop's *own* command line contains the
literal string `make test` (inside the `pgrep -f "make test"` it runs),
so `pgrep` always finds **itself** (the loop's `sh -c …`), returns a live
PID, and the loop concludes the test is "still running" — forever. The
backgrounded `make test` it was waiting on had long since exited; the
loop was matching a ghost that is structurally guaranteed to exist for as
long as the loop runs. A second, independent defect compounded it: the
`make test` had run `Files=1, Tests=0, Result: NOTESTS`, i.e. it was
invoked from a per-unit **worktree** with no configured build tree rather
than the repo-root build that `build execute` had configured — so even on
a clean exit the equivalence check had nothing to compare.

**Where it came from.** The port prompt §5 says only "`build.json` build
+ test with the feature [un]defined"; it does **not** prescribe *how* to
run a long test command. Faced with a multi-minute `make test`, the agent
improvised a background-and-poll pattern and reached for the most obvious
liveness probe (`pgrep -f "make test"`) without realising the probe's own
argv satisfies its own match. The pattern is self-reinforcing: every
regeneration after a kill reproduces it, because the lure (poll for the
backgrounded job) and the obvious tool (`pgrep -f` on the command string)
are unchanged.

**Fix.** Not yet backported to the prompt (the candidate edit was reverted
pending review). The intended guidance: (a) run build/test commands **in
the foreground**, bounded by the `Bash` tool's own timeout — do not
background a test and poll for it; (b) if a liveness check is truly needed,
never `pgrep -f` a pattern that appears in the checking command's own argv
— match the real PID (`… | grep -v $$`, or capture `$!` of the backgrounded
job), or just run synchronously and read the exit code; (c) run the C
build+test from the **configured repo-root build**, not a fresh per-unit
worktree, or the suite reports `NOTESTS`.

**Self-check.** Before emitting any `pgrep -f "<pat>"` poll: does `<pat>`
appear verbatim in the command line doing the polling? If yes, it
self-matches — the loop cannot terminate. Cheap detection on a stalled
run: `pgrep -af "make test"` — if the only match is a `while`/`for` loop
(not an actual `make`/`perl`/`cc1`), the wait is matching itself.

---

## 2026-06-21 — CrustifyPort + interactive debug — Self-referential C type (`z_stream`) wrapped in by-value `CVal`: move after `inflateInit` breaks zlib's `state->strm` check; then mis-framed as a data race for hours; then **a careless `git checkout` deleted 19 uncommitted files**

This entry has three nested lessons, in increasing order of cost: a
**port defect** (the actual bug), a **diagnosis anti-pattern** (the wrong
mental model that burned the investigation budget), and a **destructive
recovery mistake** (working-tree files erased with no reflog backstop).
All three came out of one session on libgit2 W9 (the L9 pack-reader
batch).

### 1. The port defect — by-value `CVal<GitZstream>` moves a self-referential `z_stream`

**Symptom.** The `object::cache` test suite segfaulted ~100% of the time
under threads (`threadmania` / `fast_thread_rush`), in teardown. Reading
the lone *pack-only* object in `testrepo.git`
(`0266163a…`, a 51-byte non-delta blob — every other object the test
reads is also loose, so only this one exercises the Rust pack reader)
returned `GIT_ERROR_ZLIB "error inflating zlib stream"`.

**Root cause.** zlib ≥ 1.2.12 (runtime here **1.2.13**) makes `z_stream`
**self-referential**: `inflateInit` stores a `state->strm` back-pointer
into the inflate state, and `inflateStateCheck` rejects any later
`inflate()` whose `&z` no longer equals that stored address — returning
`Z_STREAM_ERROR`, **consuming 0 input, producing 0 output**. The ported
`packfile_unpack_compressed` (`pack/pack.rs`) built its stream with the
generated `GitZstream::init` constructor:

```rust
let s = Self::zeroed();
git_zstream_init(s.as_ptr(), direction);   // inflateInit binds state->strm = &s.z
Some(CVal::new(s))                          // CVal is #[repr(transparent)] INLINE → MOVES z
```

`CVal<T>` stores `T` by value, so returning `CVal::new(s)` relocates the
`z_stream`; `state->strm` now dangles at the pre-move address. The C
original keeps `git_zstream zstream` as a stack local that is **never
moved** from init to free. `odb_loose.rs` (ported earlier) already
documented this exact hazard and inits in-place — that is why loose reads
always worked and only the pack path broke. The deferred pack-reader
batch was the one site that reached for the by-value constructor.

**Fix.** Initialise the stream **in place** in its final slot, matching
the C stack-local and `odb_loose`'s established pattern:

```rust
let zstream = crustify::CVal::new(GitZstream::zeroed());   // placed first
git_zstream_init(zstream.as_ptr(), GitZstreamType::Inflate.to_raw());
// `zstream` is never moved afterwards → state->strm stays valid
```

Verified: `object::cache` went 25/25-crash → 25/25-pass at 6×6 threads
and 10/10 at the stock 50×20; `object`/`odb`/`pack` suites green.
`GitZstream::init` is now unused but remains a footgun — the **generator**
must never emit a by-value `CVal` init for a self-referential C type
(anything that stores `&self` after construction: zlib streams, types
with intrusive list nodes pointing at themselves, etc.). Such types are
effectively `Pin`-only; init must happen at the final address.

**Self-check (port agent).** Before wrapping a C type in by-value `CVal`
and running its C constructor *before* the wrapper reaches its final
resting place: does the constructor store a pointer to the object inside
the object (or inside heap state the object owns)? zlib `*Init`,
`*StateCheck`-guarded APIs, and any `x->self = x` / intrusive-node idiom
qualify. If so, init **in place** (`CVal::new(T::zeroed())` then C-init
through `.as_ptr()`), never `init()-then-return-by-value`. Grep the
constructor for `strm`, `->self`, `&` of the subject stored into a field.

### 2. The diagnosis anti-pattern — "threaded crash" ⇒ assumed "data race"

The crash only manifested **with** threads, so the investigation spent
hours on the data-race hypothesis: built ThreadSanitizer (fighting an
ASLR-disabled-by-sandbox `FATAL`, `setarch -R` blocked by seccomp),
re-ran ASan with Rust instrumentation, audited every lock
(`p->lock`, `p->mwf.lock`, `git_mwindow__mutex` symbol sharing),
and read every function on the path looking for a racing access. **TSan
correctly reported 0 races** — which was *true*, not a tooling gap. The
bug is **deterministic**: the inflate fails on *every* read of that
object, single-threaded included (the main-thread `cache_counts`
instrumentation showed the identical `out_rc=-1, consumed=0` failure).
Threads only changed the *crash signature*: a worker's `cl_git_pass`
failure does a cross-thread `longjmp`, derailing the worker into a
teardown use-after-free. Single-threaded it failed *gracefully* (test
failure, no segfault), which is why it read as "threaded-only".

**Lesson.** "Only crashes under concurrency" ≠ "is a data race." Threads
can merely change how a *deterministic* failure is *reported* (here:
graceful error-return vs. cross-thread `longjmp`-into-freed-memory).
Before committing to race tooling, **reproduce at the lowest thread count
that still fails and instrument the single-threaded path** — a print of
the actual failing values (`out_rc`, bytes-consumed, the input bytes vs.
the on-disk truth) located the root cause in three edits, after TSan/ASan
had found nothing. Reducing the test to 6×6 threads (from 50×20) made it
**more** reliable (25/25), not less — high contention was never the
trigger; *any* second reader was.

### 3. The destructive mistake — `git checkout` erased 19 uncommitted files

**What happened.** Mid-bisection, to test whether reverting the pack
reader fixed the crash, the working tree was reset with:

```sh
git checkout HEAD -- <paths…>
```

over a broad set of paths. Those paths held the **uncommitted** W9 port
output — 7 `odb/`, 4 `object/`, 8 `pack/` Rust files — none of it staged
or committed. `git checkout -- <path>` overwrites the working copy from
the index/HEAD **with no confirmation and no reflog entry**: unlike commit
rewrites, an obliterated *working-tree* file is not recoverable from
`git reflog`. 19 files of un-snapshotted work vanished instantly.

**Why recovery was possible anyway (this time).** The same content had,
earlier in the session, been part of a *merge* whose tip became a
**dangling commit** when the branch moved. `git fsck --lost-found`
surfaced it (`e82320a2d1…`), and `git checkout e82320a2d1 -- <files>`
restored every file; faithfulness was confirmed by re-reproducing the
crash 6/6. Pure luck that a dangling commit happened to carry the bytes —
had the work never been committed in any form, it was simply gone.

**Lesson / self-check.** `git checkout -- <path>`, `git restore <path>`,
`git stash` (drops untracked unless `-u`), and `git clean` are all
**working-tree destroyers with no reflog backstop**. Before running any of
them over paths that may hold uncommitted work:

1. **Snapshot first, always.** `git stash -u` or a throwaway
   `git add -A && git commit -m wip` (or even `cp -r` / `git diff >
   /tmp/x.patch`) costs seconds and converts an irreversible loss into a
   recoverable one. Do this *especially* during bisection, whose whole
   premise is repeatedly mutating the tree.
2. **Scope the revert to tracked-and-committed paths only.** Never hand a
   broad path set to `checkout`/`restore`/`clean` without first checking
   `git status --short` for `??` (untracked) and ` M`/`MM` (unstaged)
   entries in that set — those are exactly what gets destroyed.
3. **If it's already gone:** `git fsck --lost-found` for dangling commits/
   blobs (works only if the content was ever objectified — committed,
   stashed, or merged); editor swap/undo history; build artifacts that
   embedded the source. None of these is guaranteed — which is why (1) is
   the only real defense.

The meta-lesson tying all three together: the costly part of this session
was **not** the subtle zlib bug — it was (a) anchoring on the wrong
failure model and (b) mutating un-backed-up state to test hypotheses. A
single-threaded instrumented print and a `git stash -u` before the first
bisection step would each have saved hours.

---

## 2026-06-21b — CrustifyTypeWrapper — by-value `CVal` ctor for a struct embedding a `pthread_rwlock_t` — move-after-in-place-init, generalised from z_stream to POSIX sync objects

The §2026-06-21 z_stream entry is the *self-referential-back-pointer*
instance of this hazard. This is the **embedded-OS-primitive** instance —
broader, because the address-sensitivity is invisible at the Rust type
level (no `x->self = x` to grep for; the pin comes from inside libc).

**Symptom.** `GitCache::init` in `odb/cache_h.rs` was emitted as:

```rust
pub fn init() -> Option<crustify::CVal<Self>> {
    let cache = Self::zeroed();
    let rc = unsafe { ffi::git_cache_init(cache.as_ptr()) };  // memset + pthread_rwlock_init(&cache->lock)
    if rc != 0 { return None; }
    Some(crustify::CVal::new(cache))                          // MOVES the init'd rwlock to a new address
}
```

No crash observed — because the function has **zero callers** (caught by
inspection, not a failure). But it is unsound by construction.

**Root cause.** `git_cache` embeds `git_rwlock lock`, and this build is
`GIT_THREADS 1` / `GIT_THREADS_PTHREADS 1` (`git2_features.h`), so
`git_rwlock` is a real `pthread_rwlock_t` and `git_cache_init` runs
`pthread_rwlock_init` on it **in place** at the stack address
`cache.as_ptr()`. Then `CVal::new(cache)` consumes `cache` by value (and
the by-value return may move again — NRVO is not guaranteed), relocating
the freshly-initialised `pthread_rwlock_t` to a different address. POSIX
gives no guarantee that an initialised sync object is relocatable → UB.
The other embedded field, the khash `git_cache_oidmap map` (heap pointers
+ counts, no self-reference), *is* movable, so the lock is the sole
hazard. It "would have worked" on glibc/x86-64 only by accident: an
**unlocked, no-waiters** rwlock has no self-pointers and its futex words
aren't yet registered in any kernel wait-queue, so the byte-copy survives
— an implementation detail, not a contract.

**Where it came from.** The generator treated `git_cache` as a value type
and emitted the generic *"zeroed → C-init through `.as_ptr()` → wrap in
`CVal` → return"* constructor template. That template is correct for
trivially-movable POD aggregates but wrong for any type whose C
constructor initialises an **address-pinned** sub-object. Unlike z_stream
(where `inflateInit` writes a visible `state->strm` back-pointer), here
the pin lives entirely inside `pthread_rwlock_t`; nothing in `git_cache`'s
own layout reveals it. The lure is that the SAFETY comment even reasons
correctly about the *failure* path ("zeroed header with no initialised
rwlock has nothing to release") while missing the *success* path's move.

**Why it isn't biting, and what the port does right elsewhere.** In C,
`git_cache` is never a standalone value — it is the by-value first/inner
member of a parent and is init'd in place at the parent's final heap
address: `odb.c:388 git_cache_init(&db->own_cache)`,
`repository.c:313 git_cache_init(&repo->objects)`. The rest of the Rust
port mirrors exactly that: `GitOdb::cache(&self) -> &GitCache` reads
`own_cache` embedded by value via an `addr_of!` projection (`odb_h.rs`),
never constructing or moving a standalone cache. The `init() -> CVal<Self>`
ctor is the lone artifact that contradicts the embedded-in-place model.

**Fix.** A struct that embeds a `pthread_{mutex,rwlock,cond}_t` (or any
type whose C ctor binds an address) is **not** a movable `CVal` once
initialised — it is `Pin`-only, init at its final resting address. So:

1. **Don't emit `init() -> CVal<Self>` for such types.** Either drop the
   standalone ctor (mirror the embedded path — init at
   `&parent.own_cache` after the parent is placed), or make the ctor
   *init-in-place*: take the destination place / heap slot, `git_cache_init`
   there, and return a borrow — never "build a value, then move it."
2. **Classification rule:** a C type with an embedded OS sync primitive,
   or whose constructor stores any pointer into the object, must not be
   registered as a by-value-constructible `CVal`. It is in-place-init
   only (its movability ends at the first `*_init` call).

**Self-check (generator / type wrapper).** Before emitting a
`zeroed()→C-init→CVal::new→return` constructor for type `T`, check `T`'s
fields and its C constructor for an **address-pinned sub-object**:
`pthread_mutex_t` / `pthread_rwlock_t` / `pthread_cond_t` (directly or via
a `git_mutex`/`git_rwlock`/`git_cond` typedef under `GIT_THREADS`), a
`*_init` that takes `&field`, or any `field = &self` store. If present,
`T` is move-unsafe after init: emit an **in-place** initialiser at the
final address (the embedded-in-parent path), not a by-value `CVal` ctor.
Grep the C ctor for `pthread_`, `mutex_init`, `rwlock_init`, `cond_init`,
and `&` of the subject stored into a field — same probe as the z_stream
self-check, widened to OS primitives.

**Addendum (2026-06-21) — a codebase sweep found two more of the same, both
fixed.** Auditing every `CValued` type for the *(embeds an address-bound
sub-object)* × *(has a by-value `init()→CVal` that moves it)* shape turned up,
beyond `git_cache`:

- **`GitPackfileStream::open` (`pack/pack_h.rs`)** — `git_packfile_stream`
  embeds a `git_zstream zstream` (pack.h:160); `open()` ran
  `git_packfile_stream_open` (→ `inflateInit`, binds `zstream.state->strm` to
  the local's address) then `Some(CVal::new(stream))` **moved it**. This is the
  W9 z_stream corruption (§2026-06-21) verbatim, in a different type — its
  SAFETY comment reasoned about the failure path's zeroed zstream and missed the
  success-path move, the exact same blind spot. It is the embedded `idx->stream`
  field of `git_indexer` (and a stack local in `git_packfile_unpack`), so the
  fix is the in-place `open(&self) -> c_int` (mirroring C
  `git_packfile_stream_open(&idx->stream, …)`).
- **`GitPackCache::cache_init` (`pack/pack_h.rs`)** — `git_pack_cache` embeds a
  `git_mutex lock` (= `pthread_mutex_t`); `cache_init()` ran `git_mutex_init`
  in place then `CVal::new` moved it. It is the embedded `bases` field of
  `git_pack_file` (pack.h:131), so the fix is `cache_init(&self) -> c_int`
  (mirroring C `cache_init(&p->bases)`).

Also: **`GitZstream::init`** (the original W9 footgun) was deleted outright — its
callers already init in place, and a comment now documents why no by-value ctor
exists. All three were **dead** (no by-value caller wired), so the fixes were
non-breaking. Note every one of these is exactly what a primitive-level guard
(`CVal::new` bounded `T: CMovable`, address-bound `T` non-`CMovable` + `!Unpin`,
standalone heap address-bound built via `CBox::emplace`) would reject at compile
time — three real instances now argue for the primitive fix over case-by-case
review.
