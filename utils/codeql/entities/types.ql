/**
 * Enumerate every user-defined type in the database.
 *
 * One row per `struct`, `union`, `enum`, and `typedef` tag with its
 * full-body definition location (if any), forward-declaration
 * locations, and — for typedefs — the immediate underlying
 * user-type name. Primitive / builtin types are not emitted — only
 * `UserType` and its subclasses (which excludes `int`, `char *`,
 * `size_t`, etc.).
 *
 * Tag identity: emit the C tag as written in source. A typedef and
 * its underlying struct produce TWO rows — `typedef struct ssl_session_st
 * SSL_SESSION;` produces one row for `ssl_session_st` (kind=struct)
 * and one for `SSL_SESSION` (kind=typedef, aliases=ssl_session_st).
 * Consumers walk the alias chain at composition time to attribute
 * the typedef's scope, opaqueness, and operations to the underlying
 * user type.
 *
 * Anonymous types (`struct { int x; }` with no tag) are skipped — they
 * have no stable identifier consumers can reference. No other name
 * filtering: compiler-builtin types (`__int128`, `__va_list_tag`,
 * etc.) are emitted; they classify cleanly as wrap-scope by the
 * standard definition-anchored rule (their def sites are in system
 * headers) and consumers can drop them at composition time if
 * desired.
 *
 * # cols:
 *   name        : the C tag as written in source
 *   kind        : "struct" | "union" | "enum" | "typedef"
 *   def_file    : repository-relative path of the full-body
 *                 definition site for struct/union/enum, OR the
 *                 typedef-declaration site for typedef; "" if no
 *                 full-body definition is in the DB (forward decls
 *                 only)
 *   decl_files  : pipe-separated list of repository-relative paths
 *                 of all declaration entries (forward decls + the
 *                 definition site); may be a single entry
 *   aliases     : for typedef rows — the C tag of the immediate
 *                 underlying user-type, after unwrapping a single
 *                 chain of pointers / arrays / qualifiers but BEFORE
 *                 following further typedefs. Empty when the
 *                 underlying chain terminates at a primitive
 *                 (`typedef int INT_T;`) or when no UserType is
 *                 reached. For non-typedef rows: empty. Consumers
 *                 walk this column transitively against this same
 *                 manifest to find the final underlying user type.
 *   unaliased_kind : carries kind information ONLY when `aliases`
 *                 is empty AND the row is a typedef — that is, the
 *                 cases where `aliases` is itself insufficient for
 *                 consumers. One of:
 *                   "struct_anonymous" / "union_anonymous" /
 *                   "enum_anonymous"  — typedef wraps an inline
 *                     anonymous struct/union/enum (the typedef name
 *                     IS the canonical identity — e.g.
 *                     `typedef struct { … } CLIENTHELLO_MSG;`)
 *                   "callback"        — typedef wraps a function-
 *                     pointer or function type (e.g.
 *                     `typedef int (*custom_ext_add_cb)(SSL *, …)`)
 *                   "primitive"       — typedef chain ends at a
 *                     `BuiltInType` (e.g. `typedef int FOO;`,
 *                     `typedef long size_t;` modulo glibc chain
 *                     length)
 *                 Empty for: typedef rows with a named alias (use
 *                 `aliases` for chain walking); all non-typedef
 *                 rows.
 *
 * Consumer: CrustifyTypeAnalyzer, for type enumeration,
 * `non_opaque_in` / `defined_in` resolution, and typedef → struct
 * scope reconciliation. CrustifySymbolAnalyzer indirectly via
 * `depends_on.types` tag normalisation.
 *
 * Consumer-side policy (NOT applied here):
 *
 *   Consumers building safe-wrapper layers (the type analyzer)
 *   typically filter to STRUCT-ROOTED entries — named structs plus
 *   typedefs whose `aliases` chain terminates at a struct (named or
 *   anonymous). Only structs have heap-allocatable shape, ownership
 *   semantics (ctor/dtor/up_ref/clone), and field-access surface
 *   that the safe-binding boundary needs to model. Enums, plain
 *   unions, scalar typedefs, and function-pointer typedefs flow
 *   through bindgen as `#[repr(C)]` value/alias types without
 *   analyzer modeling.
 *
 *   This filter is intentionally NOT baked into the query — non-
 *   type-analyzer consumers need the full entity set:
 *     - CrustifySymbolAnalyzer's `depends_on.types` tag
 *       normalisation needs enum/union tags appearing in
 *       signatures.
 *     - Future ABI compatibility / repr(C) flow-through checks
 *       need every UserType.
 *     - Opaque struct entries (`kind=struct AND def_file=""`)
 *       are part of the struct-rooted filter — they're typically
 *       the most important boundary entities (`BIGNUM`,
 *       `EVP_MD_CTX`, `BIO`) — but distinguishing them from
 *       value-type rows is a consumer-side join, not a query-side
 *       cut.
 *   The struct-rooted filter is applied at composition time
 *   (deterministic Python, NOT agent reasoning) — see
 *   `utils/codeql/compose/`.
 *
 * No `hasDefinition()` filter at the outer level — opaque types
 * (forward-declared with no full body in the DB, e.g.
 * `struct SSL_st;` exposed only as a pointer) are legitimate boundary
 * entities and must appear in the result set. Consumers distinguish
 * them by `def_file = ""`.
 */
import cpp
import identity

string declFilesOf(UserType t) {
  result = concat(File h |
    h = t.getADeclarationEntry().getFile()
  | pathOf(h), "|"
    order by pathOf(h)
  )
}

string kindOf(UserType t) {
  if t instanceof Struct then result = "struct"
  else if t instanceof Union then result = "union"
  else if t instanceof Enum then result = "enum"
  else if t instanceof TypedefType then result = "typedef"
  else result = "other"
}

/**
 * For typedef `td`, return the C tag of its immediate underlying
 * user-type (post-derived-unwrap). Empty when no underlying UserType
 * exists (e.g. `typedef int INT_T;`) OR when the underlying type is
 * anonymous (e.g. `typedef enum { … } STATE;` — cpp-all returns
 * synthetic names like `(unnamed enum)` for these, which are not
 * lookup-able tags; the typedef itself is the identity carrier and
 * consumers classify by the typedef's own def_file).
 */
string aliasOf(TypedefType td) {
  exists(UserType b |
    unwrappedUserType(td.getBaseType(), b) and
    not b.getName().prefix(1) = "(" and
    result = b.getName()
  )
  or
  not exists(UserType b |
    unwrappedUserType(td.getBaseType(), b) and
    not b.getName().prefix(1) = "("
  ) and
  result = ""
}

string aliasesColumnOf(UserType t) {
  if t instanceof TypedefType
  then result = aliasOf(t)
  else result = ""
}

/**
 * Holds if `t`'s unwrap chain reaches a `RoutineType` (a function
 * type — the carrier of function-pointer typedefs). Walks both
 * `DerivedType.getBaseType()` (pointers, arrays, qualifiers) AND
 * `TypedefType.getBaseType()` (typedef chains — load-bearing
 * because the chain may pass through inner typedefs before reaching
 * the routine, and `TypedefType` is NOT a subclass of `DerivedType`
 * in this cpp-all version).
 */
predicate reachesRoutineType(Type t) {
  t instanceof RoutineType
  or
  reachesRoutineType(t.(DerivedType).getBaseType())
  or
  reachesRoutineType(t.(TypedefType).getBaseType())
}

/**
 * For typedef rows whose `aliases` column ends up empty, classify
 * what's at the chain endpoint. Always has a result so consumers
 * don't lose rows; returns "" for typedefs with a named alias
 * (use `aliases` for the chain walk in that case).
 */
string unaliasedKindOf(TypedefType td) {
  if aliasOf(td) != ""
  then result = ""
  else (
    // Chain reaches an anonymous UserType (struct / union / enum
    // declared inline as the typedef's underlying type).
    exists(UserType b |
      unwrappedUserType(td.getBaseType(), b) and
      b.getName().prefix(1) = "(" |
      if b instanceof Struct then result = "struct_anonymous"
      else if b instanceof Union then result = "union_anonymous"
      else if b instanceof Enum then result = "enum_anonymous"
      else result = "other_anonymous"
    )
    or
    // No anonymous UserType reached; chain terminates at builtin or
    // routine. A function-pointer typedef classifies as "callback";
    // a primitive scalar typedef classifies as "primitive".
    not exists(UserType b |
      unwrappedUserType(td.getBaseType(), b) and
      b.getName().prefix(1) = "("
    ) and
    if reachesRoutineType(td.getBaseType())
    then result = "callback"
    else result = "primitive"
  )
}

string unaliasedKindColumnOf(UserType t) {
  if t instanceof TypedefType
  then result = unaliasedKindOf(t)
  else result = ""
}

from UserType t
where
  // Skip anonymous tags — no stable identifier for consumers.
  t.getName() != "" and
  // Keep struct / union / enum / typedef; skip class-template etc.
  // (which don't exist in C anyway but cpp-all hosts them).
  kindOf(t) != "other"
select t.getName() as name,
       kindOf(t) as kind,
       defFileOf(t) as def_file,
       declFilesOf(t) as decl_files,
       aliasesColumnOf(t) as aliases,
       unaliasedKindColumnOf(t) as unaliased_kind
