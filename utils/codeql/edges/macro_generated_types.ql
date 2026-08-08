/**
 * Which macro MINTED each type — the generator→instance relation for
 * template-by-macro families.
 *
 * A C macro that emits a whole `typedef struct {…} name;` creates a family of
 * same-shaped types with NO type-level link between them: no cast, no common
 * tag, no shared base. libgit2's `GIT_HASHMAP_*` is the canonical case — 20
 * instantiations of one 7-member layout, differing only in the pointee types
 * of `keys` / `vals`, and `casts.csv` is empty for every one of them because
 * `GIT_HASHMAP__COMMON_FUNCTIONS` emits a SEPARATE copy of each accessor per
 * instantiation rather than erasing to a shared engine. Nothing downstream can
 * recover the family, so each instance is wrapped as an unrelated Rust type
 * instead of one generic plus aliases.
 *
 * Location alone cannot express this. `macro_expansions.ql` records every
 * invocation, but expansion does not advance source lines, so an instantiation
 * site carries every macro in the expansion chain AND every macro used inside
 * whatever the chain emitted:
 *
 *   submodule.h:120   -> GIT_HASHMAP_STR_STRUCT, GIT_HASHMAP_STRUCT,
 *                        GIT_HASHMAP_STRUCT_MEMBERS
 *   hashmap_str.h:41  -> 25 macros, including GIT_ASSERT, NULL, bool, true
 *
 * (6,995 of 17,414 invocation sites in libgit2 carry more than one macro; the
 * worst carries 278.) So the relation is structural, not locational: a type is
 * MINTED by an invocation when its definition is an element that invocation
 * expanded to, and the generator is the OUTERMOST invocation at that site —
 * the macro actually written in the source, not the inner ones it expands
 * through.
 *
 * # cols:
 *   type_name           : the minted type's C tag (canonical: an anonymous
 *                         aggregate named by its typedef resolves to that name)
 *   type_def_file       : repository-relative path of the type's definition
 *   generator_macro     : C identifier of the outermost macro that minted it
 *   generator_def_file  : repository-relative path of the macro's `#define`
 *
 * A single-instantiation macro is a definition, not a generator; grouping and
 * the >= 2 threshold are the consumer's, so this query does not filter.
 *
 * Consumer: `compose/types_manifest.py` — mints one synthetic generator record
 * per family (`macro_generator`), wiring `generates` / `generated_by`.
 */
import cpp
import identity

/**
 * The outermost invocation of `mi`'s expansion chain: the macro actually
 * written at the source site. `GIT_HASHMAP_STR_STRUCT` expands to
 * `GIT_HASHMAP_STRUCT` expands to `GIT_HASHMAP_STRUCT_MEMBERS`; all three
 * report the same line, and only the first is the family's name.
 */
MacroInvocation outermostOf(MacroInvocation mi) {
  if exists(mi.getParentInvocation())
  then result = outermostOf(mi.getParentInvocation())
  else result = mi
}

/**
 * `t`'s whole definition came out of this invocation.
 *
 * NOT `mi.getAnExpandedElement()`: the extractor records no macro provenance
 * for a `TypeDeclarationEntry`, so that predicate is empty for every
 * macro-minted type (`git_submodule_cache` reports 0 expanded and 0 affected).
 * What it does record is the LOCATION, and a minted type's definition entry
 * collapses to a zero-width point at the invocation site
 * (`submodule.h:120:1:120:1` inside `120:1:120:60`).
 *
 * Containment direction is the whole test. `tde` inside `mi` means the macro
 * emitted the entire `typedef`. The reverse — a hand-written multi-line struct
 * with a macro on one of its interior lines, e.g. `GIT_HASHMAP_STRUCT_MEMBERS`
 * expanded inside a struct someone wrote by hand — leaves `tde` LARGER than
 * `mi`, so it does not match and that struct is correctly not called an
 * instance.
 */
predicate mintedBy(UserType t, MacroInvocation mi) {
  exists(TypeDeclarationEntry tde, Location d, Location m |
    tde = t.getDefinition() and d = tde.getLocation() and m = mi.getLocation() and
    d.getFile() = m.getFile() and
    (d.getStartLine() > m.getStartLine()
     or (d.getStartLine() = m.getStartLine() and d.getStartColumn() >= m.getStartColumn())) and
    (d.getEndLine() < m.getEndLine()
     or (d.getEndLine() = m.getEndLine() and d.getEndColumn() <= m.getEndColumn()))
  )
}

from UserType t, MacroInvocation outer, string name
where
  mintedBy(t, outer) and
  not exists(outer.getParentInvocation()) and
  name = canonicalTypeName(t) and
  not isAnonNamed(name)
select name as type_name,
       defFileOf(t) as type_def_file,
       outer.getMacro().getName() as generator_macro,
       pathOf(outer.getMacro().getLocation().getFile()) as generator_def_file
