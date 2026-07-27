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
 *                      walk; "false" otherwise
 *
 * Consumer: `utils/codeql/compose/types_manifest.py` —
 * populates `fields[].type` per struct entry in
 * `port/types.json` and `wrap/types.json`, omitting the `type`
 * key for scalar fields per the schema rule above.
 */
import cpp

/**
 * Repository-relative path, falling back to absolute for files outside the
 * source root (system / external headers).
 */
string pathOf(File f) {
  if exists(f.getRelativePath())
  then result = f.getRelativePath()
  else result = f.getAbsolutePath()
}

/**
 * Holds if any unwrap of `t` reaches a Struct, Union, or Enum.
 * Walks both DerivedType.getBaseType() (pointers, arrays,
 * cv-qualifiers) AND TypedefType.getBaseType() (typedef aliases).
 * Returns false for pure-primitive chains (`int`, `size_t` →
 * `unsigned long`, `char *`, `void *`).
 */
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

/**
 * Unwrap pointers / arrays / cv-qualifiers to the first `UserType`
 * reached (typedef chains are NOT followed). Mirrors the helper in
 * `entities/types.ql` so field attribution and `def_file` resolution
 * agree on what a typedef's underlying aggregate is.
 */
predicate unwrappedUserType(Type t, UserType b) {
  b = t
  or
  unwrappedUserType(t.(DerivedType).getBaseType(), b)
}

/**
 * A typedef `td` whose underlying type is the inline anonymous aggregate
 * `anon` (`typedef struct { … } git_cache;`). The typedef is `anon`'s only
 * stable identity, so its fields are attributed to the typedef name rather
 * than dropped. Restricted to struct/union (the field-bearing aggregates).
 */
predicate namingTypedef(UserType anon, TypedefType td) {
  anon.getName().prefix(1) = "(" and
  (anon instanceof Struct or anon instanceof Union) and
  unwrappedUserType(td.getBaseType(), anon)
}

/** Definition file of any struct/union (named or anonymous); "" if the DB
 *  holds no full-body definition. */
string anonDefFileOf(UserType anon) {
  if exists(anon.(Struct).getDefinition())
  then result = pathOf(anon.(Struct).getDefinition().getFile())
  else if exists(anon.(Union).getDefinition())
  then result = pathOf(anon.(Union).getDefinition().getFile())
  else result = ""
}

/**
 * The aggregate a field EMBEDS by value, if any — arrays and cv-qualifiers are
 * unwrapped, but pointers are NOT: `struct { … } *p` points at a struct, it
 * does not contain one, so its members must not be flattened into the parent.
 */
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
  result.getName().prefix(1) = "(" and
  (result instanceof Struct or result instanceof Union)
}

/**
 * `f` is reachable from aggregate `root` through one or more ANONYMOUS
 * embedded members; `path` is the dotted access path (`ext.hostname`,
 * `s3.tmp.new_cipher`). Recursion stops at the first NAMED aggregate — a
 * member of named type is its own entity and keeps its own edge.
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
 * typedef that names it. Single definition shared by the plain-field and the
 * flattened-anonymous-member cases so the two cannot drift.
 */
predicate ownerOf(UserType t, string name, string file) {
  t.getName() != "" and
  not t.getName().prefix(1) = "(" and
  name = t.getName() and
  file = anonDefFileOf(t)
  or
  exists(TypedefType td |
    namingTypedef(t, td) and
    name = td.getName() and
    file = anonDefFileOf(t)
  )
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
       isScalarOf(f) as is_scalar
