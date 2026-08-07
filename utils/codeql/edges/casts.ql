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
 *   - engine ERASURE: `stack_st_X509 -> stack_st` (instance casts to its
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
import identity

/**
 * The first `Struct` reached by peeling pointers / arrays /
 * cv-qualifiers (`DerivedType`) and typedef aliases (`TypedefType`) off
 * `t`, whose identity `canonicalTypeName` can resolve.
 *
 * This binds `Struct`, never the typedef, so a shape-A type
 * (`typedef struct {…} PACKET;`) arrives here as the ANONYMOUS struct — the
 * name lives only on the typedef, sideways. Excluding anonymous tags therefore
 * dropped every cast involving one, silently: `casts.csv` holds 0 rows for
 * `PACKET` and `OSSL_TIME`, both of which the port scope casts routinely.
 *
 * Queries that walk from a USE SITE (`signature_type_uses`, `local_type_uses`)
 * are unaffected — there the source writes `PACKET *`, so the TypedefType is
 * in the type graph and carries its own name. This one has no use site to read.
 */
Struct strippedStruct(Type t) {
  result = t and not isAnonNamed(canonicalTypeName(result))
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
select canonicalTypeName(src) as from_tag, canonicalTypeName(dst) as to_tag
