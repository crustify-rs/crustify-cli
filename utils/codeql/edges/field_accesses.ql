/**
 * Enumerate every struct/union field access in the database.
 *
 * One row per `FieldAccess`. Both `.field` and `->field` produce
 * `FieldAccess` nodes; this query doesn't distinguish (the type
 * analyzer cares about WHICH struct + field is touched, not the
 * syntax used to touch it).
 *
 * The `access_kind` column matches `global_accesses.ql`'s vocabulary:
 *
 *   - "write"  — the access is the lvalue of an assignment
 *   - "addr"   — the access is the operand of `&`
 *   - "read"   — anything else
 *
 * This is what drives `fields[].used_by` classification in the type
 * manifest (a field with only "read" accesses is read-only at the
 * boundary; a field with "write" accesses needs setter exposure).
 *
 * The declaring struct's identity is emitted as a name + location
 * pair so consumers can disambiguate same-named fields across
 * different structs (`x509_st.name` vs `x509_name_st.name`, etc.).
 *
 * # cols:
 *   enclosing_name      : enclosing function's C identifier (or "" if
 *                         the access is at file scope, e.g. inside a
 *                         static initializer)
 *   access_file         : repository-relative path of the access
 *                         site's file
 *   struct_name         : C tag of the declaring struct/union
 *   struct_def_file     : repository-relative path of the declaring
 *                         struct's definition site, or "" if no
 *                         full-body definition is in the DB
 *   field_name          : the accessed field's C identifier
 *   access_kind         : "read" | "write" | "addr"
 *   access_line         : 1-indexed line number of the access site
 *
 * Consumer: CrustifyTypeAnalyzer for `non_opaque_in` (which
 * files touch a struct's fields directly) and `fields[].used_by`
 * (port vs wrap users per field).
 *
 * `struct_name` carries the declaring aggregate's IDENTITY, not
 * `getName()` verbatim: an anonymous aggregate declared inline as a
 * typedef's underlying type (`typedef struct { ... } T;`) resolves to
 * the typedef name via `identity.qll`, matching what
 * `entities/types.ql` stamped `struct_anonymous` and what
 * `types_manifest.py` adopted as the entry's identity. Without that
 * resolution these rows all collapse into the single bucket
 * `(unnamed class/struct/union)` — 12,307 of 74,015 rows on the
 * OpenSSL tree — and every consumer keying on `struct_name` drops
 * them, so `PACKET` / `OSSL_TIME` / `CLIENTHELLO_MSG` read as though
 * no function ever touches a field of theirs.
 *
 * An anonymous aggregate EMBEDDED in a named struct
 * (`struct N { struct { int x; } inner; }`) has no typedef to resolve
 * to; its identity is the enclosing named struct, reachable only from
 * the qualifier chain. Those rows keep the placeholder here and are
 * resolved by `edges/fa_with_root.ql`, which emits `root_struct_name`
 * and the dotted `field_path` for exactly this case. Consumers
 * needing full coverage should join the two.
 */
import cpp
import identity

string enclosingNameOf(FieldAccess fa) {
  if exists(fa.getEnclosingFunction())
  then result = fa.getEnclosingFunction().getName()
  else result = ""
}

string accessKindOf(FieldAccess fa) {
  if exists(AddressOfExpr aoe | aoe.getOperand() = fa)
  then result = "addr"
  else if fa.isUsedAsLValue()
  then result = "write"
  else result = "read"
}

string structDefFileOf(Field f) {
  if exists(f.getDeclaringType().(Struct).getDefinition())
  then result = pathOf(f.getDeclaringType().(Struct).getDefinition().getFile())
  else if exists(f.getDeclaringType().(Union).getDefinition())
  then result = pathOf(f.getDeclaringType().(Union).getDefinition().getFile())
  else result = ""
}

from FieldAccess fa, Field f
where f = fa.getTarget()
select enclosingNameOf(fa) as enclosing_name,
       pathOf(fa.getFile()) as access_file,
       canonicalTypeName(f.getDeclaringType()) as struct_name,
       structDefFileOf(f) as struct_def_file,
       f.getName() as field_name,
       accessKindOf(fa) as access_kind,
       fa.getLocation().getStartLine() as access_line
