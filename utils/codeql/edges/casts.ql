/**
 * Enumerate every pointer cast between two named user-struct types.
 *
 * One row per directed `(from_tag, to_tag)` pair for which the database
 * contains at least one cast whose operand type strips to struct
 * `from_tag` and whose result type strips to struct `to_tag`
 * (`from_tag != to_tag`). "Strips" = peel pointers, arrays, cv-qualifiers,
 * and typedef aliases down to the first named `Struct` — so the tags match
 * the struct tags `entities/types.ql` emits (e.g. `stack_st_X509`,
 * `stack_st`, `ssl_st`, `ssl_connection_st`), NOT typedef spellings.
 *
 * This is the raw cast graph. It is intentionally NOT classified: the same
 * relation surfaces several distinct C idioms, distinguished only by other
 * signals (field shape, in-degree, first-member embedding), which is a
 * consumer concern:
 *   - typegen ERASURE: `stack_st_X509 -> stack_st` (instance casts to its
 *     type-erased engine), and the reverse from value getters.
 *   - polymorphic DOWNCAST: `ssl_st -> ssl_connection_st` (base handle cast
 *     to a derived; the embedded-base UPCAST goes through `&derived->base`
 *     field-address arithmetic and is NOT a cast, so it does not appear).
 *   - ASN1 ITEM erasure: `pkcs7_st -> ASN1_VALUE_st`, etc.
 *
 * Composer (`compose/types_manifest.py` via `compose/reach.py`) stores this
 * verbatim as each type's `casted: {to, from}` lists (forward = `to`,
 * inverse = `from`); no semantics are baked in here.
 *
 * # cols:
 *   from_tag : C struct tag the cast operand strips to (the source type)
 *   to_tag   : C struct tag the cast result strips to (the target type)
 */
import cpp

/**
 * The first named `Struct` reached by peeling pointers / arrays /
 * cv-qualifiers (`DerivedType`) and typedef aliases (`TypedefType`) off
 * `t`. Anonymous tags (cpp-all spells them `(unnamed …)`) are excluded so
 * the tag is a stable, lookup-able identifier — same discipline as
 * `entities/types.ql`.
 */
Struct strippedStruct(Type t) {
  result = t and result.getName() != "" and not result.getName().matches("(%")
  or
  result = strippedStruct(t.(DerivedType).getBaseType())
  or
  result = strippedStruct(t.(TypedefType).getBaseType())
}

from Struct src, Struct dst
where
  exists(Cast c |
    src = strippedStruct(c.getExpr().getType()) and
    dst = strippedStruct(c.getType())
  ) and
  src != dst
select src.getName() as from_tag, dst.getName() as to_tag
