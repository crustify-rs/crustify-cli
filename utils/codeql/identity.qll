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
