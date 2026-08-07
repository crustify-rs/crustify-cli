/**
 * Shared type-identity primitives for the crustify query pack.
 *
 * C gives an aggregate a name in two places, and CodeQL surfaces only
 * one of them on the `UserType` itself:
 *
 *   struct tag { ... };                    -> getName() = "tag"
 *   typedef struct { ... } T;              -> getName() = "(unnamed class/struct/union)"
 *
 * The second spelling is not rare in OpenSSL -- `PACKET`, `OSSL_TIME`,
 * `CLIENTHELLO_MSG`, the `OSSL_HPKE_*` family -- and `(unnamed ...)` is a
 * plausible-looking non-empty string, so a query that keys on `getName()`
 * collapses every such type into one bucket instead of failing loudly.
 *
 * Two DIFFERENT resolutions apply, and they are not interchangeable:
 *
 *   shape A   typedef struct { int x; } T;          identity = the typedef name
 *             `t.x`                                 found SIDEWAYS, via the alias chain
 *
 *   shape B   struct N { struct { int x; } inner; }; identity = the enclosing named
 *             `n.inner.x`                            struct, found OUTWARD via the
 *                                                    qualifier chain (see
 *                                                    `edges/fa_with_root.ql`)
 *
 * This module owns shape A and the primitives both shapes need. Shape B
 * stays in `fa_with_root.ql`, which is where the qualifier expression is in
 * scope. A query that must handle both composes them -- see
 * `edges/field_accesses.ql`.
 *
 * Deliberately primitives, NOT policy: `edges/casts.ql` drops anonymous tags
 * outright and `edges/field_type_uses.ql` attributes shape B to the owning
 * root under a qualified member path. Those divergences are intentional, so
 * this module exposes the building blocks and lets each query choose.
 */

import cpp

/**
 * Repository-relative path, falling back to absolute for files outside the
 * source root (system/external headers) -- keeps system entities' identity
 * consistent with the T1 entity CSVs.
 */
string pathOf(File f) {
  if exists(f.getRelativePath())
  then result = f.getRelativePath()
  else result = f.getAbsolutePath()
}

/**
 * `cpp-all` spells an unnamed aggregate `(unnamed class/struct/union)`; a
 * flattened anonymous member can also surface as `""`. Both mean "this type
 * carries no C tag of its own".
 */
bindingset[n]
predicate isAnonNamed(string n) { n = "" or n.matches("(unnamed%") }

/** `t` has no C tag of its own. */
predicate isAnonymous(UserType t) { isAnonNamed(t.getName()) }

/**
 * Strip `DerivedType` wrappers (pointer / cv-qualified / array) off `t` and
 * bind every `UserType` reached on the way down, including `t` itself.
 */
predicate unwrappedUserType(Type t, UserType b) {
  b = t
  or
  unwrappedUserType(t.(DerivedType).getBaseType(), b)
}

/**
 * Shape A: the typedef name that IS `t`'s identity, for an anonymous
 * aggregate declared inline as a typedef's underlying type.
 *
 * Mirrors the rule `entities/types.ql`'s `unaliasedKindOf` uses to stamp
 * `struct_anonymous` / `union_anonymous` / `enum_anonymous`, so an access
 * site resolves to exactly the name `types_manifest.py` adopted as the
 * entry's identity -- by construction rather than by coincidence.
 *
 * `min(...)` keeps this single-valued when several typedefs alias one
 * anonymous aggregate; the composer picks one identity per type, so the
 * access side must not multiply rows. Has NO result when `t` is named or
 * when no typedef aliases it (shape B) -- callers must supply a fallback.
 */
string typedefIdentityOf(UserType t) {
  isAnonymous(t) and
  result = min(TypedefType td |
      unwrappedUserType(td.getBaseType(), t) and not isAnonNamed(td.getName())
    |
      td.getName()
    )
}

/**
 * Total identity for a declaring aggregate: its own tag when it has one,
 * else its typedef name (shape A), else the unresolved placeholder.
 *
 * ALWAYS has a result, so a query selecting on it never silently drops a
 * row. A caller that can also resolve shape B should try that before
 * falling back here.
 */
string canonicalTypeName(UserType t) {
  if not isAnonymous(t)
  then result = t.getName()
  else
    if exists(typedefIdentityOf(t))
    then result = typedefIdentityOf(t)
    else result = t.getName()
}

/**
 * `canonicalTypeName` widened to any `Type`, for call sites that hold the
 * result of a qualifier walk (`Type`, not `UserType`). Non-aggregate types
 * pass their own name through unchanged. Also total.
 */
string canonicalNameOfType(Type t) {
  if t instanceof UserType
  then result = canonicalTypeName(t.(UserType))
  else result = t.getName()
}

/*
 * ---------------------------------------------------------------------------
 * Aggregate OWNERSHIP identity: which named entity a field belongs to.
 *
 * `canonicalTypeName` above answers "what is this type called". These answer
 * "which entity declares this field, and where is it defined" — the pair every
 * field-keyed table joins on. They lived in `entities/fields.ql` while
 * `edges/field_type_uses.ql` hand-rolled its own struct-side resolution, and
 * the two drifted: `fields.ql` emitted every shape-A (`typedef struct {…} T;`)
 * field, `field_type_uses.ql` emitted NONE of them — 213 of 214 structs with
 * no field-type edge at all were shape A, against 0 of the 373 that had them.
 * The type DAG reads the T2 table, so every one of those field edges was
 * missing from the graph. One definition, imported by both.
 * ---------------------------------------------------------------------------
 */

/** The typedef that gives an otherwise-anonymous aggregate its name. */
predicate namingTypedef(UserType anon, TypedefType td) {
  isAnonymous(anon) and
  (anon instanceof Struct or anon instanceof Union) and
  unwrappedUserType(td.getBaseType(), anon)
}

/** Definition-site path of an aggregate, or "" when only declared. */
string anonDefFileOf(UserType anon) {
  if exists(anon.(Struct).getDefinition())
  then result = pathOf(anon.(Struct).getDefinition().getFile())
  else
    if exists(anon.(Union).getDefinition())
    then result = pathOf(anon.(Union).getDefinition().getFile())
    else result = ""
}

/** Strip arrays and cv-qualifiers, but NOT pointers: a pointer to an
 * anonymous struct does not embed it. */
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
  isAnonymous(result) and
  (result instanceof Struct or result instanceof Union)
}

/**
 * `f` is reachable from `root` through ANONYMOUS embedded members; `path` is
 * the dotted access path (`ext.hostname`). Recursion stops at the first NAMED
 * aggregate — a member of named type is its own entity with its own edge.
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

/**
 * The manifest identity of an aggregate: its own tag when named, else the
 * typedef that names it. Single definition shared by every field-keyed query
 * so their struct sides cannot drift.
 */
predicate ownerOf(UserType t, string name, string file) {
  not isAnonymous(t) and
  name = t.getName() and
  file = anonDefFileOf(t)
  or
  exists(TypedefType td |
    namingTypedef(t, td) and
    not isAnonNamed(td.getName()) and
    name = td.getName() and
    file = anonDefFileOf(t)
  )
}

/** The anonymous aggregate a typedef names (`typedef struct {…} T;`). */
UserType anonBaseOf(TypedefType td) {
  unwrappedUserType(td.getBaseType(), result) and
  isAnonymous(result)
}

/**
 * The DEFINITION-site path of a type, the value every `*_def_file` column
 * carries. A typedef has no `getDefinition()` of its own, so shape A falls
 * through to the inline aggregate it names — that body site IS the type's
 * definition site.
 *
 * Shared because a `*_def_file` is half of every entity key: `entities/types.ql`
 * had this fallback and `edges/field_type_uses.ql`'s own `typeDefFileOf` did
 * not, so 1,133 field edges pointed at `(name, "")` while the node they meant
 * was keyed `(name, <path>)` — the target did not resolve and the edge died.
 */
string defFileOf(UserType t) {
  if exists(t.getDefinition())
  then result = pathOf(t.getDefinition().getFile())
  else
    if exists(anonBaseOf(t).getDefinition())
    then result = pathOf(anonBaseOf(t).getDefinition().getFile())
    else result = ""
}
