<!-- SKILL -->

For a Crustify translation, apply the worklist's established ownership,
lifetime, mutability, nullability and cardinality facts before choosing an
ffibox representation. Prefer its layout newtype and borrowed handles, and
never form a Rust reference to the wrapped C object. Use stateless ownership
when possible; carry runtime drop state only when the contract requires it.
Hand-write a representation when ffibox cannot express the proven contract,
while preserving the same seam and safety-comment discipline.
