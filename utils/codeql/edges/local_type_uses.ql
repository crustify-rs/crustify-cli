/**
 * Enumerate every direct mention of a user-defined type inside a
 * function BODY that ISN'T captured by the function's signature.
 *
 * The three forms a function can mention a type without exposing it
 * in its signature:
 *
 *   - `local_var` : a local variable declared with the type
 *                   (`SSL_CONNECTION *s;`, `EVP_MD_CTX ctx;`)
 *   - `cast`      : a C-style or static cast TO the type
 *                   (`(SSL *)p`, `(struct ssl_st *)x`)
 *   - `sizeof`    : a `sizeof(T)` expression where T is the type
 *
 * Signature uses (return type + parameter types) are handled by
 * edges/signature_type_uses.ql — this query covers the body-side
 * mentions the signature query misses. Together they enumerate
 * every function that "mentions" a type in any form, which is what
 * `opaque_in` minus `non_opaque_in` partitioning requires
 * (see `utils/codeql/compose/types_manifest.py`).
 *
 * Pointer / array / qualifier wrapping is unwrapped to find the
 * underlying UserType — `T *x` records `T`, not the pointer type.
 * Typedef chains are followed because cpp-all's `TypedefType` is a
 * `DerivedType`, so the unwrap predicate descends through them and
 * surfaces the eventual struct/union/enum/typedef at the chain's
 * end. A function that mentions `SSL_CONNECTION` in a local
 * declaration therefore emits rows for both the typedef
 * `SSL_CONNECTION` and the underlying struct `ssl_connection_st`,
 * just as `signature_type_uses.ql` does — the consumer reconciles
 * via the type-index typedef walk.
 *
 * One row per occurrence — a function with three local
 * `SSL_CONNECTION *` declarations emits three local_var rows.
 * Consumers dedup by (function, type) when computing opaque-vs-
 * non-opaque partitioning.
 *
 * # cols:
 *   function_name      : enclosing function's C identifier
 *   function_def_file  : repository-relative path of the function's
 *                        definition site, or "" if no definition is
 *                        in the DB
 *   type_name          : the user-defined type's C tag
 *   type_kind          : "struct" | "union" | "enum" | "typedef"
 *   type_def_file      : repository-relative path of the type's
 *                        definition site, or "" if no full-body
 *                        definition is in the DB
 *   use_kind           : "local_var" | "cast" | "sizeof"
 *   use_line           : 1-indexed line number of the mention
 *
 * Consumer: types_manifest.py composer — joins with
 * signature_type_uses.ql + field_accesses.ql to populate
 * `opaque_in` on type manifest entries.
 *
 * No anonymous-tag filtering on the type (rows for
 * `(unnamed enum)`, `(unnamed class/struct/union)` are kept for
 * consistency with entities/types.ql; consumers can drop them).
 * `kindOf` "other" bucket is filtered — those are class-template /
 * non-C UserType subclasses that have no port/wrap analogue in
 * pure C codebases.
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

string fnDefFileOf(Function fn) {
  if exists(fn.getDefinition())
  then result = pathOf(fn.getDefinition().getFile())
  else result = ""
}

string typeDefFileOf(UserType t) {
  if exists(t.getDefinition())
  then result = pathOf(t.getDefinition().getFile())
  else result = ""
}

/**
 * Unwrap pointers, arrays, and qualifiers from `outer` to find the
 * underlying `UserType`. `DerivedType` includes `TypedefType` in
 * cpp-all, so typedef chains are descended automatically — the
 * predicate yields the chain's terminal `UserType` AND each
 * intermediate typedef along the way, so consumers see every name
 * the source might have used.
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

from Function fn, UserType t, string kind_str, int line_num
where
  (
    // Local variable declared with this type.
    exists(LocalVariable lv |
      lv.getFunction() = fn and
      reachableUserType(lv.getType(), t) and
      kind_str = "local_var" and
      line_num = lv.getLocation().getStartLine()
    )
    or
    // Cast TO this type.
    exists(Cast c |
      c.getEnclosingFunction() = fn and
      reachableUserType(c.getType(), t) and
      kind_str = "cast" and
      line_num = c.getLocation().getStartLine()
    )
    or
    // sizeof(T) where T is the type.
    exists(SizeofTypeOperator s |
      s.getEnclosingFunction() = fn and
      reachableUserType(s.getTypeOperand(), t) and
      kind_str = "sizeof" and
      line_num = s.getLocation().getStartLine()
    )
  ) and
  t.getName() != "" and
  kindOf(t) != "other"
select fn.getName() as function_name,
       fnDefFileOf(fn) as function_def_file,
       t.getName() as type_name,
       kindOf(t) as type_kind,
       typeDefFileOf(t) as type_def_file,
       kind_str as use_kind,
       line_num as use_line
