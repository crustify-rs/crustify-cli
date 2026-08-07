/**
 * Enumerate every field of every named struct/union in the
 * database, with its declared type and a scalar/aggregate
 * classification.
 *
 * One row per (struct, field) pair. Anonymous declaring types
 * (`(unnamed class/struct/union)`) are skipped — they have no
 * stable identity consumers can reference; field accesses on
 * inner anonymous types are already surfaced under their named
 * outer struct by `edges/field_accesses.ql` when cpp-all
 * flattens them.
 *
 * The `is_scalar` column distinguishes value-type fields
 * (bindgen emits the corresponding primitive directly, the safe
 * wrapper sees a plain `c_int` / `usize` / `*mut u8`) from
 * aggregate-bearing fields (struct, union, enum, pointer-to-
 * struct, typedef chain ending at one of those). The schema
 * rule in `wrap/types.json` / `port/types.json` is:
 *
 *   - is_scalar="true"  → consumer OMITS `fields[].type`
 *   - is_scalar="false" → consumer EMITS `"type": <field_type>`
 *
 * Typedef chains are descended via an explicit
 * `TypedefType.getBaseType()` branch — in this cpp-all version
 * `TypedefType` is NOT a subclass of `DerivedType`, so the
 * standard derived-type unwrap (pointers / arrays / qualifiers)
 * does NOT step through typedef aliases on its own. The
 * predicate adds a second recursive case for `TypedefType`
 * specifically. A field declared `size_t` (typedef →
 * `unsigned long`) classifies as scalar because the chain ends
 * at `IntegralType`; a field declared `SSL_SESSION *` classifies
 * as non-scalar because the chain steps through `PointerType` →
 * `TypedefType` (SSL_SESSION) → `Struct` (ssl_session_st).
 *
 * Edge cases the predicate handles correctly:
 *
 *   - `char *`, `void *`, `int *` → scalar (primitive pointee,
 *     no aggregate)
 *   - `struct foo *` → non-scalar (pointer-to-struct)
 *   - `EVP_PKEY` (typedef → struct) → non-scalar
 *   - `enum foo_e` → non-scalar (enums are aggregate per schema)
 *   - `enum foo_e value` → non-scalar
 *   - `unsigned char buf[256]` → scalar (array of primitive)
 *   - `SSL_SESSION *cache[64]` → non-scalar (array of pointer-to-typedef-to-struct)
 *
 * # cols:
 *   struct_name      : C tag of the declaring struct/union
 *   struct_def_file  : repository-relative path of the declaring
 *                      struct's definition site, or "" if no
 *                      full-body definition is in the DB
 *   field_name       : the field's C identifier
 *   field_type       : the field's declared C type as a single
 *                      string from `Type.toString()` — includes
 *                      cv-qualifiers, pointer asterisks, and
 *                      array dimensions
 *   is_scalar        : "true" iff the type contains no Struct /
 *                      Union / Enum anywhere in its derived-type
 *                      walk; "false" otherwise. NOTE this answers
 *                      "contains an aggregate?", NOT "is a pointer?"
 *                      — use `ptr_depth` for the latter.
 *   ptr_depth        : pointer indirection count — 0 for a
 *                      non-pointer, 1 for `T *` / an object-pointer
 *                      typedef / a function pointer, 2 for `T **`, …
 *                      Sees THROUGH typedefs that hide the star, which
 *                      the `field_type` string cannot show
 *
 * Consumer: `utils/codeql/compose/types_manifest.py` —
 * populates `fields[].type` per struct entry in
 * `port/types.json` and `wrap/types.json`, omitting the `type`
 * key for scalar fields per the schema rule above.
 */
import cpp
import identity

/**
 * Holds if any unwrap of `t` reaches a Struct, Union, or Enum.
 * Walks both DerivedType.getBaseType() (pointers, arrays,
 * cv-qualifiers) AND TypedefType.getBaseType() (typedef aliases).
 * Returns false for pure-primitive chains (`int`, `size_t` →
 * `unsigned long`, `char *`, `void *`).
 */
/** Holds if `t`'s unwrap chain reaches a `RoutineType` (a function type). */
predicate reachesRoutineType(Type t) {
  t instanceof RoutineType
  or
  reachesRoutineType(t.(DerivedType).getBaseType())
  or
  reachesRoutineType(t.(TypedefType).getBaseType())
}

/** A typedef whose unwrap chain reaches a `RoutineType` — terminal for the
 * pointer walk: the identity is the typedef name, not the anonymous routine. */
predicate isCallbackTypedef(Type t) {
  t instanceof TypedefType and reachesRoutineType(t)
}

/** Holds if a pointer level is reachable from `t` through qualifiers/typedefs. */
predicate reachesPointer(Type t) {
  t instanceof PointerType
  or
  t instanceof FunctionPointerIshType
  or
  reachesPointer(t.(SpecifiedType).getBaseType())
  or
  reachesPointer(t.(TypedefType).getBaseType())
}

/**
 * Pointer indirection count for a FIELD's declared type — the authoritative
 * "is this field a pointer?" signal, replacing the consumer's old heuristic of
 * looking for a literal `*` in `field_type`.
 *
 * That heuristic was blind to the two shapes where C hides the star behind a
 * name, both of which then collapsed to a bare scalar field with NO pointer
 * record and hence no ownership analysis:
 *
 *   - an OBJECT-pointer typedef (`typedef struct _filesec *filesec_t;`) —
 *     `Type.toString()` prints `filesec_t`, and types.csv cannot help because
 *     `aliasOf` unwraps the star, making `typedef T *P` indistinguishable from
 *     `typedef T P`;
 *   - a bare function pointer (`int (*ctrl)(BIO *, int, long, void *)`) —
 *     modelled as `FunctionPointerIshType`, which is NOT a `PointerType`.
 *
 * Mirrors `edges/function_pointer_args.ql::pointerDepth` so a field and a
 * parameter of the same type report the same depth.
 */
int pointerDepthOf(Type t) {
  if isCallbackTypedef(t)
  then result = 1
  else
    if t instanceof FunctionPointerIshType
    then result = 1
    else
      if t instanceof PointerType
      then result = 1 + pointerDepthOf(t.(PointerType).getBaseType())
      else
        if t instanceof SpecifiedType and reachesPointer(t.(SpecifiedType).getBaseType())
        then result = pointerDepthOf(t.(SpecifiedType).getBaseType())
        else
          if t instanceof TypedefType and reachesPointer(t.(TypedefType).getBaseType())
          then result = pointerDepthOf(t.(TypedefType).getBaseType())
          else result = 0
}

predicate containsAggregateType(Type t) {
  t instanceof Struct
  or t instanceof Union
  or t instanceof Enum
  or containsAggregateType(t.(DerivedType).getBaseType())
  or containsAggregateType(t.(TypedefType).getBaseType())
}

string isScalarOf(Field f) {
  if containsAggregateType(f.getType())
  then result = "false"
  else result = "true"
}

from Field f, string struct_name, string struct_def_file, string field_name
where
  (
    // Ordinary field of a named struct/union, or of an anonymous aggregate
    // that a typedef names (`typedef struct { … } git_cache;`).
    ownerOf(f.getDeclaringType(), struct_name, struct_def_file) and
    field_name = f.getName()
  )
  or
  (
    // Field of an ANONYMOUS aggregate embedded by value in `root`. C gives
    // these no independent identity — `s->ext.hostname` names no type a
    // consumer can reference — so they are flattened into the owning named
    // struct under a QUALIFIED name (`ext.hostname`). Without this they were
    // dropped at every stage: no node (anonymous tags are rejected), no entry
    // in the parent's `fields[]`, and no dependency edge for their types.
    exists(UserType root |
      ownerOf(root, struct_name, struct_def_file) and
      anonEmbeddedField(root, f, field_name)
    )
  )
select struct_name,
       struct_def_file,
       field_name,
       f.getType().toString() as field_type,
       isScalarOf(f) as is_scalar,
       pointerDepthOf(f.getType()) as ptr_depth
