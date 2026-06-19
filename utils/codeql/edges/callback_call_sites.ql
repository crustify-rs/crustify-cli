/**
 * Enumerate the **invocation sites** of every callback typedef.
 *
 * One row per (callback, enclosing-function) where the function's body contains
 * an indirect call (`ExprCall`) whose callee expression's type denotes that
 * function-pointer typedef. This is the subset of a callback's consumers that
 * actually **call** it (where the borrow/own contract on its arg/return
 * pointers is realized), as opposed to the pass-through sites in
 * `signature_type_uses` that merely declare/forward the pointer.
 *
 * Purely syntactic on each call site's own body — `call.getExpr().getType()`
 * resolving to the typedef — so a callback passed A→B→struct-field→indirect-call
 * is still pinned to the function that performs the final call, with no
 * points-to / indirect-call *target* resolution (which would be undecidable).
 *
 * Note: not a strict subset of `signature_type_uses` — an invoker that obtains
 * the callback via a field/cast (without naming the typedef in its own
 * signature) appears here but not there.
 *
 * # cols:
 *   callback_name      : the function-pointer typedef's C tag
 *   callback_def_file  : repository-relative path of the typedef declaration
 *   callsite_name      : the function performing the call
 *   callsite_def_file  : repository-relative path of that function's definition
 *
 * Consumer: `types_manifest.py` — fills `callsites` on callback entries.
 */
import cpp

string pathOf(File f) {
  if exists(f.getRelativePath()) then result = f.getRelativePath() else result = f.getAbsolutePath()
}

RoutineType routineOf(Type t) {
  t instanceof RoutineType and result = t
  or
  result = routineOf(t.(DerivedType).getBaseType())
  or
  result = routineOf(t.(TypedefType).getBaseType())
}

string tdDefFileOf(TypedefType td) { result = pathOf(td.getFile()) }

string fnDefFileOf(Function fn) {
  if exists(fn.getDefinition()) then result = pathOf(fn.getDefinition().getFile()) else result = ""
}

/**
 * A named callback typedef reachable from an expression's type — unwrap pointer
 * and typedef levels (monotonic recursion, no negation, so a typedef-of-callback
 * yields both; the composer dedups by tag).
 */
TypedefType cbType(Type t) {
  (t instanceof TypedefType and exists(routineOf(t)) and result = t)
  or
  result = cbType(t.(DerivedType).getBaseType())
  or
  result = cbType(t.(TypedefType).getBaseType())
}

from ExprCall call, TypedefType cb, Function enc
where
  cb = cbType(call.getExpr().getType()) and
  enc = call.getEnclosingFunction()
select cb.getName() as callback_name,
       tdDefFileOf(cb) as callback_def_file,
       enc.getName() as callsite_name,
       fnDefFileOf(enc) as callsite_def_file
