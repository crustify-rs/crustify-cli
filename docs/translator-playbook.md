# Translator playbook

How to translate one scheduler-owned worklist. The scheduler chooses the
items, dependency order, objective and Rust homes. The translator supplies the
semantic judgement and code, then lands one verified commit. Follow
`conventions.md` for exact names, layout and anchors.

Optional prompt capabilities are listed in the agent's system prompt. Use the
ones present when their descriptions match the work. When no enabled capability
supplies an operation, inspect the source and use ordinary Rust and Cargo tools directly.

## Routes

The worklist declares one homogeneous route:

| route | items | translation responsibility |
|---|---|---|
| `type` | structs, unions, enums and type-generating macros | representation, lifecycle and field accessors |
| `symbol` | functions, globals and callback typedefs | safe call surface or native implementation |
| `raw-lifetime` | `void` or `string` lifetime tier | discover lifecycle primitives and emit reusable strategies |

Validate the declared route against the records you inspect. A type-generating
macro takes the `type` route even though its analysis node is a symbol. A
callback typedef takes the `symbol` route. Report a genuinely mixed or
misrouted worklist instead of translating it under the wrong contract.

## Common procedure

### Inspect the worklist

Read every item's semantic record and dependency closure using the enabled
analysis capability, when present, or by inspecting the C declarations,
definitions, callers and field touchers directly. Establish pointer ownership,
mutability, nullability, cardinality, type erasure and lifetime coupling.
For a pointer field, argument or return, inspect its complete codebase-wide
usage footprint. Follow relevant call paths far enough to identify every path
that stores, transfers, clones or frees it. Documentation and names are useful
evidence, but never override observed behaviour.

Encode ownership, borrowing, mutability, nullability, cardinality, type erasure
and keepalive relationships in the resulting Rust types and operations. Do not
erase a known distinction merely to reproduce a C signature.

When an enabled capability maintains agent-owned semantic findings, submit
missing ownership and lifecycle judgements through it and fix rejected or
inconsistent records before code generation. Never edit a derived analysis
artifact directly.

The scheduler guarantees that dependencies are in the worklist or already
translated, except explicitly cut SCC edges. Therefore, you should be able
to use safe Rust code for all your worklist's dependencies.

### Locate authored homes and bindings

Run:

```bash
crustify-cli <repo_root> <target> crates locate --name <worklist names...>
```

Use `--file <defined_in>` to disambiguate colliding names. The orchestrator has
already homed the items, created their `.rs` files and connected the modules.
Report a missing home. For a `raw-lifetime` marker, discover the concrete primitives
first, then home them by editing `crates.json`.

Homes are shared across batches. Use existing filled anchors as context; the
conventions define when one may be revisited.

Find the compiling `<lib>-sys` crate paired with the owning wrapper crate.
When the implementation needs a missing binding, extend its agent-owned
bindgen allowlist only for the worklist and required FFI items, then regenerate
bindings. Adjust bindgen inputs or shims only for a genuine missing binding or
bindgen limitation. Run the affected `-sys` crate's checks and tests, and
report allowlist/input changes so parallel landings can union them.

Generated headers and build objects may not exist in an isolated worktree.
Reconfigure or rebuild the C target there when necessary.

### Resolve macros

Do not reproduce a C macro as an independent Rust API. Resolve each use to the
semantic entity it denotes:

- For a symbol alias, inspect the expansion, add any missing underlying symbol
  to the worklist's bindgen allowlist and call its safe wrapper.
- For a function-like macro, use an existing `crustify_<NAME>` shim when
  present. Otherwise add the minimal shim to the bindgen input, allowlist it,
  regenerate bindings and wrap it like an ordinary FFI function.
- For a constant macro, use its generated binding.

Type-generating macros remain type-route items and follow the representation
procedure below.

### Apply the objective

Anything C can still observe preserves its ABI and any layout C observes.
Imported entities remain C-owned and receive safe wrappers; only
campaign-owned entities progress toward native Rust ownership.

In a port campaign limited to a user-selected subset, a targeted entity outside
that migration set may intentionally receive `wrap`. Treat the worklist
objective as authoritative: the inventory section says who owns the source,
not whether this particular campaign ports it. Such a wrapper is the explicit
boundary around the selected native subset, and its C implementation remains
in place.

`review` examines existing semantic findings and Rust code as an LLM judge.
Verify every ownership, lifetime and safety claim against the source; submit
corrected findings when that capability is enabled, and fix the Rust where the
claim or implementation is wrong.

`wrap` preserves the C ABI and emits a safe Rust surface over it. Raw pointers
belong only at the documented FFI seam or at an explicitly documented
higher-layer dependency whose wrapper does not yet exist. The C implementation
continues to own its storage and behaviour.

`port` emits safe native Rust while preserving behaviour and I/O equivalence.
Keep an interoperability wrapper instead for a primitive that C and Rust must
share during migration, such as a C allocator or destructor still used across
the boundary. Code and storage become fully Rust-owned only when they no longer
cross that boundary.

A `raw-lifetime` route uses `wrap` while discovering the requested untyped
lifecycle tier codebase-wide. The route selects discovery; the task objective
selects wrapping, and the campaign objective retains the surrounding scope. In
a wrap campaign, retain only primitives published on the API; in a port
campaign, include the targeted primitives needed by the selected migration.
Submit findings through the enabled semantic capability, then emit the
strategies described below. Under `review`, verify the existing findings and
strategies instead of rediscovering them as new work.

### Keep the boundary safe

Rust consumers use safe APIs. Translation progress is not a reason to expose a
raw pointer or unsafe operation publicly. Confine raw operations to
wrapped-layout accessors, FFI calls, C-ABI gateways and intrinsically unsafe
operations whose caller obligation cannot be expressed in the type system.

An SCC cut or unavailable higher-layer wrapper may create a temporary raw seam.
Keep it explicit, do not widen the public contract, and replace it when the
safe dependency becomes available. Every unsafe block follows the safety
comment convention.

## Type route

### Choose the wrapped surface

In a port campaign, expose fields touched by targeted symbols and find every
lifecycle primitive regardless of section. In a wrap campaign, expose fields
and lifecycle primitives published by the public API.

Confirm that a wrap objective still requires C layout compatibility. Pull or
derive the type's releasers, field disposers, cloners, constructors, casts and
pointer-field semantics before choosing a representation.

### Emit the representation

Represent the C layout with a safe wrapper abstraction supplied by an enabled
capability when possible. Otherwise hand-write the layout newtype and borrowed
handles under the same rule: never form a Rust reference to the wrapped C
object itself. Keep raw layout access inside small justified unsafe seams.

Use the canonical type and method names from the conventions. Borrowed handles
carry lifetimes while containing pointers; a reference to a handle covers
Rust-owned handle storage, never the C object.

For a synthetic type generator, inspect the cast topology:

- Use a generic parameterized wrapper when the item is the convergence point
  of a homogeneous family whose siblings erase to and from it.
- Alias a concrete monomorphized instance to the generator with its element
  wrapper when one generator dominates the instance.
- Add a concrete implementation only for behaviour that genuinely differs
  from the generic surface.

### Encode lifecycle

Implement the ownership and lifecycle contract proved by the type's findings.
Use stateless strategies when all drop/clone state is recoverable from the
object; use stateful ownership only when runtime state is genuinely external.
Prefer layout-compatible strategies and static monomorphization.

Emit each valid ownership variant for a type with several releasers. If the C
world no longer allocates or frees the storage, the wrapper may use the native
Rust allocator and its lifecycle primitives may be ported to native Rust.
Until then, keep targeted C lifecycle primitives available at the
interoperability seam.
Promoting construction-phase storage into a fully formed owner is an unsafe
operation: isolate it and prove that every invariant required by the owner now
holds.

### Emit field accessors

For each selected field, derive getters and setters from its ownership,
mutability, nullability, cardinality and lifetime record. Every pointer result
must be tied to the owner or state that keeps it alive.

Project fields with `addr_of!` / `addr_of_mut!` or `&raw const` / `&raw mut`.
Never use `&(*p).field` or `&mut (*p).field`. Reads originate from
the shared handle's pointer; writes originate from the mutable handle's
pointer. Raw-place projection avoids loading uninitialized storage or asserting
Rust aliasing over memory C may mutate.

For an owned-reference field, provide a setter that replaces and drops the old
owner, an owning getter that leaves the field valid, and a shared getter that
returns the dependent type's borrowed handle. For a by-value wrapped field,
return that type's shared or mutable handle over the projected place. Emit
additional variants when the field has several valid ownership or cardinality
contracts.
A setter that stores a borrowed reference must be unsafe when Rust cannot
express that the referent outlives the stored pointer; state that obligation in
its caller contract.

- When ownership versus borrowing is statically knowable, use distinct owned
  and borrowed wrapper forms with a shared trait for unambiguous accessors.
- When it depends on a runtime flag, expose separately checked owned and
  borrowed operations gated by that state.
- Represent union/discriminator pairs as tagged Rust enums where the mapping
  can be validated.
- Represent scalar-versus-array variants as separate statically typed forms.
- Parameterize type-erased fields when their concrete element type can be
  carried at compile time; otherwise bind them to the discovered untyped
  lifetime strategy.
- Add `Send` or `Sync` only with a specific safety proof, such as a guard or
  immutable-after-initialization invariant.

Use safe wrappers for already-translated dependent types and callbacks. For
strings, arrays and type-erased owners, identify their release strategy rather
than guessing from spelling.

Find lower-layer wrappers that temporarily mentioned this type as a raw
pointer because it did not yet exist. Update those sites to the new wrapper
without widening their public contract. A higher-layer dependency may remain
raw temporarily with a documented gap.

If an inline function-pointer helper has no ownership-compatible wrapper,
emit one with the type.

### Port layout and storage

Port the layout only after every remaining C-side field toucher is gone and no
public C consumer receives the concrete body. A public forward declaration
alone does not prevent opacification. Port field accessors and any lifecycle
primitives whose C implementation is no longer needed.

Port storage only after no remaining C path allocates or deallocates it. If C
still owns either side of that lifecycle, retain compatible storage and report
the blocker instead of moving it to Rust.

## Symbol route

### Functions and globals

Under each anchor, emit a `pub fn`, or `pub unsafe fn` only when no safe
contract can express the operation. Take and return typed ownership wrappers
at every boundary. Reconstruct raw pointers only at the FFI call and place the
call in a small unsafe block with the required safety comment.

Use a standard-library operation directly when it is equivalent and no C/Rust
interoperability contract requires a wrapper. For pointer arguments and
returns, encode moved, borrowed, mutable, nullable, scalar, array and
type-erased variants separately whenever one signature cannot state all valid
contracts.

Prefer thin stateless ownership for self-contained values. Carry state only
when destruction or cloning requires runtime information not recoverable from
the pointer. Use safe wrappers for translated dependencies; leave a documented
raw pointer only for an unavailable higher-layer wrapper.

Find lower-layer wrappers that referred to the new symbol's types or callback
surface raw and update them when the new safe contract makes that possible.

### Callbacks

A callback record is a C function-pointer typedef. Inspect its signature and
callsites, and emit a callable handle whose arguments and result use safe
wrappers. When callsites disagree on pointer ownership, emit one distinctly
named wrapper per recorded ownership distribution while sharing the underlying
C function-pointer type.

Wrap an inline function pointer when no ownership-compatible callable wrapper
already exists.

### Raw lifetime strategies

For every discovered `void` or `string` releaser, disposer or cloner, emit the
strategy type needed by owned pointers. Home it with the primitive's
translation unit. Do not also expose the primitive as an ordinary safe
function: consumers reach it through the lifecycle strategy.

### Port symbols

Translate the implementation to safe idiomatic Rust and preserve observable
behaviour. Re-export it to C under the established feature-gated ABI wiring
while C consumers remain. When a batch removes every C consumer of a formerly
TU-local re-export, remove it. Use the gateway, export and feature names from
the conventions. The raw gateway reconstructs safe wrappers and delegates to
its sibling native implementation.

Fence only the replaced C bodies with the path-derived
per-file guard, grouping adjacent bodies when useful rather than guarding the
whole file. In the C `#else` branch, declare each Rust export and redirect
TU-local names to their collision-safe exports.

Wire the file flag through the configured build so enabling it builds and
links the owning Rust static library and defines the per-file switch for the C
translation unit. Inspect the actual build pipeline rather than assuming its
link mechanics.

## Tests and completion

Write unit tests beside the emitted code. Exercise owned and borrowed forms,
mutable and shared access, scalar and array variants, lifecycle strategies,
generic instances and callback variants relevant to the worklist. Run both C
and Rust sides with the configured sanitizers when the test crosses the FFI
boundary, and fix double-free, use-after-free, invalid-free, leak and bounds
failures.

Replace every scheduler TODO with the filled anchor required by the
conventions. If a lifecycle strategy lives outside the operation's authored
home, replace the TODO with a thin cross-file reference and place the promoted
anchor at the real definition.

Run:

```bash
cargo check --workspace
cargo clippy --workspace
cargo test --workspace
```

If C sources changed, run the configured C build and baseline tests with the
Rust feature off. For a port objective, also run them with the feature on. A
wrap-only wave need not rebuild an unchanged C side.

Run every enabled deterministic safety-review capability according to its role
guidance. Fix an unsafe wrapper bypass or unsound reference; retain a necessary
FFI seam with its safety justification.

Commit one changeset in the worktree. Push it to the scheduler's local session
branch through `git rev-parse --git-common-dir`; on a non-fast-forward rejection,
rebase onto the session branch, revalidate and retry. Purge the worktree only
after the local landing succeeds. Never push an agent branch to a remote.
