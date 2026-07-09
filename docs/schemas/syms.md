# syms.json schema

Field **meaning** for the per-stem `syms.json` manifests (produced by
`compose/syms_manifest.py`). This file is the single source of field semantics;
`crustify query syms --schema` emits it. The exact JSON shape an analyzer submits
-- and its validation rules -- is the *contract*, served separately by `crustify
query syms --update-help`, so meaning and shape never duplicate.

One entry per symbol (function, macro, global, or callback -- a function-pointer
typedef) whose definition -- or, when the symbol is never defined (a header
typedef/decl), its declaration -- lives in a file of this stem-group. Two entry
shapes share one schema: a BASE shape carried by every entry, plus an optional
PORT-SCOPE ADDITIONS layer (`used_by`, `depends_on`) the composer adds when the
entry's defining file is listed in `crustify/targets/<target>/scope.json`. There
is no separate wrap vs port template: a wrap entry is simply base-only.

Each `## <field>` section documents one record field; the heading name is the
field key.

## partition

Files are grouped into one manifest dir per `path_partition.manifest_dir_for(file)`
-- stem-grouped. `ssl/record/record.c` and `ssl/record/record.h` both land in
`analysis/ssl/record/record/`. System / external files (CodeQL reports these by
absolute path) route under `analysis/system/` (e.g. `/usr/include/string.h` ->
`analysis/system/usr/include/string/syms.json`).

## port_additions

The two port-scope fields (`used_by`, `depends_on`) are emitted only when the
entry's `defined_in` -- or `declared_in[0]` for declaration-only entries -- is
listed under `.port` in `crustify/targets/<target>/scope.json`. Wrap-scope
entries omit both. A macro's body is never emitted; the agent reads the
expansion from source when it needs to classify or port it.

## name

The C identifier exactly as written at the definition site. Composer-filled;
agents never edit it.

## kind

The symbol's category, and the field the CrustifySymbolAnalyzer agent is
responsible for finishing. For functions and globals the composer fills it
deterministically from CodeQL linkage: `function_exported`, `function_static`,
`function_inline_header`, `function_inline_tu`, `global_static`, `global_extern`.

For macros the composer can't know the subkind without reading the body, so it
emits the placeholder `"macro"`; the agent then refines it from the `#define`
body (read from source, per `prompts/analyzer/symbol_analyzer.md` Sec 4) into one
of four macro subkinds:

- **`macro_constant`** -- expands to a typed compile-time constant: a numeric,
  char, or string literal, an enum value, or a substitution chain ending in one.
- **`macro_symbol`** -- references one or more existing symbols (function calls
  or global reads/writes). Covers function-like wrappers and object-like aliases
  that resolve to a symbol. E.g. `OPENSSL_malloc` -> `CRYPTO_malloc(...)` and
  `OPENSSL_free` -> `CRYPTO_free(...)` are `macro_symbol`, NOT `macro_misc`.
- **`macro_typegen`** -- declares a typedef + struct + function family per
  instantiation, expanded at file scope: `DEFINE_STACK_OF(T)`,
  `DEFINE_LHASH_OF_EX(T)`.
- **`macro_misc`** -- everything else: token-paste utilities, header-guard
  sentinels, type aliases, pure-arithmetic expressions -- an expansion that
  yields no typed constant, references no symbol, and declares no type family.

The final enum is `function_{exported,static,inline_header,inline_tu}`,
`macro_{constant,symbol,typegen,misc}`, `global_{static,extern}`, `callback`.
Wrap output never carries the TU-bounded kinds `function_static`,
`function_inline_tu`, `global_static`.

**`callback`** -- a function-pointer typedef (CodeQL identifies it
deterministically: a typedef whose unwrap chain reaches a RoutineType).
Composer-filled kind; signature-shaped (carries `ptr_args` / `ptr_ret` /
`used_by.{call,ref}` / a signature `depends_on`, NO body). `defined_in` is null (a
header typedef). The agent fills ONLY its per-arg/return ownership (same ptr
rules as functions), inferring it from `used_by.call` (the invokers). When
invokers realize DIFFERENT ownership, the agent FORKS the callback: `--update`
splits it into multiple `kind:callback` entries, same name/type but distinct
`ptr_args`/`ptr_ret`, disambiguated by a `variant` index (0 = primary,
composer-emitted; >=1 = agent-created fork) and a partitioned `used_by.call`. One
entry = one Rust wrapper. The `variant` field is absent/0 for the common
single-contract case.

## declared_in

Sorted list of header files that declare or export the symbol. Composer-filled.

## defined_in

The single file holding the definition: `.c` for functions and globals, `.h` for
inline functions and macros. Null when the symbol is declared in the DB but never
defined. Composer-filled.

## type

The full C signature for functions, the declared C type for globals, and null
for macros (a macro has no type). Composer-filled.

## loc

Body line span (`endLine-startLine+1`) of a function's definition; 1 for a
global, 0 for a macro; 0 when absent (a pre-loc extraction). Composer-filled from
`functions.csv`. Feeds the port bin-packer's lines-of-code batch budget
(`config.PORT_MAX_LOC`), which binds together with the symbol-count cap.

## ptr_args

Per `function_*` entry; empty when the function has no pointer params, else one
record per pointer arg ordered by position (left-to-right).

COMPOSER fields (always set): `position`; `name` (param name as written, falling
back to `arg<N>`); `type` (verbatim innermost pointee type -- a user tag like
`EVP_PKEY`, a primitive like `char`/`void`, or the synthetic markers
`(routine)`/`(array)`); `const` (is the innermost pointee const-qualified?);
`depth` (1 for `T*`, 2 for `T**`, ...).

AGENT fields: `array` (buffer-style pointer?), `string` (NUL-terminated string?),
`moved` (does ownership transfer to the callee?), `borrowed` (does the callee
retain a borrow bound to another entity's lifetime?), `lifetime` (when
`borrowed=true`: `arg:<name>`, `arg:<name>->path`, `static`, or `other`),
`mutable` (null/true/false), `note` (free-form).

Ownership: `moved` and `borrowed` MAY BOTH be true -- that encodes
runtime-conditional ownership (owned on one path, borrowed on another);
`borrowed=true` requires `lifetime` set. Mutability rules: (i) `const=true` forces
`mutable=false`; (ii) port-scope entries -> `mutable=null` (the Rust author
decides); (iii) wrap-scope pointers to user-defined types -> `mutable=null`
(opaque-handle wrappers); (iv) wrap-scope buffers / out-scalars / `void*` -> agent
decides true or false.

## ptr_ret

Per `function_*` entry; null when the return type isn't a pointer, else a single
record. COMPOSER fields: `type`, `const`, `depth` (same semantics as `ptr_args`).
AGENT fields: `array`, `string`, `moved` (does the caller own the returned
pointer?), `borrowed` (is the return bound to another entity's lifetime?),
`mutable`, `lifetime` (when `borrowed=true`: `arg:<name>`, `arg:<name>->path`,
`static`, or `other`), `note`. Ownership: `moved` and `borrowed` MAY BOTH be true
-- runtime-conditional ownership; `borrowed=true` requires `lifetime` set.

## used_by

PORT-SCOPE addition; composer-filled (agents must not modify). `call` and `ref`
name the enclosing functions (or file paths for file-scope macro expansions) that
reach this entry. By kind: `function_*` -> `{call:[callers], ref:[addr-of users]}`,
where a site that both calls and takes the address is listed only under `call`;
`global_*` -> `{call:null, ref:[accessors]}`; `macro_*` -> `{call:[expansion
sites], ref:[]}`. The composer's default bucketing is fine for most kinds; the
analyzer may re-bucket `call`<->`ref` when a macro kind justifies it.

## depends_on

PORT-SCOPE addition; composer ground-truth (agents must not modify).
`depends_on.syms` is the forward callee/reference set: each record is `{name,
defined_in, declared_in}`, with `defined_in` nullable for externals (e.g. libc
functions not in the DB) -- resolve via `defined_in` first, else
`declared_in[0]`. `depends_on.types` is the forward type-use list: each record is
`{type, fields}`, where `type` is the canonical struct/union/enum tag (typedef
chains resolved) and `fields` are the accessed field names. The composer unions
signature types (parameters/return) with body-touched types from
`t2/field_accesses.csv`; signature types come first (signature order), body-only
types follow (first-encounter order); a signature type whose body touches no
field carries `fields:[]` (opaque use).

## provenance

Illustrative slice only; a real run emits hundreds-to-thousands of entries per
manifest dir depending on stem-group density.
