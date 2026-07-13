# THIS DOCUMENT IS OBSOLETE

The instructions, principles, guidelines in this document
should not be followed anymore.

---

# Hazards — UB, soundness gaps, borrow-checker frontiers

A catalogue of the failure modes the port engineers against. Each
entry names a concrete scenario, shows how it would manifest, explains
why the Rust abstract machine considers it UB (or why the type system
won't accept it), and points at the rule or primitive that prevents it.

This document is descriptive, not prescriptive — the *rules* live in
[`RULES.md`](./RULES.md); the *patterns* live in [`HELP.md`](./HELP.md);
the *allowlist* (what raw-pointer escapes are tolerated and why) lives
in `RULES.md` §"Raw-pointer allowlist". This file collects the *failure
modes* those documents exist to prevent.

## Soundness mental model

Two distinct classes of soundness concern recur. They look similar at
the source-code level but require entirely different remedies.

**Class 1 — Optimizer-driven UB.** Rust's abstract machine attaches
aliasing claims to references (`&T` is `noalias readonly`; `&mut T` is
`noalias`). When you hold a Rust reference into FFI memory and the C
side mutates the same bytes through any other path during the borrow,
the claim is violated. LLVM is then free to hoist, cache, or reorder
reads in ways that diverge from runtime reality. Symptoms: stale
values, optimization-dependent crashes, heisenbugs that vanish under
`-O0`. Fixed by: never holding a Rust reference into FFI memory across
an FFI call, except via value-load accessors.

**Class 2 — Ownership/borrow mismatch.** A C pointer field's *runtime
semantics* (owner vs borrower) don't line up with what Rust's type
system can statically express. The information lives in pointer
comparisons, invariants encoded in destructor logic, or tribal
knowledge. Symptoms: double-free, use-after-free, refcount drift.
Fixed by: choosing the right wrapper type (`CArc<T>`, `SelfPtr<'this, T>`,
`CList<T, ID>`, tagged enum) — or, when no layout-preserving wrapper
fits, accepting the runtime discipline as an allowlisted exception.

The classes are orthogonal. A given field may face one, the other,
both, or neither.

---

## Class 1 — Optimizer-driven UB

### H1.1 — Borrow synthesis over `(*p).field`

**Scenario.** A handler reaches into an FFI struct field via a `&`
or `&mut` borrow on a place expression rooted at `(*self.as_ptr())`.

```rust
// UB: synthesises &mut ffi::ssl_st briefly, even though the cast
// to *mut _ happens one expression later.
let p: *mut ffi::ssl_st = &mut (*self.as_ptr()).ssl as *mut _;
&*(p as *const Ssl)
```

**Why UB.** `&mut (*self.as_ptr()).ssl` materialises a `&mut ssl_st`
reference for the (very brief) lifetime of the expression. That
reference asserts exclusivity over the embedded `ssl_st` bytes,
which overlap the memory `&self` covers (since `ssl_st` is field 0
of `ssl_connection_st`). The Stacked / Tree Borrows model pops the
parent `&self` off the borrow stack while the `&mut` is alive; the
parent's tag is invalidated even after the `&mut` dies. LLVM also
gets a `noalias` claim it can't honour because the parent's
`*self.as_ptr()` reads may overlap.

**Prevention.** RULES.md §"Access discipline at the FFI boundary"
bans `&` / `&mut` on `(*p).field` place expressions, and further
mandates `addr_of!` / `addr_of_mut!` for the read and write itself
— bare `(*p).field` value-loads and direct assignments are also
disallowed. The only sound forms are:

```rust
let x = unsafe { core::ptr::addr_of!((*p).field).read() };
unsafe { core::ptr::addr_of_mut!((*p).field).write(v) };
```

**Greppable lint.** RULES.md provides
`grep -rnE '(^|[^&])&\s*(mut\s+)?\(\*…\.as_ptr\(\)\)\.'` — expected
zero hits.

---

### H1.2 — `&T` borrow held across an FFI call that mutates

**Scenario.** Code takes a sub-reference into an FFI struct, then
calls a C function that mutates the same memory through a different
pointer.

```rust
let key: &[u8; N] = &sess.master_key;   // borrow synthesised
unsafe {
    ffi::SSL_SESSION_set_master_key(sess.as_ptr() as *mut _, …);
}                                       // C memcpys into sess->master_key
let b = key[0];                          // UB
```

**Why UB.** `&[u8; N]` is `noalias readonly` for its lifetime. The
borrow lifetime extends across the FFI call. LLVM is told "nothing
else writes this memory until `key` dies," so `key[0]` may be
hoisted above the FFI call, cached at the borrow point, or reused
from a previously-loaded value. The actual write inside the C
function is invisible to the optimiser at this borrow.

**Prevention.** Don't hold a `&` or `&mut` borrow on FFI memory
across an FFI call. Accessors return values, not borrows; if a
borrow is needed (e.g. `&CStr` to a session hostname), the contract
is that the *caller* doesn't extend the borrow across mutations.
Documented per-field in the accessor's `SAFETY` comment.

---

### H1.3 — `Deref<Target = ffi::T>` returning naked references

**Scenario.** A wrapper implements `Deref` so users can write
`sess.field` instead of `sess.field()`.

```rust
impl Deref for SslSession {
    type Target = ffi::ssl_session_st;
    fn deref(&self) -> &Self::Target {
        unsafe { &*self.as_ptr() }
    }
}

// Caller:
let key = &sess.master_key;          // borrow extended via Deref
unsafe { ffi::SSL_SESSION_set_master_key(...); }
let b = key[0];                       // UB (same as H1.2)
```

**Why UB.** `Deref::deref` returns `&Self::Target`. Any sub-projection
inherits that borrow's lifetime. The `UnsafeCell<MaybeUninit<T>>`
in the wrapper's internal storage defeats `noalias` only for
`&UnsafeCell<T>` — once a "naked" `&ffi::T` is extracted via the
deref, the standard `noalias readonly` applies and H1.2 is back.

**Prevention.** Wrappers do **not** implement `Deref<Target = ffi::T>`
or `DerefMut`. RULES.md §"Access discipline" makes this explicit.
Reads go through per-field accessors returning values.

The nightly `!Freeze` trick could enable a sound `Deref` (by
suppressing the `noalias` claim at the type level), but it requires
pinning the whole project to nightly and is not chosen.

---

### H1.4 — Method-call autoref on FFI place

**Scenario.** Calling a `&self` method on an FFI place autorefs.

```rust
// (*sc.as_ptr()).method_taking_self()  — autoref makes &T to the place
```

**Why UB.** Method call autoref synthesises an `&T` (or `&mut T`)
implicitly. That reference carries the same aliasing claims as an
explicit `&(*p).field`, and H1.1/H1.2 apply.

**Prevention.** Don't call inherent methods directly on `(*p)`
places. Read out the value first or use a typed accessor.

---

### H1.5 — Long-lived borrow across a callback

**Scenario.** A handler borrows a field, calls a C function that
invokes a Rust callback, the callback mutates the same field, then
the handler resumes reading the borrow.

```rust
let alpn = sc.ext_alpn_slice();              // &[u8]
unsafe { ffi::SSL_do_handshake(sc.as_ptr()); }
                                              // C dispatches to a Rust callback
                                              // that writes to sc->ext.alpn
let len = alpn.len();                         // UB if the callback ran
```

**Why UB.** The Rust callback is *Rust code*. From the abstract
machine's perspective, the callback acquires `&mut SslConnection`
internally and may mutate. The outer `alpn` borrow was held during
the call; it's now stale. LLVM had no way to know the callback
would mutate; the optimisation assumptions were valid given the
borrow's `noalias` claim, but the runtime broke them.

**Prevention.** Same as H1.2 — don't hold borrows across FFI calls.
For callbacks specifically, the typical handler shape is "read into
locals, call FFI, read again after." Accessors that return values
make this idiomatic.

---

## Class 2 — Ownership / borrow mismatch

### H2.1 — Self-referential struct can't `&'a` itself

**Scenario.** A C struct embeds a substructure that holds a
back-pointer to the container.

```c
struct ssl_connection_st {
    record_layer_st rlayer;       // embedded
    /* ... */
};
struct record_layer_st {
    SSL_CONNECTION *s;            // points back to the container
    /* ... */
};
```

**Why the type system can't express it as `&'a`.** The lifetime
`'a` on `record_layer_st<'a>` would need to come from outside the
struct it's stored in — but its source is the lifetime of
`ssl_connection_st`, which contains the `record_layer_st`. This
closes a loop the type system explicitly forbids. You can't write a
constructor that satisfies the borrow checker.

**Prevention.** The principled fix is `crustify::SelfPtr<'this, T>` —
a `#[repr(transparent)]` wrapper over `*mut T` with a phantom
`'this` lifetime. Construction is `unsafe` (the human asserts the
target outlives `'this`); projection (`get(&self) -> &T`) materialises
a fresh borrow on demand, bounded by the parent's borrow. Layout-
compatible with the C field.

**Current status.** `crustify::SelfPtr<'this, T>` is implemented and
available. When a back-pointer or interior-pointer field is
encountered, apply `SelfPtr` directly — see
[RULES.md §"Structural pointers"](./RULES.md#structural-pointers--use-selfptrthis-t).
No allowlist entry is needed; this is a solved problem.

---

### H2.2 — Cyclic borrows (intrusive linked lists)

**Scenario.** Sibling sessions in the SSL_CTX cache hold mutual
`prev`/`next` pointers.

```c
struct ssl_session_st {
    SSL_SESSION *prev, *next;
    /* ... */
};
```

**Why the type system can't express it.** Rust's borrow checker
requires a *tree* of borrows. Each `&T` has a single source; the
graph of borrows is acyclic. Bidirectional or cyclic borrows
violate this invariant. Two sessions both holding `&` references to
each other can't satisfy any consistent lifetime assignment.

**Prevention.** `crustify::CList<T, ID>` + `CListNode<T, ID>`
(RFL-style intrusive list). The list owns all nodes; `prev`/`next`
are typed navigation pointers that don't model ownership.
Construction and removal are `unsafe`; iteration and read access
are safe.

**Current status.** `CList` is deferred (PIPELINE.md row 7). Cache
sibling fields stay raw until it lands.

---

### H2.3 — Runtime-determined lifetimes (cache membership)

**Scenario.** A session lives "until either it's evicted from the
cache or the cache itself is destroyed." Either event happens at
runtime.

**Why the type system can't express it.** Rust lifetimes are
compile-time annotations corresponding to lexical scopes or
explicit lifetime parameters bounded by them. "Until a runtime
event" has no compile-time name. `'static` over-approximates (the
session isn't immortal); any other lifetime needs a *source* the
compiler can verify.

**Prevention.** Either:
- Runtime-checked borrows via `Arc<T>` + `Weak<T>` — but changes
  layout, incompatible with C-shared structs.
- A purpose-built primitive (`SelfPtr<'this, T>` for the common
  case where the lifetime is tied to a parent struct) that *fakes*
  the lifetime with a phantom marker and an audited `unsafe`
  constructor.

**Current status.** Layout-preserving cache pointers are part of
the Category B migration target for the session cache subsystem.

---

### H2.4 — Runtime-discriminated owner-or-borrow

**Scenario.** A field is *sometimes* an owner (carries its own
refcount) and *sometimes* a borrow (aliases another field). The
example from PIPELINE.md row 8: `s3.tmp.pkey` either aliases one of
`ks_pkey[i]` (borrow) or owns its own pkey (separate refcount). The
C destructor walks `ks_pkey[]` first, NULLing `tmp.pkey` if it
aliases a slot, then frees `tmp.pkey` only if still non-null.

```c
for (i = 0; i < num_ks; i++)
    if (ks_pkey[i]) {
        if (tmp.pkey == ks_pkey[i]) tmp.pkey = NULL;  // alias break
        EVP_PKEY_free(ks_pkey[i]);
    }
if (tmp.pkey) EVP_PKEY_free(tmp.pkey);                 // only if not aliased
```

**Why no Rust primitive resolves it under Category A.** No layout-
compatible type captures "owner or borrow":
- `CArc<T>` always owns → silently up_ref's on alias (refcount
  drift) or double-frees at Drop.
- `SelfPtr<'this, T>` always borrows → can't represent the
  independent-owner case.
- Tagged enum (`Owned(CArc) | InArray(idx)`) statically
  discriminates, but changes byte layout (discriminant + payload).
  C reads break.
- Tagged low-bit pointer preserves byte count but requires the C
  side to mask the tag — too intrusive.

The discriminant lives in "which other field this pointer happens
to equal," which can't be a compile-time type without abandoning
layout sovereignty.

**Prevention.** Allowlisted as RULES.md §A1. The conversion path is
Category B migration of the containing sub-struct: once Rust owns
the layout, the tagged enum becomes the natural representation and
the runtime alias-check disappears.

**Hazard if "fixed" naively.** Wrapping `tmp.pkey` and the
`ks_pkey[]` slots both as `CArc<EvpPkey>` and clone-on-alias
produces refcount drift (2 instead of 1 during the aliased period),
behaviorally diverging from C even if memory-correct.

---

### H2.5 — Naive `Clone` introduces refcount drift

**Scenario.** Trying to fix H2.4 by always cloning the `CArc` when
aliasing.

```rust
// Pseudocode:
self.tmp.pkey = Some(self.ks_pkey[i].as_ref().unwrap().clone());
//                                                  ^^^^^^^ EVP_PKEY_up_ref
```

**Why it diverges from C.** OpenSSL's C code never up_refs in this
path — it just assigns the pointer. The C-observable refcount stays
at 1 during the handshake. The Rust clone-and-own approach raises
it to 2. Memory-correct (both `CArc`s drop, count reaches 0, one
free), but any code that observes the refcount sees the difference.

**Prevention.** Not a primitive — a design choice. The project
prefers C-bit-identical semantics, so the clone-and-own approach
is *not* chosen. The raw-pointer status quo (allowlist A1) stays
until Category B migration.

---

### H2.6 — Double-free via aliased `CArc` slots

**Scenario.** Storing the same pointer in two `CArc<T>` slots
without up_ref'ing.

```rust
// Imagine constructing in Rust without going through C:
unsafe {
    let arc1 = CArc::from_raw(ptr);   // takes the one refcount
    let arc2 = CArc::from_raw(ptr);   // takes "the one refcount" again
}
// Both drop on scope exit → EVP_PKEY_free called twice → use-after-free.
```

**Why UB.** Each `CArc::from_raw` consumes "one outstanding
refcount." Calling it twice on the same pointer with the same
refcount budget is a contract violation. Drop runs twice; the
second drop hits an already-freed allocation.

**Prevention.** `CArc::from_raw` is `unsafe`. The contract — "the
caller guarantees the pointer carries one unconsumed refcount" —
must be honoured at construction sites. RULES.md §"Method wrapper
policy" requires raw-pointer-receiving constructors to document
the contract in their SAFETY comments.

---

## Class 3 — Concurrency / synchronisation

### H3.1 — Atomic field accessed non-atomically

**Scenario.** Reading or writing a refcount or TSAN-qualified
counter via plain `(*p).field`.

```rust
unsafe { (*self.as_ptr()).references += 1; }   // BUG — should be atomic
```

**Why UB.** Refcounts in OpenSSL are manipulated via `CRYPTO_UP_REF`
/ `CRYPTO_DOWN_REF`, which use atomic primitives. A plain
non-atomic read/write races with concurrent atomic ops from C side;
the compiler is also free to fuse, hoist, or reorder non-atomic
accesses in ways that race with atomic mutations.

**Prevention.** Atomic fields are a hard exception per RULES.md
§"Access discipline" — use `addr_of!`/`addr_of_mut!` + the
appropriate atomic load/store, or (preferred) call the C accessor
function that uses the right primitive internally.

---

### H3.2 — Lock-guarded field accessed without lock

**Scenario.** Reading or writing a field that lives under
`SSL_CTX->lock` discipline (session cache hash buckets, stats
counters, etc.) without taking the lock.

**Why UB.** Concurrent C-side writers under the lock race with the
unprotected Rust-side reader. Even if Rust uses atomic ops, the
*coherence* invariants the C lock protects span multiple fields
that can't all be made atomic individually.

**Prevention.** Wrapper methods that touch lock-guarded state
either (a) take the lock internally for the scope of their access,
or (b) require the caller to attest that the lock is held (the
method is `pub unsafe fn` with the lock attestation as the SAFETY
contract). RULES.md §"Field accessor policy" Rule 1 + §"Access
discipline" cover this.

---

### H3.3 — C destructor re-entrance during Rust `Drop`

**Scenario.** Rust drops a `CArc<SslSession>`, which calls
`SSL_SESSION_free`, which (in some configurations) invokes a
finalizer callback, which reaches back into the `SslConnection`
that owned the session.

**Why it's a hazard.** Rust's `Drop` is allowed to run arbitrary
code, including FFI calls. If the FFI call re-enters Rust through a
callback, the callback's `&mut SslConnection` may overlap with the
ongoing `Drop`'s implicit `&mut Self`. Stacked / Tree Borrows
violation; potential aliasing UB.

**Prevention.** Per-type SAFETY comments on `impl Drop` document
what callbacks can fire and what re-entrance is permitted. In
practice for the port: most `*_free` paths in OpenSSL do not
trigger user callbacks; the few that do (BIO finalizers,
session finalizers) are flagged in their wrapper's documentation.

---

## Class 4 — Lifetime / pointer-validity hazards

### H4.1 — Drop order with co-allocated pointers

**Scenario.** Two `CArc<T>` fields in the same struct hold pointers
to the same allocation (without up_ref'ing). Field drop order
matters: the second drop hits a dangling pointer.

**Why UB.** The first `Drop` decrements the refcount to zero and
frees. The second `Drop` calls `EVP_PKEY_free` on the same (now-freed)
allocation.

**Prevention.** Don't wrap aliased pointers as separate `CArc`s
(this is H2.6 again). The deeper fix is H2.4's enumeration: model
the aliasing explicitly so Drop knows which slot owns and which
borrows.

---

### H4.2 — Use-after-free via stale sibling pointer

**Scenario.** A session is removed from the SSL_CTX cache and
freed, but a sibling session's `prev`/`next` still points at the
freed allocation.

**Why UB.** Dangling pointer. Following it via
`(*sibling.next).field` reads freed memory.

**Prevention.** Don't wrap intrusive list pointers as raw `*mut T`
in safe APIs; expose iteration only through a `CList<T, ID>`-style
wrapper that synchronises link maintenance with node removal.
Pending PIPELINE.md row 7.

---

## Cross-references

**Rules that prevent these hazards:**
- RULES.md §"Access discipline at the FFI boundary" — H1.1–H1.5
- RULES.md §"Field accessor policy" Rule 3 (atomic exception) — H3.1
- RULES.md §"Method wrapper policy" — H2.6 contracts
- RULES.md §"Raw-pointer allowlist" — H2.1, H2.2, H2.4 deferrals

**Patterns that resolve these hazards:**
- HELP.md §"FFI bridge pointer in `*-types` wrappers" — base layout
- HELP.md §"Buffer reservations and read windows" — H1.2 fix for
  buffer cursors
- HELP.md §"Scalar out-parameters — `COut`" — out-param shape

**Infrastructure that unblocks deferred fixes:**
- PIPELINE.md row 7 (`CList<T, ID>`) — H2.2, H4.2
- PIPELINE.md row 8 (`SelfPtr<'this, T>`) — H2.1, H2.3 (partial)
- Category B migration — H2.3 (full), H2.4

**Raw-pointer exposure — where it's tracked:**
- RULES.md §"Raw-pointer allowlist" §A1 — H2.4 (structural: blocked
  on Category B migration; raw is correct until then)
- RULES.md §"Raw-pointer allowlist" §A2 — out-param field addresses
  (structural: raw at the FFI seam by necessity)
- Unwritten wrap-only wrappers — H2.1, H2.2, H4.2, secret-material
  cleansing, and H1.2-class session sites. These are wrap-only types /
  accessors whose wrapper is simply unwritten — owed work, not
  tolerated exceptions.

---

## Maintenance

When a new hazard is identified during porting:

1. Reproduce or describe it with a concrete code example.
2. Classify it: optimizer (Class 1), type-system mismatch (Class 2),
   concurrency (Class 3), or lifetime (Class 4).
3. Add the entry here with the four-part shape: *Scenario*, *Why UB*,
   *Prevention*, *Current status*.
4. Update the cross-references section to point at the relevant
   rule, pattern, or allowlist entry.

When a primitive graduates from deferred to available (as `SelfPtr`
did), update the prevention guidance to say "use the new type" and
move the field tracking to the corresponding RULES.md allowlist entry.

When an allowlist entry is converted, mark the corresponding hazard
entry as historical (keep for context; note the resolution).
