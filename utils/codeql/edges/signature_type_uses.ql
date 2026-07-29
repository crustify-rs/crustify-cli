/**
 * Enumerate every user-defined type appearing in a function's
 * signature.
 *
 * One row per (function, type-tag-in-signature) pair. "Appearing in
 * the signature" means the tag is reachable by unwrapping pointers,
 * arrays, and const/volatile qualifiers from the function's return
 * type or any of its parameter types until a `UserType` (struct,
 * union, enum, typedef) is hit. Primitive types
 * (`int`, `char *`, `void *`, `size_t`, etc.) are skipped — they
 * aren't user-defined and don't carry boundary semantics.
 *
 * Each `(function, type)` pair appears at most once, even if the
 * same type appears in both the return and a parameter, or in
 * multiple parameters. The `position` column records the FIRST
 * position the type was found at, breaking ties:
 *
 *   - "return"
 *   - "param_<i>" for the i-th parameter (0-indexed)
 *
 * Typedefs unwrap one level — if the parameter type is
 * `SSL_SESSION *`, both the typedef `SSL_SESSION` AND its underlying
 * struct `ssl_session_st` appear as separate rows. Consumers
 * reconcile typedef ↔ struct identity at composition time using
 * entities/types.ql.
 *
 * # cols:
 *   function_name       : function's C identifier
 *   function_def_file   : repository-relative path of the function's
 *                         definition site, or "" if no definition is
 *                         in the DB
 *   type_name           : the user-defined type's C tag
 *   type_kind           : "struct" | "union" | "enum" | "typedef"
 *   type_def_file       : repository-relative path of the type's
 *                         definition site, or "" if no full-body
 *                         definition is in the DB
 *   position            : "return" | "param_<i>"
 *
 * Consumer: CrustifySymbolAnalyzer for `depends_on.types.{port,wrap}`
 * population on function entries; CrustifyTypeAnalyzer for
 * inverted "which functions touch this type" lookup driving the op
 * candidate set.
 *
 * No `hasDefinition()` filter on the function — declaration-only
 * externs still carry signature types worth recording (a wrap-side
 * function whose body is missing still has a type surface that may
 * pull in user-defined types).
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

string kindOf(UserType t) {
  if t instanceof Struct then result = "struct"
  else if t instanceof Union then result = "union"
  else if t instanceof Enum then result = "enum"
  else if t instanceof TypedefType then result = "typedef"
  else result = "other"
}

string typeDefFileOf(UserType t) {
  if exists(t.getDefinition())
  then result = pathOf(t.getDefinition().getFile())
  else result = ""
}

string fnDefFileOf(Function fn) {
  if exists(fn.getDefinition())
  then result = pathOf(fn.getDefinition().getFile())
  else result = ""
}

/**
 * `t` is reached by unwrapping pointers, arrays, and qualifiers from
 * `outer`. We only stop on `UserType` — primitives are skipped at
 * the select.
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

from Function fn, UserType t, string pos
where
  (
    // Return type
    (reachableUserType(fn.getType(), t) and pos = "return")
    or
    // Parameter types
    exists(int i, Parameter p |
      p = fn.getParameter(i) and
      reachableUserType(p.getType(), t) and
      pos = "param_" + i.toString()
    )
  ) and
  // Skip anonymous tags (synthetic placeholder names like
  // `(unnamed enum)`) and the "other" UserType bucket. No `__`
  // prefix filter — compiler builtins are kept consistently with
  // entities/types.ql.
  t.getName() != "" and
  kindOf(t) != "other"
select fn.getName() as function_name,
       fnDefFileOf(fn) as function_def_file,
       t.getName() as type_name,
       kindOf(t) as type_kind,
       typeDefFileOf(t) as type_def_file,
       pos as position
