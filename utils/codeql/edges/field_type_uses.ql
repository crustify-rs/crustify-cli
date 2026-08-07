/**
 * Enumerate every user-defined type referenced from a struct/union
 * field's declared type.
 *
 * One row per (struct, field, user_type) triple. The pattern is
 * the field-level analogue of `edges/signature_type_uses.ql` — for
 * each field of every named struct or union, walk the field's type
 * (unwrapping pointers, arrays, qualifiers, and typedef aliases)
 * and emit a row for each user-defined type reached.
 *
 * Why this exists: the type-manifest composer needs to know which
 * user types a port-touched field exposes. Example: port code does
 * `s->session->cipher` — the `cipher` field of `ssl_session_st` has
 * type `SSL_CIPHER *`. To make the binding work on the Rust side,
 * `SSL_CIPHER` must appear in `wrap/types.json` (at least as an
 * opaque handle), even if no port-fn signature or body ever names
 * `SSL_CIPHER` directly. The composer joins this query against the
 * port-side `field_accesses` rows to discover such transitively-
 * reachable types.
 *
 * Both the typedef alias and its underlying user-type at every
 * chain step are emitted as separate rows (same convention as
 * signature_type_uses.ql) — consumers reconcile via the type-index
 * typedef walk.
 *
 * # cols:
 *   struct_name      : C tag of the declaring struct/union
 *   struct_def_file  : repository-relative path of the declaring
 *                      struct's definition site, or "" if no
 *                      full-body definition is in the DB
 *   field_name       : the field's C identifier
 *   type_name        : the user-defined type's C tag (struct tag,
 *                      typedef name, etc. — whatever cpp-all
 *                      surfaces at the chain step)
 *   type_kind        : "struct" | "union" | "enum" | "typedef"
 *   type_def_file    : repository-relative path of the type's
 *                      definition site, or "" if no full-body
 *                      definition is in the DB
 *
 * Consumer: `utils/codeql/compose/types_manifest.py` —
 * field-driven reachability gate (scenarios 5+6 in the reach
 * ruleset, per the design discussion).
 *
 * Identity on BOTH ends comes from `identity.qll`'s `canonicalTypeName`, the
 * same resolver `field_accesses.ql` / `fa_with_root.ql` use. An aggregate with
 * no tag of its own is named by its typedef (shape A) or by the named struct
 * that embeds it (shape B); only a genuinely unresolvable one is skipped.
 * Sharing the resolver is the point -- this query kept its own naming when the
 * access queries were fixed, and silently dropped every field-type edge of a
 * `typedef struct {…} T;`.
 */
import cpp
import identity

string structDefFileOf(Field f) {
  if exists(f.getDeclaringType().(Struct).getDefinition())
  then result = pathOf(f.getDeclaringType().(Struct).getDefinition().getFile())
  else if exists(f.getDeclaringType().(Union).getDefinition())
  then result = pathOf(f.getDeclaringType().(Union).getDefinition().getFile())
  else result = ""
}

string typeDefFileOf(UserType t) {
  if exists(t.getDefinition())
  then result = pathOf(t.getDefinition().getFile())
  else result = ""
}

string typeKindOf(UserType t) {
  if t instanceof Struct then result = "struct"
  else if t instanceof Union then result = "union"
  else if t instanceof Enum then result = "enum"
  else if t instanceof TypedefType then result = "typedef"
  else result = "other"
}

/**
 * Unwrap pointers, arrays, and qualifiers from `outer` and bind `t`
 * to every `UserType` reached along the way. Mirrors
 * `signature_type_uses.ql`'s `reachableUserType`.
 */
predicate reachableUserType(Type outer, UserType t) {
  outer = t
  or
  reachableUserType(outer.(DerivedType).getBaseType(), t)
  or
  // Descend INTO a function pointer's SIGNATURE. The `DerivedType` step above
  // already reaches the `RoutineType` itself, but a routine's parameter and
  // return types hang off `getAParameterType()` / `getReturnType()` — NOT
  // `getBaseType()` — so without these two disjuncts the walk dies at the
  // routine and every user type named by a bare (un-typedef'd) function
  // pointer is invisible. A typedef'd callback also benefits: the consumer
  // gains a direct edge to the types in the callback's signature, alongside
  // the indirect one through the callback's own symbol entry.
  reachableUserType(outer.(RoutineType).getReturnType(), t)
  or
  reachableUserType(outer.(RoutineType).getAParameterType(), t)
}

/**
 * The aggregate a field EMBEDS by value — arrays and cv-qualifiers unwrapped,
 * pointers NOT (a pointer to an anonymous struct does not contain it).
 * Mirrors `entities/fields.ql`.
 */
Type embeddedTypeOf(Type t) {
  if t instanceof ArrayType
  then result = embeddedTypeOf(t.(ArrayType).getBaseType())
  else
    if t instanceof SpecifiedType
    then result = embeddedTypeOf(t.(SpecifiedType).getBaseType())
    else result = t
}

/** The ANONYMOUS struct/union a field embeds by value, if any. */
UserType anonMemberAggregate(Field f) {
  result = embeddedTypeOf(f.getType()) and
  result.getName().prefix(1) = "(" and
  (result instanceof Struct or result instanceof Union)
}

/**
 * `f` is reachable from `root` through ANONYMOUS embedded members; `path` is
 * the dotted access path. Mirrors `entities/fields.ql` so the two agree on
 * which fields belong to which named struct.
 */
predicate anonEmbeddedField(UserType root, Field f, string path) {
  exists(Field outer |
    outer.getDeclaringType() = root and
    f.getDeclaringType() = anonMemberAggregate(outer) and
    path = outer.getName() + "." + f.getName()
  )
  or
  exists(Field outer, string sub |
    outer.getDeclaringType() = root and
    anonEmbeddedField(anonMemberAggregate(outer), f, sub) and
    path = outer.getName() + "." + sub
  )
}

string aggDefFileOf(UserType u) {
  if exists(u.(Struct).getDefinition())
  then result = pathOf(u.(Struct).getDefinition().getFile())
  else
    if exists(u.(Union).getDefinition())
    then result = pathOf(u.(Union).getDefinition().getFile())
    else result = ""
}

from Field f, UserType t, string struct_name, string struct_def_file, string field_name
where
  reachableUserType(f.getType(), t) and
  not isAnonNamed(canonicalTypeName(t)) and
  typeKindOf(t) != "other" and
  (
    // Field of a struct/union that HAS an identity of its own -- its tag, or
    // for shape A (`typedef struct {…} T;`) the naming typedef, resolved
    // SIDEWAYS through the alias chain by `canonicalTypeName`. Without that
    // resolution an anonymous typedef'd struct matched neither this disjunct
    // nor the embedded one below, and every one of its field-type edges was
    // dropped: `CLIENTHELLO_MSG` produced ZERO rows while the manifest knew it
    // reached `PACKET` and `raw_extension_st`.
    not isAnonNamed(canonicalTypeName(f.getDeclaringType())) and
    struct_name = canonicalTypeName(f.getDeclaringType()) and
    struct_def_file = structDefFileOf(f) and
    field_name = f.getName()
    or
    // Field of an ANONYMOUS aggregate embedded by value: its type is a
    // dependency of the OWNING named struct, recorded under the qualified
    // member path (`ext.hostname`). Without this the type edge was attributed
    // to `(unnamed …)` and dropped.
    exists(UserType root |
      not isAnonNamed(canonicalTypeName(root)) and
      anonEmbeddedField(root, f, field_name) and
      struct_name = canonicalTypeName(root) and
      struct_def_file = aggDefFileOf(root)
    )
  )
select struct_name,
       struct_def_file,
       field_name,
       canonicalTypeName(t) as type_name,
       typeKindOf(t) as type_kind,
       typeDefFileOf(t) as type_def_file
