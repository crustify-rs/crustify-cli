/**
 * Free-site element types. At every call to one of the four buffer-release
 * frees, the type of the freed pointer BEFORE its implicit conversion to
 * `void*` is the buffer's element type T. One row per (free, element, site).
 *
 * # cols:
 *   free   : the release function (the CVec strategy key)
 *   elem   : the element type spelling as written at the free site (typedef
 *            kept, e.g. TLS_GROUP_INFO / unsigned char)
 *   path   : file of the free call
 *   line   : line of the free call
 */
import cpp

predicate isFree(string n) {
  n = "CRYPTO_free" or n = "CRYPTO_clear_free" or
  n = "CRYPTO_secure_free" or n = "CRYPTO_secure_clear_free"
}

// Peel typedef + cv down to a PointerType (the freed pointer as written).
PointerType asPointer(Type t) {
  result = t
  or result = asPointer(t.(TypedefType).getBaseType())
  or result = asPointer(t.(SpecifiedType).getBaseType())
}

// Strip only cv off the pointee, keeping its typedef spelling.
Type stripCV(Type t) {
  result = t and not t instanceof SpecifiedType
  or result = stripCV(t.(SpecifiedType).getBaseType())
}

from FunctionCall fc, string free, Type pointee, string elem
where
  free = fc.getTarget().getName() and isFree(free) and
  pointee = stripCV(asPointer(fc.getArgument(0).getType()).getBaseType()) and
  elem = pointee.getName() and
  elem != "void" and
  not elem.matches("(%")            // drop anonymous
select free, elem,
       fc.getLocation().getFile().getRelativePath() as path,
       fc.getLocation().getStartLine() as line
