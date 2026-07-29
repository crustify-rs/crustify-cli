/**
 * Enumerate every user-defined type appearing in a CALLBACK's signature.
 *
 * The callback sibling of `edges/signature_type_uses.ql`: where that query
 * ranges over `Function`s, this one ranges over **function-pointer typedefs**
 * (the `kind == "callback"` types — a `TypedefType` whose unwrap chain reaches
 * a `RoutineType`). One row per (callback, type-tag-in-signature) pair, the tag
 * reached by unwrapping pointers/arrays/qualifiers from the routine's return
 * type or any parameter type until a `UserType` is hit. Primitives are skipped.
 *
 * Drives `depends_on.types` on callback entries in `types.json` — the forward
 * edges the dag needs to order a callback wrapper AFTER its argument/return
 * type wrappers. (`used_by` on the callback comes from the inverse of the
 * function-side `signature_type_uses` — functions that consume the callback —
 * so it is intentionally NOT recomputed here.)
 *
 * # cols:
 *   callback_name       : the function-pointer typedef's C tag
 *   callback_def_file   : repository-relative path of the typedef declaration,
 *                         or "" if outside the source root
 *   type_name           : the user-defined type's C tag
 *   type_kind           : "struct" | "union" | "enum" | "typedef"
 *   type_def_file       : repository-relative path of the type's definition
 *                         site, or "" if no full-body definition is in the DB
 *   position            : "return" | "param_<i>" (first position; ties broken
 *                         return-before-params, low-index-first)
 */
import cpp

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

string tdDefFileOf(TypedefType td) { result = pathOf(td.getFile()) }

/**
 * The `RoutineType` a (function-pointer) typedef ultimately names. Walks both
 * `DerivedType.getBaseType()` (the pointer level) and `TypedefType.getBaseType()`
 * (typedef chains — `TypedefType` is not a `DerivedType` in this cpp-all
 * version), mirroring `entities/types.ql`'s `reachesRoutineType`. Yields nothing
 * for a non-callback typedef, so those produce no rows.
 */
RoutineType routineOf(Type t) {
  t instanceof RoutineType and result = t
  or
  result = routineOf(t.(DerivedType).getBaseType())
  or
  result = routineOf(t.(TypedefType).getBaseType())
}

/** As `signature_type_uses.ql`: stop only on a `UserType`. */
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

from TypedefType td, RoutineType rt, UserType t, string pos
where
  rt = routineOf(td) and
  (
    (reachableUserType(rt.getReturnType(), t) and pos = "return")
    or
    exists(int i |
      reachableUserType(rt.getParameterType(i), t) and
      pos = "param_" + i.toString()
    )
  ) and
  t.getName() != "" and
  kindOf(t) != "other" and
  // never a self-edge onto the callback typedef itself
  t != td
select td.getName() as callback_name,
       tdDefFileOf(td) as callback_def_file,
       t.getName() as type_name,
       kindOf(t) as type_kind,
       typeDefFileOf(t) as type_def_file,
       pos as position
