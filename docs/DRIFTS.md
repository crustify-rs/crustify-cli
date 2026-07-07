# Drifts

Catalogue of C language patterns and constructs whose semantics do NOT
map one-to-one onto Rust, so the wrapper / port must actively bridge the
gap rather than transliterate. Where `PITFALLS.md` records agent
mistakes, this file records genuine language-level mismatches: the C
idiom is correct and idiomatic, but a naive 1:1 Rust rendering is wrong
(a leak, a double-free, a lost invariant, or simply inexpressible).

Maintained for three reasons:

1. **Wrapper / port authoring.** When a construct drifts, the wrapper
   and port prompts point here for the canonical bridge, so the port
   composes the right Rust shape instead of transliterating the C.
2. **Analysis coverage.** Each drift names the analysis signal that
   flags it (a dtor role, a `casted` edge, a ptr flag). If a drift has
   no signal, that is a gap to close in the schema / composer.
3. **Review.** A human reading a produced wrapper can check that a
   known drift was bridged, not copied.

Entry conventions:

- Short anchor id (`D<N>`) + title.
- **Pattern (C)** - the C construct, concretely.
- **Rust semantics** - why it does not map.
- **Naive-port hazard** - what a 1:1 rendering breaks.
- **Bridge** - the Rust shape that restores the C contract.
- **Analysis signal** - what in the analysis tree marks the drift.
- **Example** - a real occurrence, with file:line.

---

## D1 - Field teardown split from storage free (no single `Drop`)

**Pattern (C).** A type's destruction is split across two operations: a
*field disposer* that releases the object's owned fields but leaves the
header allocation intact (`*_free` / `*_cleanup` / `*_dispose` taking the
object by pointer, with no free of the header itself), and a *storage
free* that releases the header allocation. The two may be either (a)
orchestrated by one public destructor that calls the disposer and then
frees storage, or (b) two separately callable functions the caller
sequences by hand.

**Rust semantics.** `Drop::drop` is a single method, run exactly once
when a value leaves scope. It cannot be split into "dispose the fields
now, free the storage later" as two independently invocable steps. C
separates them precisely to allow the header to be reused (dispose then
reinitialise in place) or storage to be managed independently of field
disposal - neither is expressible as one `Drop`.

**Naive-port hazard.** Binding only one of the two operations to `Drop`
and leaving the other for manual invocation silently does half the
teardown under automatic RAII: bind only the storage free -> the owned
fields leak; bind only the disposer -> the header allocation leaks (and a
later manual free is now a use-after-free / double-free). The user is
forced to remember a manual call that RAII was supposed to eliminate.

**Bridge.** Wrap the C object in a Rust newtype whose single `Drop`
invokes BOTH operations in the correct order - field disposer first, then
storage free - reconstituting the one-`Drop` contract. When C already
provides an orchestrating destructor that does both, bind `Drop` to that
alone. For an embedded / by-value header (disposer only -> `CVal`),
`Drop` calls the disposer; for a heap header (`CBox` / `CArc`), `Drop`
calls disposer-then-free, or the orchestrator when one exists.

**Analysis signal.** The dtor `{shared, exclusive, fields}` split names
exactly these roles: `fields` is the disposer, `shared` / `exclusive` is
the storage releaser. A type carrying both roles is the marker that the
wrapper generator must compose them in a single `Drop` rather than expose
either one.

**Example.** `SSL_free(SSL *s)` (ssl/ssl_lib.c) dispatches the field
disposer through the method vtable - `s->method->ssl_free` resolves to
`ossl_ssl_connection_free` (ssl/ssl_lib.c:1493), which frees the
connection's fields only (no `OPENSSL_free(s)`) - and then frees the
header itself with `OPENSSL_free(s)`. Here one public destructor
orchestrates both, so `Drop -> SSL_free` suffices; analysis still records
both roles (`dtor.shared = SSL_free`, `dtor.fields =
ossl_ssl_connection_free`) so the split is explicit. When the disposer
and the storage free are separate public functions instead, the wrapper
must sequence them itself.
