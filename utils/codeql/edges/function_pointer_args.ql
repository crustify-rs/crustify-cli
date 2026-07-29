/**
 * Enumerate every pointer-typed parameter of every function in the
 * database.
 *
 * One row per (function, ptr-arg-position) where the parameter's
 * declared type is a pointer (`T *`, `T **`, `const T *`, …). The
 * row carries the structural facts the syms-manifest composer needs
 * to populate the per-function `ptr_args[]` entries:
 *
 *   - The 0-indexed parameter position.
 *   - The C parameter name as written (empty when the prototype
 *     omits names; consumer falls back to `"arg<N>"`).
 *   - The verbatim type at the bottom of the pointer chain, after
 *     unwrapping every `PointerType` level. This may be a
 *     `UserType` tag (struct, union, enum, typedef) or a
 *     `BuiltInType` name (`int`, `char`, `void`, …). Anonymous
 *     UserTypes surface with their cpp-all synthetic name
 *     (`(unnamed …)`); the consumer drops those at the join site.
 *   - Whether the pointee is `const`-qualified (cv-qualifier on the
 *     innermost non-pointer type).
 *   - The pointer depth (1 for `T *`, 2 for `T **`, …). A typedef that
 *     HIDES a `*` counts as a pointer level, so `SSL_verify_cb cb` and
 *     `OPENSSL_STRING s` are depth 1 rather than being dropped from the
 *     result entirely (the `pointerDepth > 0` gate used to filter them,
 *     leaving those parameters with no record and hence nowhere to carry an
 *     ownership block). A typedef naming a NON-pointer is not unwrapped, so
 *     `SSL *` still reports `SSL`, not `ssl_st`.
 *   - A CALLBACK typedef is terminal: `pointee_type` is the typedef name
 *     (`SSL_verify_cb`), not `"(routine)"`. `"(routine)"` now appears only
 *     for a bare, un-typedef'd function pointer, which names nothing a
 *     consumer could depend on.
 *
 * # cols:
 *   function_name      : enclosing function's C identifier
 *   function_def_file  : repository-relative path of the function's
 *                        definition site, or "" if no definition is
 *                        in the DB
 *   position           : 0-indexed parameter index
 *   param_name         : C parameter name as written, or "" if the
 *                        prototype omits names at this position
 *   pointee_type       : the verbatim type at the bottom of the
 *                        pointer chain (UserType tag OR BuiltInType
 *                        name OR "(routine)" for function-pointer
 *                        params OR "(array)" for array-of-pointer
 *                        edge cases)
 *   is_const           : "true" iff the innermost pointee is
 *                        const-qualified; "false" otherwise
 *   depth              : pointer depth — 1 for `T *`, 2 for `T **`, …
 *
 * Consumer: `utils/codeql/compose/syms_manifest.py` — fills the
 * composer-side fields of `ptr_args[]` on every `function_*` entry
 * in `wrap/syms.json` and `port/syms.json`.
 *
 * Non-pointer parameters do NOT produce rows. A function with no
 * pointer parameters contributes zero rows; consumers correctly
 * read that as `ptr_args: []`.
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

/**
 * A CALLBACK typedef — a typedef whose unwrap chain reaches a `RoutineType`.
 * It is terminal for the pointer walk: the identity we want is the typedef
 * name, not the anonymous routine it wraps.
 */
predicate isCallbackTypedef(Type t) { t instanceof TypedefType and exists(routineOf(t)) }

/**
 * Pointer indirection count, seeing THROUGH pointer typedefs.
 *
 * A typedef that hides a `*` (`typedef int (*SSL_verify_cb)(...)`,
 * `typedef char *OPENSSL_STRING;`) is a pointer level: without this, such a
 * parameter scored depth 0, failed the `pointerDepth(ptype) > 0` gate, and
 * produced NO ptr_args record at all — so it had nowhere to carry an
 * ownership block. A typedef that names a NON-pointer
 * (`typedef struct ssl_st SSL;`) is NOT unwrapped, so `SSL *` still reports
 * depth 1 with pointee `SSL` rather than `ssl_st`.
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

/**
 * The innermost type reached by unwrapping all `PointerType` levels
 * from `t`. Equal to `t` itself when `t` isn't a pointer. The
 * returned type may still be cv-qualified (wrapped in an INNER
 * `SpecifiedType` over the pointee) — consumers use `.isConst()`
 * to test const-ness, so that wrapper is preserved.
 *
 * Walks transparently through OUTER `SpecifiedType` wrappers when
 * they're applied to a pointer level (e.g. `void *__restrict__`)
 * — those wrap the pointer not the pointee, and stopping at them
 * would lose the underlying pointer.
 */
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

/**
 * Render the innermost pointee for the `pointee_type` column. We
 * unwrap one more layer of `SpecifiedType` (the cv-qualifier
 * wrapper) so the surfaced name is the unqualified tag.
 */
string pointeeName(Type inner) {
  exists(Type unq |
    (
      unq = inner.(SpecifiedType).getBaseType()
      or
      (not inner instanceof SpecifiedType and unq = inner)
    ) and
    (
      // RoutineType (function pointers) — emit a synthetic marker so
      // consumers route these through callback-type handling.
      unq instanceof RoutineType and result = "(routine)"
      or
      // ArrayType — rare in param position (arrays decay to pointers
      // before reaching here). Surface with a marker for the rare
      // legitimate `T (*arr)[N]` case.
      unq instanceof ArrayType and result = "(array)"
      or
      // UserType — emit the C tag (may be `(unnamed …)` for
      // anonymous tags; consumer filters those at join time).
      unq instanceof UserType and result = unq.(UserType).getName()
      or
      // BuiltInType — `int`, `char`, `void`, …
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
 * One signature parameter position of a "signature-bearing entity": a real
 * `Function` (named params) OR a function-pointer typedef / callback (the
 * underlying `RoutineType`'s parameter types, which carry no names). Unifying
 * both lets the same `ptr_args_of` reach index and `_compose_ptr_args` composer
 * serve callbacks with no extra code — a callback name never collides with a
 * function name, so the merged index stays unambiguous.
 */
predicate sigParam(string name, string defFile, int pos, string paramName, Type ptype) {
  exists(Function fn, Parameter p |
    p = fn.getParameter(pos) and
    ptype = p.getType() and
    name = fn.getName() and
    defFile = fnDefFileOf(fn) and
    paramName = p.getName()
  )
  or
  exists(TypedefType td, RoutineType rt |
    rt = routineOf(td) and
    ptype = rt.getParameterType(pos) and
    name = td.getName() and
    defFile = tdDefFileOf(td) and
    paramName = ""
  )
}

from string name, string defFile, int pos, string paramName, Type ptype
where
  sigParam(name, defFile, pos, paramName, ptype) and
  // Use depth>0 instead of `instanceof PointerType` so we catch
  // pointer types wrapped in an outer `SpecifiedType` qualifier
  // (e.g. libc's `void *__restrict__`). The walk inside
  // `pointerDepth` handles the unwrap.
  pointerDepth(ptype) > 0
select name as function_name,
       defFile as function_def_file,
       pos as position,
       paramName as param_name,
       pointeeName(pointerInner(ptype)) as pointee_type,
       constStr(pointerInner(ptype)) as is_const,
       pointerDepth(ptype) as depth
