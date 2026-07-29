/**
 * Enumerate every user-defined type referenced from a global
 * variable's declared type.
 *
 * One row per (global, user_type) pair. Field-level analogue of
 * `edges/signature_type_uses.ql` and `edges/field_type_uses.ql` —
 * for each global variable in the database, walk its declared type
 * and emit a row for each user-defined type reached.
 *
 * Why this exists: a port-reachable wrap global whose type is
 * `SSL_METHOD *` (or `struct ssl_method_st *`) pulls `SSL_METHOD`
 * onto the boundary, even when no port-fn signature or body
 * mentions `SSL_METHOD` directly. The composer joins this query
 * against port-side `global_accesses` rows to close the gap.
 *
 * Both typedef aliases and their underlying user-types appear as
 * separate rows along the chain — same convention as the other
 * `*_type_uses.ql` queries.
 *
 * # cols:
 *   global_name      : the variable's C identifier
 *   global_def_file  : repository-relative path of the global's
 *                      definition site, or "" if no definition is
 *                      in the DB (declaration-only externs)
 *   type_name        : the user-defined type's C tag
 *   type_kind        : "struct" | "union" | "enum" | "typedef"
 *   type_def_file    : repository-relative path of the type's
 *                      definition site, or "" if no full-body
 *                      definition is in the DB
 *
 * Consumer: `utils/codeql/compose/types_manifest.py` — global-
 * driven reachability gate (scenario 7 in the reach ruleset).
 *
 * Function-local variables and parameters are NOT enumerated here —
 * local-variable type uses are covered by `local_type_uses.ql`
 * keyed on the enclosing function. This query is GlobalVariable-
 * scoped only.
 */
import cpp

/**
 * Repository-relative path, falling back to absolute for files outside the
 * source root (system/external headers) — keeps system entities' identity
 * consistent with the T1 entity CSVs.
 */
string pathOf(File f) {
  if exists(f.getRelativePath())
  then result = f.getRelativePath()
  else result = f.getAbsolutePath()
}

string defFileOf(GlobalVariable g) {
  if exists(g.getDefinition())
  then result = pathOf(g.getDefinition().getFile())
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

from GlobalVariable g, UserType t
where
  reachableUserType(g.getType(), t) and
  t.getName() != "" and
  not t.getName().prefix(1) = "(" and
  typeKindOf(t) != "other"
select g.getName() as global_name,
       defFileOf(g) as global_def_file,
       t.getName() as type_name,
       typeKindOf(t) as type_kind,
       typeDefFileOf(t) as type_def_file
