/**
 * Enumerate every function whose declared return type is a pointer.
 *
 * One row per function whose `Function.getType()` is a `PointerType`.
 * The row carries the structural facts the syms-manifest composer
 * needs to populate the per-function `ptr_ret` object:
 *
 *   - The verbatim type at the bottom of the pointer chain, after
 *     unwrapping every `PointerType` level. Same conventions as
 *     `edges/function_pointer_args.ql` — may be a `UserType` tag,
 *     a `BuiltInType` name, or one of the synthetic markers
 *     `"(routine)"` / `"(array)"`.
 *   - Whether the pointee is `const`-qualified.
 *   - The pointer depth (1 for `T *`, 2 for `T **`, …).
 *
 * Non-pointer returns (void, scalar, struct-by-value) do NOT produce
 * rows. Consumers correctly read absence as `ptr_ret: null`.
 *
 * # cols:
 *   function_name      : function's C identifier
 *   function_def_file  : repository-relative path of the function's
 *                        definition site, or "" if no definition is
 *                        in the DB
 *   pointee_type       : the verbatim type at the bottom of the
 *                        pointer chain
 *   is_const           : "true" iff the innermost pointee is
 *                        const-qualified; "false" otherwise
 *   depth              : pointer depth — 1 for `T *`, 2 for `T **`, …
 *
 * Consumer: `utils/codeql/compose/syms_manifest.py` — fills the
 * composer-side fields of `ptr_ret` on every `function_*` entry
 * with a pointer-typed return.
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

/** Holds if a pointer level is reachable from `t` through qualifiers/typedefs. */
predicate reachesPointer(Type t) {
  t instanceof PointerType
  or
  // A bare function pointer IS a pointer level, but CodeQL models it as
  // `FunctionPointerIshType extends DerivedType` — NOT a `PointerType` — so
  // without this it scored depth 0, failed the `pointerDepth > 0` gate, and
  // produced no record at all (which is why `"(routine)"` never once appeared
  // in the emitted CSVs despite being documented below).
  t instanceof FunctionPointerIshType
  or
  reachesPointer(t.(SpecifiedType).getBaseType())
  or
  reachesPointer(t.(TypedefType).getBaseType())
}

/** A typedef whose unwrap chain reaches a `RoutineType` — terminal for the
 *  pointer walk, so the reported identity is the typedef name. */
predicate isCallbackTypedef(Type t) { t instanceof TypedefType and exists(routineOf(t)) }

/**
 * Pointer indirection count, seeing THROUGH pointer typedefs — mirrors
 * `edges/function_pointer_args.ql`. A return type that is itself a pointer
 * typedef (`SSL_verify_cb SSL_CTX_get_verify_callback(...)`) used to score 0
 * and produce no `ptr_ret` record at all. A typedef naming a NON-pointer
 * (`typedef struct ssl_st SSL;`) is not unwrapped, so `SSL *` still reports
 * pointee `SSL`.
 */
int pointerDepth(Type t) {
  if isCallbackTypedef(t)
  then result = 1
  else
    // A bare function pointer is terminal at depth 1, exactly like a callback
    // typedef — there is no pointee chain to keep walking, only a signature.
    if t instanceof FunctionPointerIshType
    then result = 1
    else
      if t instanceof PointerType
      then result = 1 + pointerDepth(t.(PointerType).getBaseType())
      else
        if t instanceof SpecifiedType and reachesPointer(t.(SpecifiedType).getBaseType())
        then result = pointerDepth(t.(SpecifiedType).getBaseType())
        else
          if t instanceof TypedefType and reachesPointer(t.(TypedefType).getBaseType())
          then result = pointerDepth(t.(TypedefType).getBaseType())
          else result = 0
}

Type pointerInner(Type t) {
  if isCallbackTypedef(t)
  then result = t
  else
    // Bare function pointer: the innermost thing is its `RoutineType`, which
    // `pointeeName` renders as the synthetic marker `"(routine)"`. It names
    // nothing depend-able (that is what makes it "bare"); the user types in
    // its signature are recovered separately by the *_type_uses queries.
    if t instanceof FunctionPointerIshType
    then result = t.(FunctionPointerIshType).getBaseType()
    else
      if t instanceof PointerType
      then result = pointerInner(t.(PointerType).getBaseType())
      else
        if t instanceof SpecifiedType and reachesPointer(t.(SpecifiedType).getBaseType())
        then result = pointerInner(t.(SpecifiedType).getBaseType())
        else
          if t instanceof TypedefType and reachesPointer(t.(TypedefType).getBaseType())
          then result = pointerInner(t.(TypedefType).getBaseType())
          else result = t
}

string pointeeName(Type inner) {
  exists(Type unq |
    (
      unq = inner.(SpecifiedType).getBaseType()
      or
      (not inner instanceof SpecifiedType and unq = inner)
    ) and
    (
      unq instanceof RoutineType and result = "(routine)"
      or
      unq instanceof ArrayType and result = "(array)"
      or
      unq instanceof UserType and result = unq.(UserType).getName()
      or
      unq instanceof BuiltInType and result = unq.toString()
    )
  )
}

string fnDefFileOf(Function fn) {
  if exists(fn.getDefinition())
  then result = pathOf(fn.getDefinition().getFile())
  else result = ""
}

string constStr(Type inner) {
  if inner.isConst() then result = "true" else result = "false"
}

string tdDefFileOf(TypedefType td) { result = pathOf(td.getFile()) }

/**
 * The `RoutineType` a (function-pointer) typedef ultimately names — see
 * `entities/types.ql`'s `reachesRoutineType`. Yields nothing for a
 * non-callback typedef.
 */
RoutineType routineOf(Type t) {
  t instanceof RoutineType and result = t
  or
  result = routineOf(t.(DerivedType).getBaseType())
  or
  result = routineOf(t.(TypedefType).getBaseType())
}

/**
 * The pointer-typed return of a "signature-bearing entity": a real `Function`
 * OR a function-pointer typedef / callback (the underlying `RoutineType`'s
 * return type). Unifying both lets the same `ptr_ret_of` reach index and
 * `_compose_ptr_ret` composer serve callbacks with no extra code.
 */
predicate sigRet(string name, string defFile, Type rtype) {
  exists(Function fn | rtype = fn.getType() and name = fn.getName() and defFile = fnDefFileOf(fn))
  or
  exists(TypedefType td, RoutineType rt |
    rt = routineOf(td) and rtype = rt.getReturnType() and
    name = td.getName() and defFile = tdDefFileOf(td)
  )
}

from string name, string defFile, Type rtype
where
  sigRet(name, defFile, rtype) and
  pointerDepth(rtype) > 0
select name as function_name,
       defFile as function_def_file,
       pointeeName(pointerInner(rtype)) as pointee_type,
       constStr(pointerInner(rtype)) as is_const,
       pointerDepth(rtype) as depth
