/**
 * FieldAccess enriched with the *outermost named* struct/union the
 * qualifier chain resolves to, walking past anonymous nested
 * struct/unions. Same columns as `edges/field_accesses.ql` plus:
 *   - root_struct_name      : the named ancestor's tag (or ""
 *                             when no named ancestor is found)
 *   - root_struct_def_file  : that ancestor's definition file
 *
 * Heuristic: a Struct/Union whose Name starts with "(unnamed" is
 * treated as anonymous; we recurse via the qualifier expression
 * until we hit either a Field whose declaring struct is non-anon,
 * or a non-FieldAccess base whose Type strips to a non-anon struct.
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

bindingset[n]
predicate isAnonNamed(string n) {
  n = "" or n.matches("(unnamed%")
}

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
       f.getDeclaringType().getName() as struct_name,
       rootT.getName() as root_struct_name,
       structDefFileOf(rootT) as root_struct_def_file,
       f.getName() as field_name,
       accessKindOf(fa) as access_kind,
       fa.getLocation().getStartLine() as access_line
