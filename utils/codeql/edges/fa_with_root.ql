/**
 * FieldAccess enriched with the *outermost named* struct/union the
 * qualifier chain resolves to, walking past anonymous nested
 * struct/unions. Same columns as `edges/field_accesses.ql` plus:
 *   - root_struct_name      : the named ancestor's tag (or ""
 *                             when no named ancestor is found)
 *   - root_struct_def_file  : that ancestor's definition file
 *   - field_path            : dotted member path from the named ancestor
 *                             (`ext.hostname`); equals field_name when the
 *                             access is not through an anonymous member
 *
 * Heuristic: a Struct/Union whose Name starts with "(unnamed" is
 * treated as anonymous; we recurse via the qualifier expression
 * until we hit either a Field whose declaring struct is non-anon,
 * or a non-FieldAccess base whose Type strips to a non-anon struct.
 *
 * That outward walk resolves an anonymous aggregate EMBEDDED in a named
 * one, but it cannot resolve `typedef struct { ... } T;` — there is no
 * named ancestor to reach, because the name sits sideways on the typedef.
 * Both emitted names therefore run through `identity.qll`'s
 * `canonicalTypeName`, which supplies the typedef identity for that case;
 * the qualifier walk still owns the embedded one. A row keeps the
 * `(unnamed ...)` placeholder only when NEITHER resolution applies.
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

Type rootContainerOfExpr(Expr e) {
  // Walk past FieldAccesses whose declaring struct is anonymous.
  if e instanceof FieldAccess
  then exists(FieldAccess fa | fa = e |
    if isAnonNamed(fa.getTarget().getDeclaringType().getName())
    then result = rootContainerOfExpr(fa.getQualifier())
    else result = fa.getTarget().getDeclaringType())
  else result = e.getType().stripType()
}

/**
 * The dotted member path from the outermost NAMED container down to `fa`'s
 * field: `ext.hostname`, `s3.tmp.new_cipher`. Plain (non-anonymous) accesses
 * yield the bare field name. Must agree with the qualified names
 * `entities/fields.ql` and `edges/field_type_uses.ql` emit for the same
 * flattened members, or the declaration side and the access side key
 * differently.
 */
string rootFieldPath(FieldAccess fa) {
  if isAnonNamed(fa.getTarget().getDeclaringType().getName())
  then (
    exists(FieldAccess q | q = fa.getQualifier() |
      result = rootFieldPath(q) + "." + fa.getTarget().getName()
    )
    or
    not fa.getQualifier() instanceof FieldAccess and result = fa.getTarget().getName()
  )
  else result = fa.getTarget().getName()
}

Type rootNamedDeclaringType(FieldAccess fa) {
  if isAnonNamed(fa.getTarget().getDeclaringType().getName())
  then result = rootContainerOfExpr(fa.getQualifier())
  else result = fa.getTarget().getDeclaringType()
}

string structDefFileOf(Type t) {
  if exists(t.(Struct).getDefinition())
  then result = pathOf(t.(Struct).getDefinition().getFile())
  else if exists(t.(Union).getDefinition())
  then result = pathOf(t.(Union).getDefinition().getFile())
  else result = ""
}

from FieldAccess fa, Field f, Type rootT
where f = fa.getTarget()
  and rootT = rootNamedDeclaringType(fa)
select enclosingNameOf(fa) as enclosing_name,
       pathOf(fa.getFile()) as access_file,
       canonicalTypeName(f.getDeclaringType()) as struct_name,
       canonicalNameOfType(rootT) as root_struct_name,
       structDefFileOf(rootT) as root_struct_def_file,
       f.getName() as field_name,
       rootFieldPath(fa) as field_path,
       accessKindOf(fa) as access_kind,
       fa.getLocation().getStartLine() as access_line
