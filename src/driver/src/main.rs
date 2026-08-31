//! Canonical rustc driver for `crustify-audit unsafe`.
//!
//! Changes to the deterministic safety pass belong here.
//!
//! PoC: a rustc driver (HIR + typeck) that counts three properties of a Rust
//! crate that the regex/`syn` approaches cannot do precisely:
//!
//!   * `unsafe_blocks`        - number of `unsafe { ... }` blocks
//!   * `unsafe_block_stmts`   - statements lexically inside unsafe blocks
//!   * `unsafe_block_lines`   - source lines spanned by unsafe blocks (outermost)
//!   * `raw_ptr_derefs`       - dereferences `*p` where `p: *const T | *mut T`
//!                              (decided by the *type* of the operand via typeck,
//!                              so `*Box`/`*&`/`Deref` impls are NOT counted)
//!   * `raw_ptr_derefs_outside_impl` - of those, the subset outside any
//!                              `impl`/trait body (port-body raw access vs. the
//!                              sanctioned accessor/seam centralisation)
//!
//! Run as a rustc-compatible front end: it compiles the given file/crate and
//! prints the metrics as JSON in `after_analysis`. See `run.sh`.
#![feature(rustc_private)]

extern crate rustc_abi;
extern crate rustc_driver;
extern crate rustc_hir;
extern crate rustc_interface;
extern crate rustc_middle;
extern crate rustc_span;

use rustc_abi::ExternAbi;
use rustc_driver::{Callbacks, Compilation};
use rustc_hir as hir;
use rustc_hir::def::DefKind;
use rustc_hir::intravisit::{self, Visitor, VisitorExt};
use rustc_middle::hir::nested_filter;
use rustc_middle::middle::codegen_fn_attrs::CodegenFnAttrFlags;
use rustc_middle::ty::{self, Ty, TyCtxt, TypeckResults};
use rustc_span::def_id::DefId;
use rustc_span::hygiene::{ExpnKind, MacroKind};
use rustc_span::Span;
use std::collections::{HashMap, HashSet};

/// FFI-seam conversion routines: raw pointers in these signatures are the
/// expected boundary, not a smell. Mirrors `ffibox`'s seam surface
/// (`c_type.rs`, `CCell` + the owning handles) plus the names the ported trees
/// add for callback wrappers.
const SEAM_FNS: &[&str] = &[
    // ffibox
    "as_ptr",
    "as_mut_ptr",
    // the erasure trio: erase shared / erase exclusive / reconstitute
    "as_void_ptr",
    "as_mut_void_ptr",
    "from_void_ptr",
    "from_ptr",
    "from_raw",
    "from_raw_parts",
    "from_raw_uninit",
    // `CCell`'s adopt-a-raw-pointer pair: the same family as `from_raw`, named
    // for the handle each yields (`c_type.rs`).
    "ref_from_raw",
    "mut_from_raw",
    "into_raw",
    "into_raw_parts",
    "into_raw_uninit",
    // ported trees: safe callback wrapper -> raw C fn pointer
    "to_raw",
];

fn is_seam_fn(tcx: TyCtxt<'_>, did: DefId) -> bool {
    tcx.opt_item_name(did)
        .is_some_and(|n| SEAM_FNS.contains(&n.as_str()))
}

/// True if `t` is `PhantomData<..>` -- a ZST, so it is not the wrapper's
/// storage and must be skipped when deciding the storage shape.
fn is_phantom(tcx: TyCtxt<'_>, t: Ty<'_>) -> bool {
    matches!(t.kind(), ty::TyKind::Adt(d, _)
        if tcx.item_name(d.did()).as_str() == "PhantomData")
}

/// True if `t` is POINTER storage -- a raw pointer, or one of the pointer
/// newtypes a handle is `#[repr(transparent)]` over.
fn is_ptr_storage(tcx: TyCtxt<'_>, t: Ty<'_>) -> bool {
    match t.kind() {
        ty::TyKind::RawPtr(..) => true,
        ty::TyKind::Adt(d, _) => matches!(
            tcx.item_name(d.did()).as_str(),
            "CPtr" | "NonNull" | "CBox" | "CBoxUninit" | "CVoidBox"
        ),
        _ => false,
    }
}

/// True if `W` is a TYPE wrapper: a wrapper whose storage IS the C object's
/// bytes (an inline `CType<ffi::T>`), as opposed to a POINTER to it.
///
/// This is the distinction that decides whether a reference is a hazard at all.
/// `&W` over inline storage asserts `noalias` / `readonly` / validity over
/// memory C may write through a pointer it retains; `&W` over a pointer slot
/// asserts it over Rust-owned storage, which is harmless -- that is exactly why
/// access goes through the borrowed handles, which hold the pointer by value.
///
/// Membership is keyed on `CCell` rather than on the field shape, because
/// `CCell` is what the framework itself treats as a wrapper (`CBox<W>`, the
/// `Ref`/`Mut` associated types) and because it resolves AFTER macro expansion,
/// so `define_ctype!`-generated and hand-written wrappers are seen alike. The
/// field shape then splits that set; a wrapper with no non-ZST field is counted
/// as a type wrapper, which keeps the target at 0 rather than silently exempting
/// it.
fn is_type_wrapper(tcx: TyCtxt<'_>, did: DefId) -> bool {
    // A LAYOUT newtype -- storage IS the C object's bytes. `&W` / `&mut W` over
    // it asserts noalias / readonly / validity on memory C may write, so this is
    // the set where a reference is forbidden. A HANDLE holds the pointer by
    // value, covers Rust-owned storage, and is not counted.
    //
    // Decided structurally (see `structural_wrapper`), so a hand-written layout
    // newtype that never declares `CCell` is policed like a generated one.
    matches!(structural_wrapper(tcx, did), Some((true, _)))
}

/// True if `t` is `&W` or `&mut W` where `W` is a LAYOUT newtype (see
/// `structural_wrapper`). Catches the `&self` / `&mut self` receiver (whose
/// type is `&Self` / `&mut Self` = `&W` / `&mut W`) and explicit reference
/// params alike.
///
/// A reference of EITHER kind over a wrapped C object asserts something about
/// memory C may write through a pointer it retains -- `noalias` and `readonly`
/// on the shared form, `noalias` on the exclusive one, and validity on both.
/// Access goes through the borrowed handles instead, which hold the pointer by
/// value and so cover Rust-owned storage. This metric should be 0.
///
/// A reference to a POINTER wrapper is not counted: see `is_type_wrapper`.
fn is_ref_to_type_wrapper(tcx: TyCtxt<'_>, t: Ty<'_>) -> bool {
    if let ty::TyKind::Ref(_, pointee, _) = t.kind() {
        if let ty::TyKind::Adt(def, _) = pointee.kind() {
            return is_type_wrapper(tcx, def.did());
        }
    }
    false
}

/// True if `t` is `*const/*mut c_void` (`core::ffi::c_void`, the type-erased
/// FFI pointer — same type via any alias path).
fn is_void_ptr(tcx: TyCtxt<'_>, t: Ty<'_>) -> bool {
    if let ty::TyKind::RawPtr(pointee, _) = t.kind() {
        if let ty::TyKind::Adt(def, _) = pointee.kind() {
            return tcx.item_name(def.did()).as_str() == "c_void";
        }
    }
    false
}

/// If `t` is a raw pointer `*const/*mut P`, return the pointee `P`.
fn raw_pointee<'tcx>(t: Ty<'tcx>) -> Option<Ty<'tcx>> {
    match t.kind() {
        ty::TyKind::RawPtr(p, _) => Some(*p),
        _ => None,
    }
}

/// True if the pointee `p` is a C type that has a wrapper
/// (i.e. a safe wrapper exists for it) — or is itself such a wrapper.
fn pointee_has_wrapper(tcx: TyCtxt<'_>, p: Ty<'_>, wrapped_c: &HashSet<DefId>) -> bool {
    match p.kind() {
        ty::TyKind::Adt(def, _) => wrapped_c.contains(&def.did()) || is_wrapper(tcx, def.did()),
        _ => false,
    }
}

/// The wrapper inventory of the current crate: `wrapper ADT -> the C types it
/// wraps`, empty where the wrapper names no ADT (`*mut c_void`) — a LAYOUT
/// newtype may carry several. Key presence IS `is_wrapper`; the flattened
/// values are `wrapped_c`.
type Wrappers = HashMap<DefId, Vec<DefId>>;

/// Structural inventory: every local struct `structural_wrapper` admits.
///
/// Nothing here reads a trait, an associated item, or a type name from ffibox,
/// so a hand-written wrapper and a `define_ctype!` one are found alike, and the
/// pass carries no dependency on the framework it audits.
fn scan_structural(tcx: TyCtxt<'_>) -> Wrappers {
    let mut out = HashMap::new();
    for ld in tcx.hir_crate_items(()).definitions() {
        let did = ld.to_def_id();
        if !matches!(tcx.def_kind(did), DefKind::Struct) {
            continue;
        }
        if let Some((_, c)) = structural_wrapper(tcx, did) {
            out.insert(did, c);
        }
    }
    out
}

/// Scan local trait impls for the wrapper seam, reading each wrapper's C type
/// from the `type C` associated item.
///
/// The seam trait is the definition of "wrapper" in this codebase:
/// `define_ctype!` expands to an impl carrying `type C`, and the wrappers the
/// macro cannot express -- generic (`OpensslStackOwned<E>`, `Lhash<E>`) or
/// lifetime-carrying (`Packet<'buf>`, `WPacket<'_>`) -- write the same impl by
/// hand. Keying on the trait therefore covers both, and reads the C type from
/// `type C` instead of guessing at field 0.
///
/// `CLayout` is accepted alongside `CCell` so a tree part-way through a
/// migration still resolves; whichever impl carries `type C` is the one read.
fn scan_wrappers(tcx: TyCtxt<'_>) -> HashMap<DefId, DefId> {
    let mut c_ty = HashMap::new();
    for ld in tcx.hir_crate_items(()).definitions() {
        let did = ld.to_def_id();
        if !matches!(tcx.def_kind(did), DefKind::Impl { of_trait: true }) {
            continue;
        }
        let tr = tcx.impl_trait_ref(did).skip_binder();
        let trait_name = tcx.item_name(tr.def_id);
        if !matches!(trait_name.as_str(), "CCell" | "CLayout") {
            continue;
        }
        let ty::TyKind::Adt(sdef, _) = tr.self_ty().kind() else {
            continue;
        };
        for it in tcx.associated_items(did).in_definition_order() {
            if !matches!(tcx.def_kind(it.def_id), DefKind::AssocTy) {
                continue;
            }
            if tcx.item_name(it.def_id).as_str() != "C" {
                continue;
            }
            if let ty::TyKind::Adt(cdef, _) = tcx.type_of(it.def_id).skip_binder().kind() {
                c_ty.insert(sdef.did(), cdef.did());
            }
        }
    }
    c_ty
}

thread_local! {
    /// Built once per compilation (the driver is one rustc invocation per crate).
    static WRAPPERS: std::cell::RefCell<Option<Wrappers>> =
        const { std::cell::RefCell::new(None) };
    static DECLARED: std::cell::RefCell<Option<HashMap<DefId, DefId>>> =
        const { std::cell::RefCell::new(None) };
}

fn with_wrappers<R>(tcx: TyCtxt<'_>, f: impl FnOnce(&Wrappers) -> R) -> R {
    WRAPPERS.with(|c| {
        let mut b = c.borrow_mut();
        if b.is_none() {
            *b = Some(scan_structural(tcx));
        }
        f(b.as_ref().unwrap())
    })
}

/// The `CCell` / `CLayout`-declared set. Read ONLY by
/// `wrapper_newtypes_declared` / `_nonconformant` / `_undeclared`, where the
/// declaration is the subject being measured rather than a dependency.
fn is_declared_wrapper(tcx: TyCtxt<'_>, did: DefId) -> bool {
    DECLARED.with(|c| {
        let mut b = c.borrow_mut();
        if b.is_none() {
            *b = Some(scan_wrappers(tcx));
        }
        b.as_ref().unwrap().contains_key(&did)
    })
}

/// STRUCTURAL wrapper detection, replacing the `CCell`-declared keying.
///
/// A wrapper is a `#[repr(transparent)]` newtype whose single non-ZST field is,
/// after peeling further transparent newtypes, either
///   * a raw pointer / `NonNull` -- a HANDLE (borrowed or owning), or
///   * a `#[repr(C)]` ADT by value -- a LAYOUT newtype over the C object.
///
/// This is what a wrapper IS, in any crate. Keying on `CCell` measured what an
/// author DECLARED, which cannot see a hand-written wrapper (the translator
/// playbook permits them for lifetime-carrying and generic cases) and
/// which ties the audit to ffibox. Every `CCell` wrapper satisfies the
/// structural test by construction -- ffibox requires `#[repr(transparent)]`
/// over `CType<Self::C>` -- so the structural set SUBSUMES the declared one,
/// and a type that declares `CCell` while failing this test is a wrapper
/// without the layout it claims: reported as `wrapper_declared_nonconformant`,
/// never silently admitted.
fn peel_transparent<'tcx>(tcx: TyCtxt<'tcx>, t: Ty<'tcx>) -> Ty<'tcx> {
    let mut cur = t;
    for _ in 0..8 {
        // depth guard; nesting is 2-3 in practice
        // `NonNull<T>` lowers to a PATTERN type (`*const T is !null`), so a
        // handle peeled to the bottom lands here rather than on `RawPtr`.
        if let ty::TyKind::Pat(base, _) = cur.kind() {
            cur = *base;
            continue;
        }
        let ty::TyKind::Adt(def, args) = cur.kind() else {
            return cur;
        };
        if !def.repr().transparent() || !def.is_struct() {
            return cur;
        }
        let mut inner = None;
        for f in def.all_fields() {
            let ft = tcx.type_of(f.did).instantiate(tcx, args).skip_norm_wip();
            if is_phantom(tcx, ft) || is_zst_marker(tcx, ft) {
                continue;
            }
            inner = Some(ft);
            break;
        }
        match inner {
            Some(i) => cur = i,
            None => return cur,
        }
    }
    cur
}

/// `PhantomPinned` and friends: unit-like ZST markers that are not storage.
fn is_zst_marker(tcx: TyCtxt<'_>, t: Ty<'_>) -> bool {
    matches!(t.kind(), ty::TyKind::Adt(d, _)
        if matches!(tcx.item_name(d.did()).as_str(),
                    "PhantomPinned" | "PhantomData"))
}

/// Peel transparent newtypes, then unwrap arrays/slices, to the ADT a field
/// ultimately stores. `[git_oid; 4]` and `CType<ffi::git_oid>` both land on
/// `git_oid`.
fn field_adt<'tcx>(tcx: TyCtxt<'tcx>, t: Ty<'tcx>) -> Option<ty::AdtDef<'tcx>> {
    let mut cur = peel_transparent(tcx, t);
    for _ in 0..8 {
        match cur.kind() {
            ty::TyKind::Array(e, _) | ty::TyKind::Slice(e) => {
                cur = peel_transparent(tcx, *e);
            }
            ty::TyKind::Adt(d, _) => return Some(*d),
            _ => return None,
        }
    }
    None
}

/// The C types whose BYTES `did` stores inline, transitively.
///
/// A field is C's when it is a `#[repr(C)]` ADT from ANOTHER CRATE. That
/// cross-crate step is what separates a wrapper from C's own aggregate:
/// `git2::Oid { raw: raw::git_oid }` reaches into libgit2-sys, while
/// `libgit2_sys::git_index_entry { ctime: git_index_time, .. }` matches the
/// same repr shape entirely within its own crate and is not a wrapper of
/// anything. Both are `#[repr(C)]` carrying a `#[repr(C)]` field, so nothing
/// short of the crate boundary tells them apart.
///
/// A same-crate `#[repr(C)]` field is RECURSED into rather than accepted, so a
/// wrapper that reaches C through its own intermediate struct still resolves.
/// Depth- and cycle-guarded.
fn embedded_c(
    tcx: TyCtxt<'_>,
    did: DefId,
    depth: u32,
    seen: &mut HashSet<DefId>,
    out: &mut Vec<DefId>,
) {
    if depth > 8 {
        return;
    }
    for f in tcx.adt_def(did).all_fields() {
        let ft = tcx.type_of(f.did).instantiate_identity().skip_norm_wip();
        if is_phantom(tcx, ft) || is_zst_marker(tcx, ft) {
            continue;
        }
        let Some(d) = field_adt(tcx, ft) else {
            continue;
        };
        if !d.repr().c() {
            continue;
        }
        if d.did().krate != did.krate {
            if !out.contains(&d.did()) {
                out.push(d.did());
            }
        } else if seen.insert(d.did()) {
            embedded_c(tcx, d.did(), depth + 1, seen, out);
        }
    }
}

/// `Some((is_layout, cs))` when `did` is structurally a wrapper. `is_layout` is
/// `true` for a LAYOUT newtype (C's bytes inline), `false` for a HANDLE
/// (pointer). `cs` are the C types it wraps, which the peel already reaches:
///
///   * HANDLE -- the POINTEE. `#[repr(transparent)]` over a raw pointer, or
///     over `NonNull` / `CPtr` and friends, which peel to one.
///   * LAYOUT -- every `#[repr(C)]` ADT reached by `embedded_c`. `W` itself
///     must be `#[repr(C)]` or `#[repr(transparent)]`; both give it C's bytes.
///     NOT gated on a single field: a struct carrying a C object beside Rust
///     ones still has those bytes inside it, and `&W` still asserts noalias /
///     readonly / validity across them.
///
/// `cs` is empty for a wrapper naming no ADT (`*mut c_void`).
fn structural_wrapper(tcx: TyCtxt<'_>, did: DefId) -> Option<(bool, Vec<DefId>)> {
    let def = tcx.adt_def(did);
    if !def.is_struct() {
        return None;
    }
    let adt_did = |t: Ty<'_>| match t.kind() {
        ty::TyKind::Adt(d, _) => Some(d.did()),
        _ => None,
    };
    // HANDLE first: a transparent newtype whose storage IS a pointer.
    if def.repr().transparent() {
        let inner = peel_transparent(tcx, tcx.type_of(did).instantiate_identity().skip_norm_wip());
        match inner.kind() {
            ty::TyKind::RawPtr(p, _) | ty::TyKind::Ref(_, p, _) => {
                return Some((false, adt_did(*p).into_iter().collect()));
            }
            ty::TyKind::Adt(d, args) if tcx.item_name(d.did()).as_str() == "NonNull" => {
                // Reached only when the pattern-type peel did not fire; the
                // pointee is the sole type argument.
                return Some((
                    false,
                    args.types().next().and_then(adt_did).into_iter().collect(),
                ));
            }
            _ => {}
        }
    }
    // LAYOUT: C's bytes inline.
    if def.repr().c() || def.repr().transparent() {
        let mut cs = Vec::new();
        let mut seen = HashSet::from([did]);
        embedded_c(tcx, did, 0, &mut seen, &mut cs);
        if !cs.is_empty() {
            return Some((true, cs));
        }
    }
    None
}

/// Is `did` a wrapper type?
fn is_wrapper(tcx: TyCtxt<'_>, did: DefId) -> bool {
    with_wrappers(tcx, |w| w.contains_key(&did))
}

/// The `DefId` of an impl's self-type (`impl T` / `impl Tr for T` -> `T`), via
/// HIR path resolution (no type normalization needed).
fn impl_self_def(tcx: TyCtxt<'_>, impl_did: DefId) -> Option<DefId> {
    let local = impl_did.as_local()?;
    let hir::ItemKind::Impl(imp) = tcx.hir_expect_item(local).kind else {
        return None;
    };
    if let hir::TyKind::Path(hir::QPath::Resolved(_, path)) = imp.self_ty.kind {
        if let hir::def::Res::Def(_, did) = path.res {
            return Some(did);
        }
    }
    None
}

/// True if `did` — or an enclosing item — IS the C-ABI boundary, by either of
/// the two ways a fn can be one:
///
///  * it carries a C symbol name (`#[unsafe(no_mangle)]` / `#[export_name]`),
///    so C callers reach it by that name; or
///  * it has a non-Rust ABI (`extern "C"`), so C reaches it by function
///    pointer — the callback-shim form, which needs no symbol name.
///
/// Both are the port stage's re-export seam: raw and void pointers in such a
/// signature are the C contract (`OPENSSL_sk_freefunc` and friends take
/// type-erased pointers), not a discipline smell, and the unsafe inside belongs
/// to the boundary rather than to the port body.
///
/// The ABI arm was previously a separate `is_extern_c_fn` used ONLY for the
/// pointer-sanctioning sites, so a bare `extern "C"` shim had its pointers
/// excused while its unsafe blocks fell into the unattributed bucket. Folding
/// it in here makes one predicate decide all three.
///
/// Both arms replace an earlier `mod ffi_export` region check: that named a
/// module convention neither ported tree uses, so the sanctioning branch was
/// unreachable and every void pointer fell through to the smell bucket.
fn in_ffi_export(tcx: TyCtxt<'_>, mut did: DefId) -> bool {
    loop {
        if matches!(tcx.def_kind(did), DefKind::Fn | DefKind::AssocFn) {
            let attrs = tcx.codegen_fn_attrs(did);
            if attrs.flags.contains(CodegenFnAttrFlags::NO_MANGLE) || attrs.symbol_name.is_some() {
                return true;
            }
            if tcx.fn_sig(did).skip_binder().skip_binder().abi() != ExternAbi::Rust {
                return true;
            }
        }
        match tcx.opt_parent(did) {
            // Keep walking out of closures / nested bodies into the owning fn;
            // stop at the module boundary.
            Some(p) if !matches!(tcx.def_kind(p), DefKind::Mod | DefKind::ForeignMod) => did = p,
            _ => return false,
        }
    }
}

/// True if `did` is (transitively) inside ANY `impl`/`trait` body (an accessor
/// *definition* — the sanctioned home for raw field projection).
fn in_any_impl(tcx: TyCtxt<'_>, mut did: DefId) -> bool {
    while let Some(parent) = tcx.opt_parent(did) {
        match tcx.def_kind(parent) {
            DefKind::Impl { .. } | DefKind::Trait => return true,
            DefKind::Mod | DefKind::ForeignMod => return false,
            _ => did = parent,
        }
    }
    false
}

/// True if `did` is (transitively) inside an `impl T { .. }` / `impl Tr for T`
/// whose `T` is a wrapper (implements `CCell`).
fn in_wrapper_impl(tcx: TyCtxt<'_>, mut did: DefId) -> bool {
    while let Some(parent) = tcx.opt_parent(did) {
        match tcx.def_kind(parent) {
            DefKind::Impl { .. } => {
                return impl_self_def(tcx, parent).is_some_and(|s| is_wrapper(tcx, s));
            }
            DefKind::Mod | DefKind::ForeignMod => return false,
            _ => did = parent,
        }
    }
    false
}

#[derive(Default)]
struct Counts {
    unsafe_blocks: u64,
    unsafe_block_stmts: u64,
    unsafe_block_lines: u64, // raw brace-to-brace span (incl. blanks/comments)
    unsafe_block_code_lines: u64, // non-blank, non-`//`-comment lines only
    unsafe_blocks_wrapper_impl: u64, // unsafe blocks inside `impl <wrapper T>`
    unsafe_blocks_ffi_export: u64,
    // `unsafe fn` / `unsafe impl` / `unsafe trait` DECLARATIONS. A block is a
    // local assertion its author discharges; an `unsafe fn` pushes the
    // obligation onto every caller, and a `pub` one exports it out of the
    // crate. Same sanctioning axis as everything else: the seam names and the
    // C-ABI gateway are expected, the remainder is the finding.
    unsafe_fns: u64,
    unsafe_fns_seam: u64,
    unsafe_fns_pub: u64,
    unsafe_impls: u64,
    unsafe_traits: u64,
    // Calls to a foreign item -- one declared in an `extern` block
    // (`is_foreign_item`), which is the FFI boundary itself and is
    // crate-agnostic: a bindgen `*-sys` binding, `libc`, or a local
    // `extern "C"` block all resolve to it. Calling one is an unsafe op, so
    // this is the crate-wide unsafe-FFI-call surface. Resolution-based on the
    // callee, so alias- and re-export-proof.
    ffi_calls: u64,
    // Wrapper inventory. `wrapper_newtypes` is the STRUCTURAL count -- every
    // `#[repr(transparent)]` newtype over a pointer or a `#[repr(C)]` type --
    // split into the two roles. The `_declared` pair is the `CCell` baseline
    // this replaces, kept so the two can be compared: `_nonconformant` is a
    // type declaring `CCell` that fails the structural test, i.e. a wrapper
    // without the layout it claims, and `_undeclared` is one the old keying
    // could not see.
    wrapper_newtypes: u64,
    wrapper_newtypes_declared: u64,
    wrapper_declared_nonconformant: u64,
    wrapper_newtypes_undeclared: u64, // unsafe blocks at the C-ABI boundary
    //   (`in_ffi_export`: `#[no_mangle]` /
    //   `#[export_name]` / `extern "C"`)
    // Signature raw pointers. ONE family, with the sanctioned subset named
    // rather than excluded: `raw_ptr_args` + `raw_ptr_rets` is every raw-pointer position
    // in a signature (the denominator), `raw_ptr_seam` the subset that is legitimate
    // by construction, so the smell is `raw_ptr_args + raw_ptr_rets - raw_ptr_seam`. The old
    // scheme split `rp_wrap_nonseam_*` from `rp_outside_*` and counted the seam
    // region in NEITHER, so it reported a numerator with no denominator.
    raw_ptr_args: u64,
    raw_ptr_rets: u64,
    raw_ptr_seam: u64,       // seam fn / C-ABI boundary / ptr-to-own-Self
    raw_ptr_wrapped: u64,    // of the NON-seam remainder, pointee is a wrapped C type
    raw_ptr_in_wrapper: u64, // of the NON-seam remainder, inside `impl <wrapper T>`
    //   — kept because a raw ptr in the very type meant to
    //   hide it is worse than one in a ported free fn
    ref_to_type_wrapper: u64, // `&W` / `&mut W` (incl. the receiver), W a TYPE wrapper
    // `(*p).field` where `p: *C` and `C` has a wrapper (bypasses the
    // accessor): total, and the subset outside any impl/trait (the smell).
    field_proj_wrapped: u64,
    field_proj_outside_impl: u64,
    // `&(*p).field` / `&mut (*p).field` where `p: *C` and `C` has a wrapper --
    // a reference one level down into memory C may write. Should be 0.
    field_ref_wrapped: u64,
    // `*c_void` in signatures: sanctioned (seam / ffi_export) vs smell (elsewhere)
    void_ptr_sanctioned: u64,
    void_ptr_smell: u64,
    raw_ptr_derefs: u64,
    raw_ptr_derefs_outside_impl: u64, // ...of those, the subset NOT in any impl/trait body
    total_stmts: u64,
    code_lines: u64, // crate-wide physical LoC: non-blank, non-`//`-comment source lines
}

/// Per-category source sites `(file, 1-based line)` — the actionable locations
/// the audit consumer acts on, aggregated by `sites_json`.
#[derive(Default)]
struct Sites {
    raw_ptr: Vec<(String, usize)>, // raw ptr to a wrapped C type in a signature
    void_ptr: Vec<(String, usize)>, // `*c_void` smell
    field_proj: Vec<(String, usize)>, // `(*p).field` bypassing the accessor
    field_ref: Vec<(String, usize)>, // `&(*p).field` -- a reference INTO the C object
    raw_deref: Vec<(String, usize)>, // `*p` (raw ptr) outside any impl/trait body
}

/// `(file, 1-based line)` for a span, local-path filename.
fn span_site(tcx: TyCtxt<'_>, span: Span) -> (String, usize) {
    let sm = tcx.sess.source_map();
    let file = sm
        .span_to_filename(span)
        .into_local_path()
        .map(|p| p.display().to_string())
        .unwrap_or_default();
    let line = sm.lookup_char_pos(span.lo()).line;
    (file, line)
}

/// Aggregate `(file, line)` sites into audit.py's
/// `[{"file":..,"count":N,"lines":[..]}]` JSON (one row per file, lines sorted/deduped).
fn sites_json(sites: &[(String, usize)]) -> String {
    use std::collections::BTreeMap;
    let mut by_file: BTreeMap<&str, Vec<usize>> = BTreeMap::new();
    for (f, l) in sites {
        by_file.entry(f.as_str()).or_default().push(*l);
    }
    let rows: Vec<String> = by_file
        .iter()
        .map(|(f, lines)| {
            let mut ls = lines.clone();
            ls.sort_unstable();
            ls.dedup();
            let arr: Vec<String> = ls.iter().map(|l| l.to_string()).collect();
            format!(
                "{{\"file\":\"{}\",\"count\":{},\"lines\":[{}]}}",
                f,
                ls.len(),
                arr.join(",")
            )
        })
        .collect();
    format!("[{}]", rows.join(","))
}

struct BodyVisitor<'a, 'tcx> {
    tcx: TyCtxt<'tcx>,
    typeck: &'tcx TypeckResults<'tcx>,
    depth: u32,       // unsafe-block nesting depth
    in_wrapper: bool, // this body is inside an `impl <wrapper T>`
    in_ffi: bool,     // this body IS / is inside the C-ABI boundary
    in_impl: bool,    // this body is inside any impl/trait
    wrapped_c: &'a HashSet<DefId>,
    c: &'a mut Counts,
    sites: &'a mut Sites,
}

impl<'a, 'tcx> Visitor<'tcx> for BodyVisitor<'a, 'tcx> {
    fn visit_stmt(&mut self, s: &'tcx hir::Stmt<'tcx>) {
        self.c.total_stmts += 1;
        if self.depth > 0 {
            self.c.unsafe_block_stmts += 1;
        }
        intravisit::walk_stmt(self, s);
    }

    fn visit_expr(&mut self, e: &'tcx hir::Expr<'tcx>) {
        match e.kind {
            // `unsafe { ... }`
            hir::ExprKind::Block(b, _)
                if matches!(b.rules, hir::BlockCheckMode::UnsafeBlock(_)) =>
            {
                self.c.unsafe_blocks += 1;
                if self.in_wrapper {
                    // Region attribution only. Where the block's TEXT came from
                    // does not change what it is: an unsafe block in a wrapper
                    // impl is an unsafe block in a wrapper impl.
                    self.c.unsafe_blocks_wrapper_impl += 1;
                }
                if self.in_ffi {
                    self.c.unsafe_blocks_ffi_export += 1;
                }
                // Line metrics: outermost blocks only (nested ones would
                // double-count). EVERY outermost block counts, macro-expanded
                // or not: the metric asks how much unsafe is compiled into this
                // crate, and a block ffibox's `define_ctype!` emitted runs here
                // exactly like one an agent typed. Sanctioning is the only axis
                // that excuses anything, and it does not apply to blocks --
                // `unsafe_blocks_wrapper_impl` / `_ffi_export` ATTRIBUTE a block
                // to a region, they do not exempt it.
                //
                // The earlier exclusion was justified by `span_to_snippet` being
                // unable to reach a macro span's text; that is not so -- every
                // macro-expanded block on libippcp resolves -- and dropping them
                // understated the ratio in proportion to how macro-driven the
                // wrapping is (7.28% against 10.01% here), which is the same bias
                // the `code_lines` rebuild removed from the denominator.
                if self.depth == 0 {
                    let sm = self.tcx.sess.source_map();
                    let lo = sm.lookup_char_pos(b.span.lo()).line;
                    let hi = sm.lookup_char_pos(b.span.hi()).line;
                    self.c.unsafe_block_lines += (hi.saturating_sub(lo) + 1) as u64;
                    // filtered: drop blank + `//`-comment lines (same filter as
                    // the code_LOC denominator, so the ratio is apples-to-apples)
                    if let Ok(snip) = sm.span_to_snippet(b.span) {
                        self.c.unsafe_block_code_lines +=
                            snip.lines()
                                .filter(|l| {
                                    let t = l.trim();
                                    !t.is_empty() && !t.starts_with("//")
                                })
                                .count() as u64;
                    }
                }
                self.depth += 1;
                intravisit::walk_expr(self, e);
                self.depth -= 1;
                return;
            }
            // `*operand` where operand is a raw pointer (type-decided)
            hir::ExprKind::Unary(hir::UnOp::Deref, inner) => {
                if self.typeck.expr_ty(inner).is_raw_ptr() {
                    self.c.raw_ptr_derefs += 1;
                    // The actionable split: derefs in wrapper accessor / seam
                    // bodies (inside an impl) are the sanctioned centralisation;
                    // those outside any impl are port-body raw access.
                    if !self.in_impl {
                        self.c.raw_ptr_derefs_outside_impl += 1;
                        self.sites.raw_deref.push(span_site(self.tcx, e.span));
                    }
                }
            }
            // `(*p).field` where `p: *C` and `C` has a wrapper (also covers the
            // `addr_of!((*p).field)` form, whose operand IS this field expr).
            hir::ExprKind::Field(base, _) => {
                if let hir::ExprKind::Unary(hir::UnOp::Deref, inner) = base.kind {
                    if let ty::TyKind::RawPtr(pointee, _) = self.typeck.expr_ty(inner).kind() {
                        if pointee_has_wrapper(self.tcx, *pointee, self.wrapped_c) {
                            self.c.field_proj_wrapped += 1;
                            if !self.in_impl {
                                self.c.field_proj_outside_impl += 1;
                                self.sites.field_proj.push(span_site(self.tcx, e.span));
                            }
                        }
                    }
                }
            }
            // `&(*p).field` / `&mut (*p).field` where `p: *C` and `C` has a
            // wrapper: a reference over a FIELD of memory C may write -- the
            // same rule that keeps `&W` out, one level down. `addr_of!` /
            // `&raw` lower to `BorrowKind::Raw`, so matching only
            // `BorrowKind::Ref` is exactly the sanctioned/forbidden split.
            hir::ExprKind::AddrOf(hir::BorrowKind::Ref, _, operand) => {
                if let hir::ExprKind::Field(base, _) = operand.kind {
                    if let hir::ExprKind::Unary(hir::UnOp::Deref, inner) = base.kind {
                        if let ty::TyKind::RawPtr(pointee, _) = self.typeck.expr_ty(inner).kind() {
                            if pointee_has_wrapper(self.tcx, *pointee, self.wrapped_c) {
                                self.c.field_ref_wrapped += 1;
                                self.sites.field_ref.push(span_site(self.tcx, e.span));
                            }
                        }
                    }
                }
            }
            _ => {}
        }
        intravisit::walk_expr(self, e);
    }
}

/// The `ffibox` macros, tallied by expansion site. The `define_*ctype!`
/// family emits the wrapper newtype (one per representation); the `impl_*!`
/// family binds a lifecycle contract or an ownership marker to it.
const CRUSTIFY_MACROS: &[&str] = &[
    "define_ctype",
    "impl_dropped",
    "impl_cloned",
    "impl_cvalued",
];

/// Recursively tally references to crustify-crate structs in a type.
fn count_ty(tcx: TyCtxt<'_>, t: Ty<'_>, m: &mut std::collections::BTreeMap<String, u64>) {
    match t.kind() {
        ty::TyKind::Adt(def, args) => {
            let did = def.did();
            if tcx.crate_name(did.krate).as_str() == "crustify" {
                *m.entry(tcx.item_name(did).to_string()).or_default() += 1;
            }
            for a in args.types() {
                count_ty(tcx, a, m);
            }
        }
        ty::TyKind::RawPtr(p, _) | ty::TyKind::Ref(_, p, _) => count_ty(tcx, *p, m),
        ty::TyKind::Slice(e) | ty::TyKind::Array(e, _) => count_ty(tcx, *e, m),
        _ => {}
    }
}

/// `UM_MODE=usage`: profile crustify-crate primitive usage.
///  - `types`: references to the smart-pointer / cell structs in type positions
///    (fn signatures, struct/enum/union fields, const/alias types)
///  - `trait_impls`: `impl <crustify trait> for T` counts
///  - `macros`: distinct invocations of the crustify `*!` macros
///  - `ffi_calls`: per-`crate::symbol` count of every call to a foreign fn
///    (`tcx.is_foreign_item` — declared in an `extern` block), crate-agnostic
///    (bindgen `*-sys`, `libc`, local `extern "C"`). Calling one is unsafe, so
///    this is the crate-wide unsafe-FFI-call surface.
///  - `ffi_call_sites`: those calls grouped `{crate::symbol: {region: [{file,count,lines}]}}`
///    where region is `free_fn` / `inherent_impl` / `trait_impl:<Trait>` — so a
///    `git__free` in `trait_impl:CDropped` (a sanctioned wrapper dtor) is separable
///    from one in a `free_fn` port body (actionable smell)
fn usage_json(tcx: TyCtxt<'_>, krate: rustc_span::Symbol) -> String {
    use std::collections::BTreeMap;
    let mut types: BTreeMap<String, u64> = BTreeMap::new();
    let mut trait_impls: BTreeMap<String, u64> = BTreeMap::new();
    let mut macros: BTreeMap<String, HashSet<rustc_span::ExpnId>> = BTreeMap::new();
    // Crate-wide scan: every call to a foreign fn — one declared in an `extern`
    // block (`tcx.is_foreign_item`), which is the FFI boundary itself and is
    // crate-agnostic (bindgen `*-sys`, `libc`, or a local `extern "C"` block all
    // resolve to it). Calling one is an unsafe op, so this is the unsafe-FFI-call
    // surface. Resolution-based (callee `DefId`), so alias-/re-export-proof and
    // multi-line-safe. Keyed `crate::symbol` so same-named foreign fns from
    // different crates (e.g. `libc::close` vs a sys binding) stay distinct.
    let mut ffi_calls: BTreeMap<String, u64> = BTreeMap::new();
    // crate::symbol -> region ("free_fn" | "inherent_impl" | "trait_impl:<Trait>")
    // -> sites. The region separates wrapper-teardown chokepoints (a `git__free`
    // in `trait_impl:CDropped` / `:CLenDropped`) from port-body smell (`free_fn` /
    // `inherent_impl`), so the actionable subset is a filter, not a judgement.
    let mut ffi_sites: BTreeMap<String, BTreeMap<String, Vec<(String, usize)>>> = BTreeMap::new();
    for owner in tcx.hir_body_owners() {
        let region = call_region(tcx, owner.to_def_id());
        let typeck = tcx.typeck(owner);
        let mut callees: Vec<(DefId, rustc_span::Span)> = Vec::new();
        CallCollector {
            typeck,
            out: &mut callees,
        }
        .visit_body(tcx.hir_body_owned_by(owner));
        for (did, sp) in callees {
            if tcx.is_foreign_item(did) {
                let key = format!("{}::{}", tcx.crate_name(did.krate), tcx.item_name(did));
                *ffi_calls.entry(key.clone()).or_default() += 1;
                ffi_sites
                    .entry(key)
                    .or_default()
                    .entry(region.clone())
                    .or_default()
                    .push(span_site(tcx, sp));
            }
        }
    }

    for ld in tcx.hir_crate_items(()).definitions() {
        let did = ld.to_def_id();
        match tcx.def_kind(did) {
            DefKind::Fn | DefKind::AssocFn => {
                let sig = tcx.fn_sig(did).skip_binder().skip_binder();
                for t in sig
                    .inputs()
                    .iter()
                    .copied()
                    .chain(std::iter::once(sig.output()))
                {
                    count_ty(tcx, t, &mut types);
                }
            }
            DefKind::Struct | DefKind::Enum | DefKind::Union => {
                for f in tcx.adt_def(did).all_fields() {
                    count_ty(tcx, tcx.type_of(f.did).skip_binder(), &mut types);
                }
            }
            DefKind::Impl { of_trait: true } => {
                let tdid = tcx.impl_trait_ref(did).skip_binder().def_id;
                if tcx.crate_name(tdid.krate).as_str() == "crustify" {
                    *trait_impls
                        .entry(tcx.item_name(tdid).to_string())
                        .or_default() += 1;
                }
            }
            _ => {}
        }
        // distinct macro invocations (items from one invocation share an ExpnId)
        let ctxt = tcx.def_span(did).ctxt();
        if let ExpnKind::Macro(MacroKind::Bang, name) = ctxt.outer_expn_data().kind {
            if let Some(last) = name.as_str().rsplit("::").next() {
                if CRUSTIFY_MACROS.contains(&last) {
                    macros
                        .entry(last.to_string())
                        .or_default()
                        .insert(ctxt.outer_expn());
                }
            }
        }
    }

    let obj = |m: &BTreeMap<String, u64>| {
        m.iter()
            .map(|(k, v)| format!("\"{k}\":{v}"))
            .collect::<Vec<_>>()
            .join(",")
    };
    let macros_obj = macros
        .iter()
        .map(|(k, s)| format!("\"{k}\":{}", s.len()))
        .collect::<Vec<_>>()
        .join(",");
    let ffi_sites_obj = ffi_sites
        .iter()
        .map(|(sym, by_region)| {
            let inner = by_region
                .iter()
                .map(|(region, sites)| format!("\"{region}\":{}", sites_json(sites)))
                .collect::<Vec<_>>()
                .join(",");
            format!("\"{sym}\":{{{}}}", inner)
        })
        .collect::<Vec<_>>()
        .join(",");
    format!(
        "{{\"crate\":\"{krate}\",\"types\":{{{}}},\"trait_impls\":{{{}}},\"macros\":{{{}}},\"ffi_calls\":{{{}}},\"ffi_call_sites\":{{{}}}}}",
        obj(&types), obj(&trait_impls), macros_obj, obj(&ffi_calls), ffi_sites_obj
    )
}

// ---------------------------------------------------------------- seed mode

fn enclosing_impl_self(tcx: TyCtxt<'_>, mut did: DefId) -> Option<DefId> {
    while let Some(parent) = tcx.opt_parent(did) {
        match tcx.def_kind(parent) {
            DefKind::Impl { .. } => return impl_self_def(tcx, parent),
            DefKind::Mod | DefKind::ForeignMod => return None,
            _ => did = parent,
        }
    }
    None
}

fn enclosing_fn(tcx: TyCtxt<'_>, mut did: DefId) -> Option<rustc_hir::def_id::LocalDefId> {
    loop {
        if matches!(tcx.def_kind(did), DefKind::Fn | DefKind::AssocFn) {
            return did.as_local();
        }
        match tcx.opt_parent(did) {
            Some(parent) if !matches!(tcx.def_kind(parent), DefKind::Mod | DefKind::ForeignMod) => {
                did = parent;
            }
            _ => return None,
        }
    }
}

/// Classify a body owner's enclosing region, for grouping ffi-call sites:
/// `trait_impl:<Trait>` (a call inside `impl Trait for T` — e.g. the
/// `CDropped` / `CLenDropped` wrapper-teardown chokepoints), `inherent_impl`
/// (a method in `impl T { .. }`), or `free_fn` (a free function or any
/// other body not in an impl).
fn call_region(tcx: TyCtxt<'_>, mut did: DefId) -> String {
    while let Some(parent) = tcx.opt_parent(did) {
        match tcx.def_kind(parent) {
            DefKind::Impl { of_trait } => {
                return if of_trait {
                    let tdid = tcx.impl_trait_ref(parent).skip_binder().def_id;
                    format!("trait_impl:{}", tcx.item_name(tdid))
                } else {
                    "inherent_impl".to_string()
                };
            }
            DefKind::Mod | DefKind::ForeignMod => break,
            _ => did = parent,
        }
    }
    "free_fn".to_string()
}

/// Collect free-function callee `DefId`s + call-site spans in a body, for FFI
/// call classification and named-symbol resolution.
struct CallCollector<'a, 'tcx> {
    typeck: &'tcx TypeckResults<'tcx>,
    out: &'a mut Vec<(DefId, Span)>,
}
impl<'a, 'tcx> Visitor<'tcx> for CallCollector<'a, 'tcx> {
    fn visit_expr(&mut self, e: &'tcx hir::Expr<'tcx>) {
        if let hir::ExprKind::Call(f, _) = e.kind {
            if let hir::ExprKind::Path(ref qp) = f.kind {
                if let hir::def::Res::Def(_, did) = self.typeck.qpath_res(qp, f.hir_id) {
                    self.out.push((did, e.span));
                }
            }
        }
        intravisit::walk_expr(self, e);
    }
}

/// If `did` is a `type A = B;` alias, the ADT it names.
///
/// A HIR path resolves to whatever the source literally wrote, so
/// `*mut ffi::ENGINE` resolves to the alias `ENGINE`, never to `engine_st`.
/// Named type sites accept both spellings through this one-hop resolution;
/// bindgen's `pub type ENGINE = engine_st;` form is the common case.
fn alias_target(tcx: TyCtxt<'_>, did: DefId) -> Option<DefId> {
    if !matches!(tcx.def_kind(did), DefKind::TyAlias) {
        return None;
    }
    match tcx.type_of(did).skip_binder().kind() {
        ty::TyKind::Adt(def, _) => Some(def.did()),
        _ => None,
    }
}

/// Collect requested names mentioned by one HIR type, through one alias hop.
struct NamedPathVisitor<'a, 'tcx> {
    tcx: TyCtxt<'tcx>,
    wanted: &'a HashSet<String>,
    hits: &'a mut HashSet<String>,
}
impl<'a, 'tcx> Visitor<'tcx> for NamedPathVisitor<'a, 'tcx> {
    fn visit_ty(&mut self, t: &'tcx hir::Ty<'tcx, hir::AmbigArg>) {
        if let hir::TyKind::Path(hir::QPath::Resolved(_, path)) = t.kind {
            if let hir::def::Res::Def(_, did) = path.res {
                let direct = self.tcx.item_name(did).to_string();
                if self.wanted.contains(&direct) {
                    self.hits.insert(direct);
                }
                if let Some(target) = alias_target(self.tcx, did) {
                    let underlying = self.tcx.item_name(target).to_string();
                    if self.wanted.contains(&underlying) {
                        self.hits.insert(underlying);
                    }
                }
            }
        }
        intravisit::walk_ty(self, t);
    }
}

/// Find raw-pointer type declarations that mention a requested C type.
struct RawPtrDeclVisitor<'a, 'tcx> {
    tcx: TyCtxt<'tcx>,
    wanted: &'a HashSet<String>,
    sites: &'a mut HashMap<String, Vec<(String, usize)>>,
}
impl<'a, 'tcx> Visitor<'tcx> for RawPtrDeclVisitor<'a, 'tcx> {
    type NestedFilter = nested_filter::All;

    fn maybe_tcx(&mut self) -> Self::MaybeTyCtxt {
        self.tcx
    }

    fn visit_ty(&mut self, t: &'tcx hir::Ty<'tcx, hir::AmbigArg>) {
        if let hir::TyKind::Ptr(p) = t.kind {
            let mut hits = HashSet::new();
            NamedPathVisitor {
                tcx: self.tcx,
                wanted: self.wanted,
                hits: &mut hits,
            }
            .visit_ty_unambig(p.ty);
            let site = span_site(self.tcx, t.span);
            for name in hits {
                self.sites.entry(name).or_default().push(site.clone());
            }
        }
        intravisit::walk_ty(self, t);
    }
}

fn semantic_names(
    tcx: TyCtxt<'_>,
    t: Ty<'_>,
    wanted: &HashSet<String>,
    hits: &mut HashSet<String>,
) {
    match t.kind() {
        ty::TyKind::Adt(def, args) => {
            let name = tcx.item_name(def.did()).to_string();
            if wanted.contains(&name) {
                hits.insert(name);
            }
            for arg in args.types() {
                semantic_names(tcx, arg, wanted, hits);
            }
        }
        ty::TyKind::RawPtr(p, _) | ty::TyKind::Ref(_, p, _) => {
            semantic_names(tcx, *p, wanted, hits)
        }
        ty::TyKind::Slice(e) | ty::TyKind::Array(e, _) => semantic_names(tcx, *e, wanted, hits),
        ty::TyKind::Tuple(ts) => {
            for t in ts.iter() {
                semantic_names(tcx, t, wanted, hits);
            }
        }
        _ => {}
    }
}

/// Find `*p` expressions where `p` points at a requested C type.
struct RawDerefVisitor<'a, 'tcx> {
    tcx: TyCtxt<'tcx>,
    typeck: &'tcx TypeckResults<'tcx>,
    wanted: &'a HashSet<String>,
    sites: &'a mut HashMap<String, Vec<(String, usize)>>,
}
impl<'a, 'tcx> Visitor<'tcx> for RawDerefVisitor<'a, 'tcx> {
    fn visit_expr(&mut self, e: &'tcx hir::Expr<'tcx>) {
        if let hir::ExprKind::Unary(hir::UnOp::Deref, inner) = e.kind {
            if let ty::TyKind::RawPtr(pointee, _) = self.typeck.expr_ty(inner).kind() {
                let mut hits = HashSet::new();
                semantic_names(self.tcx, *pointee, self.wanted, &mut hits);
                let site = span_site(self.tcx, e.span);
                for name in hits {
                    self.sites.entry(name).or_default().push(site.clone());
                }
            }
        }
        intravisit::walk_expr(self, e);
    }
}

/// Find every raw-pointer declaration in one symbol's signature/body region.
struct AnyRawPtrDeclVisitor<'a, 'tcx> {
    tcx: TyCtxt<'tcx>,
    sites: &'a mut Vec<(String, usize)>,
}
impl<'a, 'tcx> Visitor<'tcx> for AnyRawPtrDeclVisitor<'a, 'tcx> {
    fn visit_ty(&mut self, t: &'tcx hir::Ty<'tcx, hir::AmbigArg>) {
        if matches!(t.kind, hir::TyKind::Ptr(_)) {
            self.sites.push(span_site(self.tcx, t.span));
        }
        intravisit::walk_ty(self, t);
    }
}

/// Find every raw-pointer dereference in one symbol's body region.
struct AnyRawDerefVisitor<'a, 'tcx> {
    tcx: TyCtxt<'tcx>,
    typeck: &'tcx TypeckResults<'tcx>,
    sites: &'a mut Vec<(String, usize)>,
}
impl<'a, 'tcx> Visitor<'tcx> for AnyRawDerefVisitor<'a, 'tcx> {
    fn visit_expr(&mut self, e: &'tcx hir::Expr<'tcx>) {
        if let hir::ExprKind::Unary(hir::UnOp::Deref, inner) = e.kind {
            if matches!(self.typeck.expr_ty(inner).kind(), ty::TyKind::RawPtr(..)) {
                self.sites.push(span_site(self.tcx, e.span));
            }
        }
        intravisit::walk_expr(self, e);
    }
}

/// Requested source or linked symbol names represented by a function DefId.
fn matching_symbol_names(tcx: TyCtxt<'_>, did: DefId, wanted: &HashSet<String>) -> HashSet<String> {
    let mut hits = HashSet::new();
    if !matches!(tcx.def_kind(did), DefKind::Fn | DefKind::AssocFn) {
        return hits;
    }
    if let Some(item_name) = tcx.opt_item_name(did) {
        let name = item_name.to_string();
        if wanted.contains(&name) {
            hits.insert(name);
        }
    }
    if let Some(symbol_name) = tcx.codegen_fn_attrs(did).symbol_name {
        let name = symbol_name.to_string();
        if wanted.contains(&name) {
            hits.insert(name);
        }
    }
    hits
}

fn add_symbol_sites(
    names: &HashSet<String>,
    sites: &[(String, usize)],
    out: &mut HashMap<String, Vec<(String, usize)>>,
) {
    for name in names {
        out.entry(name.clone())
            .or_default()
            .extend_from_slice(sites);
    }
}

/// Resolve function/link-name seeds. A local definition owns its signature and
/// body. A call to an external C symbol assigns the enclosing wrapper body's
/// raw-pointer surface to that symbol as well.
fn collect_symbol_sites(
    tcx: TyCtxt<'_>,
    wanted: &HashSet<String>,
    raw_ptr: &mut HashMap<String, Vec<(String, usize)>>,
    raw_deref: &mut HashMap<String, Vec<(String, usize)>>,
) -> HashSet<String> {
    let mut resolved = HashSet::new();

    // Definitions without bodies (notably `extern` declarations) still seed
    // and contribute their signature when they live in an audited crate.
    for ld in tcx.hir_crate_items(()).definitions() {
        let did = ld.to_def_id();
        let hits = matching_symbol_names(tcx, did, wanted);
        if hits.is_empty() {
            continue;
        }
        resolved.extend(hits.iter().cloned());
        if let Some(local) = did.as_local() {
            if let Some(decl) = tcx.expect_hir_owner_node(local).fn_decl() {
                let mut sites = Vec::new();
                AnyRawPtrDeclVisitor {
                    tcx,
                    sites: &mut sites,
                }
                .visit_fn_decl(decl);
                add_symbol_sites(&hits, &sites, raw_ptr);
            }
        }
    }

    for owner in tcx.hir_body_owners() {
        let body = tcx.hir_body_owned_by(owner);
        let typeck = tcx.typeck(owner);
        let mut hits = matching_symbol_names(tcx, owner.to_def_id(), wanted);
        let mut calls = Vec::new();
        CallCollector {
            typeck,
            out: &mut calls,
        }
        .visit_body(body);
        for (callee, _) in calls {
            hits.extend(matching_symbol_names(tcx, callee, wanted));
        }
        if hits.is_empty() {
            continue;
        }
        resolved.extend(hits.iter().cloned());

        // Include the enclosing wrapper signature for call-resolved symbols.
        // `hir_body_owners()` also yields closures. Walk through those to the
        // owning function: a closure DefId owns a body but no item node, while
        // the wrapper signature still belongs to the symbol call it encloses.
        if let Some(enclosing) = enclosing_fn(tcx, owner.to_def_id()) {
            if let Some(decl) = tcx.expect_hir_owner_node(enclosing).fn_decl() {
                let mut sites = Vec::new();
                AnyRawPtrDeclVisitor {
                    tcx,
                    sites: &mut sites,
                }
                .visit_fn_decl(decl);
                add_symbol_sites(&hits, &sites, raw_ptr);
            }
        }

        let mut ptr_sites = Vec::new();
        AnyRawPtrDeclVisitor {
            tcx,
            sites: &mut ptr_sites,
        }
        .visit_body(body);
        add_symbol_sites(&hits, &ptr_sites, raw_ptr);

        let mut deref_sites = Vec::new();
        AnyRawDerefVisitor {
            tcx,
            typeck,
            sites: &mut deref_sites,
        }
        .visit_body(body);
        add_symbol_sites(&hits, &deref_sites, raw_deref);
    }
    resolved
}

/// Local structural wrappers grouped by the requested C type they contain.
fn named_wrappers(tcx: TyCtxt<'_>, wanted: &HashSet<String>) -> HashMap<DefId, Vec<String>> {
    with_wrappers(tcx, |wrappers| {
        wrappers
            .iter()
            .filter_map(|(wrapper, c_types)| {
                let mut names: Vec<String> = c_types
                    .iter()
                    .filter_map(|c_type| {
                        let name = tcx.item_name(*c_type).to_string();
                        wanted.contains(&name).then_some(name)
                    })
                    .collect();
                names.sort();
                names.dedup();
                (!names.is_empty()).then_some((*wrapper, names))
            })
            .collect()
    })
}

fn hir_adt_def<A>(tcx: TyCtxt<'_>, t: &hir::Ty<'_, A>) -> Option<DefId> {
    let hir::TyKind::Path(hir::QPath::Resolved(_, path)) = t.kind else {
        return None;
    };
    let hir::def::Res::Def(_, did) = path.res else {
        return None;
    };
    Some(alias_target(tcx, did).unwrap_or(did))
}

/// Find explicit `&[Wrapper]` and `&mut [Wrapper]` materializations.
struct WrapperSliceVisitor<'a, 'tcx> {
    tcx: TyCtxt<'tcx>,
    wrappers: &'a HashMap<DefId, Vec<String>>,
    shared: &'a mut HashMap<String, Vec<(String, usize)>>,
    mutable: &'a mut HashMap<String, Vec<(String, usize)>>,
}
impl<'a, 'tcx> Visitor<'tcx> for WrapperSliceVisitor<'a, 'tcx> {
    type NestedFilter = nested_filter::All;

    fn maybe_tcx(&mut self) -> Self::MaybeTyCtxt {
        self.tcx
    }

    fn visit_ty(&mut self, t: &'tcx hir::Ty<'tcx, hir::AmbigArg>) {
        if let hir::TyKind::Ref(_, borrowed) = t.kind {
            if let hir::TyKind::Slice(element) = borrowed.ty.kind {
                if let Some(wrapper) = hir_adt_def(self.tcx, element) {
                    if let Some(names) = self.wrappers.get(&wrapper) {
                        let site = span_site(self.tcx, t.span);
                        let sites = if borrowed.mutbl == hir::Mutability::Mut {
                            &mut self.mutable
                        } else {
                            &mut self.shared
                        };
                        for name in names {
                            sites.entry(name.clone()).or_default().push(site.clone());
                        }
                    }
                }
            }
        }
        intravisit::walk_ty(self, t);
    }
}

/// Find expressions that form or return a wrapper slice even when its type is
/// inferred (notably `slice::from_raw_parts::<Wrapper>(...)`).
struct MaterializedSliceVisitor<'a, 'tcx> {
    tcx: TyCtxt<'tcx>,
    typeck: &'tcx TypeckResults<'tcx>,
    wrappers: &'a HashMap<DefId, Vec<String>>,
    shared: &'a mut HashMap<String, Vec<(String, usize)>>,
    mutable: &'a mut HashMap<String, Vec<(String, usize)>>,
}
impl<'a, 'tcx> Visitor<'tcx> for MaterializedSliceVisitor<'a, 'tcx> {
    fn visit_expr(&mut self, e: &'tcx hir::Expr<'tcx>) {
        if matches!(
            e.kind,
            hir::ExprKind::AddrOf(..) | hir::ExprKind::Call(..) | hir::ExprKind::MethodCall(..)
        ) {
            if let ty::TyKind::Ref(_, pointee, mutbl) = self.typeck.expr_ty_adjusted(e).kind() {
                if let ty::TyKind::Slice(element) = pointee.kind() {
                    if let ty::TyKind::Adt(def, _) = element.kind() {
                        if let Some(names) = self.wrappers.get(&def.did()) {
                            let site = span_site(self.tcx, e.span);
                            let sites = if *mutbl == hir::Mutability::Mut {
                                &mut self.mutable
                            } else {
                                &mut self.shared
                            };
                            for name in names {
                                sites.entry(name.clone()).or_default().push(site.clone());
                            }
                        }
                    }
                }
            }
        }
        intravisit::walk_expr(self, e);
    }
}

/// `UM_MODE=seed`: requested C-type or symbol sites.
fn seed_json(tcx: TyCtxt<'_>, krate: rustc_span::Symbol) -> String {
    let mut names: Vec<String> = std::env::var("UM_SEED_NAME")
        .unwrap_or_default()
        .split_whitespace()
        .map(str::to_string)
        .collect();
    names.sort();
    names.dedup();
    let wanted: HashSet<String> = names.iter().cloned().collect();
    let mut raw_ptr: HashMap<String, Vec<(String, usize)>> = HashMap::new();
    let mut raw_deref: HashMap<String, Vec<(String, usize)>> = HashMap::new();
    let mut deref_impl: HashMap<String, Vec<(String, usize)>> = HashMap::new();
    let mut deref_mut_impl: HashMap<String, Vec<(String, usize)>> = HashMap::new();
    let mut slice_ref: HashMap<String, Vec<(String, usize)>> = HashMap::new();
    let mut slice_mut: HashMap<String, Vec<(String, usize)>> = HashMap::new();
    let wrappers = named_wrappers(tcx, &wanted);

    tcx.hir_walk_toplevel_module(&mut RawPtrDeclVisitor {
        tcx,
        wanted: &wanted,
        sites: &mut raw_ptr,
    });
    tcx.hir_walk_toplevel_module(&mut WrapperSliceVisitor {
        tcx,
        wrappers: &wrappers,
        shared: &mut slice_ref,
        mutable: &mut slice_mut,
    });
    for owner in tcx.hir_body_owners() {
        RawDerefVisitor {
            tcx,
            typeck: tcx.typeck(owner),
            wanted: &wanted,
            sites: &mut raw_deref,
        }
        .visit_body(tcx.hir_body_owned_by(owner));
        MaterializedSliceVisitor {
            tcx,
            typeck: tcx.typeck(owner),
            wrappers: &wrappers,
            shared: &mut slice_ref,
            mutable: &mut slice_mut,
        }
        .visit_body(tcx.hir_body_owned_by(owner));
    }
    let symbol_hits = collect_symbol_sites(tcx, &wanted, &mut raw_ptr, &mut raw_deref);
    // ffibox itself intentionally implements Deref/DerefMut for its borrowed
    // handles. Report only manual implementations in wrapper crates.
    if krate.as_str() != "ffibox" {
        for ld in tcx.hir_crate_items(()).definitions() {
            let did = ld.to_def_id();
            if !matches!(tcx.def_kind(did), DefKind::Impl { of_trait: true }) {
                continue;
            }
            let trait_did = tcx.impl_trait_ref(did).skip_binder().def_id;
            if tcx.crate_name(trait_did.krate).as_str() != "core" {
                continue;
            }
            let trait_symbol = tcx.item_name(trait_did);
            let trait_name = trait_symbol.as_str();
            if !matches!(trait_name, "Deref" | "DerefMut") {
                continue;
            }
            let Some(wrapper) = impl_self_def(tcx, did) else {
                continue;
            };
            let Some(wrapper_names) = wrappers.get(&wrapper) else {
                continue;
            };
            let site = span_site(tcx, tcx.def_span(did));
            let sites = if trait_name == "DerefMut" {
                &mut deref_mut_impl
            } else {
                &mut deref_impl
            };
            for name in wrapper_names {
                sites.entry(name.clone()).or_default().push(site.clone());
            }
        }
    }

    let entries: Vec<String> = names.iter().filter_map(|name| {
        let ptr = raw_ptr.get(name).map(Vec::as_slice).unwrap_or(&[]);
        let deref = raw_deref.get(name).map(Vec::as_slice).unwrap_or(&[]);
        let deref_impls = deref_impl.get(name).map(Vec::as_slice).unwrap_or(&[]);
        let deref_mut_impls = deref_mut_impl.get(name).map(Vec::as_slice).unwrap_or(&[]);
        let shared_slices = slice_ref.get(name).map(Vec::as_slice).unwrap_or(&[]);
        let mutable_slices = slice_mut.get(name).map(Vec::as_slice).unwrap_or(&[]);
        if !symbol_hits.contains(name) && ptr.is_empty() && deref.is_empty()
            && deref_impls.is_empty()
            && deref_mut_impls.is_empty() && shared_slices.is_empty()
            && mutable_slices.is_empty()
        {
            return None;
        }
        Some(format!(
            "{{\"name\":\"{}\",\"raw_ptr_sites\":{},\"raw_deref_sites\":{},\"deref_impl_sites\":{},\"deref_mut_impl_sites\":{},\"slice_ref_sites\":{},\"slice_mut_sites\":{}}}",
            name, sites_json(ptr), sites_json(deref), sites_json(deref_impls),
            sites_json(deref_mut_impls), sites_json(shared_slices),
            sites_json(mutable_slices),
        ))
    }).collect();
    format!(
        "{{\"crate\":\"{krate}\",\"seeds\":[{}]}}",
        entries.join(",")
    )
}

struct MetricsCallbacks;

impl Callbacks for MetricsCallbacks {
    fn after_analysis(
        &mut self,
        _compiler: &rustc_interface::interface::Compiler,
        tcx: TyCtxt<'_>,
    ) -> Compilation {
        let krate = tcx.crate_name(rustc_span::def_id::LOCAL_CRATE);
        // Under cargo, only emit for workspace primary packages (skips deps and
        // build scripts); standalone (run.sh) always emits.
        let under_cargo = std::env::var_os("CARGO").is_some();
        let primary = std::env::var_os("CARGO_PRIMARY_PACKAGE").is_some();
        if under_cargo && (!primary || krate.as_str() == "build_script_build") {
            return Compilation::Continue;
        }
        // Usage mode: primitive-usage profile, separate from the unsafe metrics.
        if std::env::var("UM_MODE").as_deref() == Ok("usage") {
            println!("{}", usage_json(tcx, krate));
            return Compilation::Continue;
        }
        // Named mode prints its site entries, then falls through to emit the
        // crate-wide metrics block from the same compilation. The dispatcher
        // merges both stdout lines per crate.
        if std::env::var("UM_MODE").as_deref() == Ok("seed") {
            println!("{}", seed_json(tcx, krate));
        }
        if std::env::var_os("UM_DEBUG").is_some() {
            let (mut ns, mut nw, mut shown) = (0u32, 0u32, 0u32);
            for ld in tcx.hir_crate_items(()).definitions() {
                let did = ld.to_def_id();
                if matches!(tcx.def_kind(did), DefKind::Struct) {
                    ns += 1;
                    let nm = match tcx.def_span(did).ctxt().outer_expn_data().kind {
                        ExpnKind::Macro(_, s) => s.to_string(),
                        k => format!("{k:?}"),
                    };
                    if is_wrapper(tcx, did) {
                        nw += 1;
                    }
                    if shown < 10 {
                        eprintln!("  STRUCT {} expn={}", tcx.item_name(did), nm);
                        shown += 1;
                    }
                }
            }
            eprintln!("CRATE structs={ns} detected_wrappers={nw}");
        }
        // Set of C types that have a wrapper (a safe wrapper exists). Each
        // wrapper contributes its seam's `type C`, either representation.
        let wrapped_c: HashSet<DefId> =
            with_wrappers(tcx, |w| w.values().flatten().copied().collect());

        let mut c = Counts::default();
        let mut sites = Sites::default();
        // Every body owner (fn, closure, const/static initializer, ...). Each is
        // a separate typeck context; intravisit does not descend into nested
        // bodies, so visiting every owner covers the whole crate exactly once.
        // Declaration-shaped metrics need their own pass: `hir_body_owners()`
        // yields only fns, closures and initializers, so a struct / impl /
        // trait never reaches it.
        for ld in tcx.hir_crate_items(()).definitions() {
            let did = ld.to_def_id();
            match tcx.def_kind(did) {
                DefKind::Struct => {
                    let is_layout = matches!(structural_wrapper(tcx, did), Some((true, _)));
                    let declared = is_declared_wrapper(tcx, did);
                    // LAYOUT newtypes only. A handle is a wrapper too, but it
                    // is not the set this audit polices: `&handle` covers
                    // Rust-owned pointer storage and is ordinary, while `&W` on
                    // a layout newtype is the hazard `ref_to_type_wrapper`
                    // exists to keep at 0.
                    if is_layout {
                        c.wrapper_newtypes += 1;
                        if !declared {
                            c.wrapper_newtypes_undeclared += 1
                        }
                    }
                    if declared {
                        c.wrapper_newtypes_declared += 1;
                        if !is_layout {
                            c.wrapper_declared_nonconformant += 1
                        }
                    }
                }
                DefKind::Impl { of_trait: true } => {
                    // `TrivialClone` is a compiler-internal marker `#[derive(Clone)]`
                    // emits as an `unsafe impl`; it asserts nothing the author
                    // wrote and would swamp the real lifecycle contracts.
                    let internal = tcx
                        .item_name(tcx.impl_trait_ref(did).skip_binder().def_id)
                        .as_str()
                        == "TrivialClone";
                    if !internal && tcx.impl_trait_header(did).safety.is_unsafe() {
                        c.unsafe_impls += 1;
                    }
                }
                DefKind::Trait => {
                    if tcx.trait_def(did).safety.is_unsafe() {
                        c.unsafe_traits += 1;
                    }
                }
                _ => {}
            }
        }

        for owner in tcx.hir_body_owners() {
            let did = owner.to_def_id();
            {
                let typeck = tcx.typeck(owner);
                let mut callees: Vec<(DefId, Span)> = Vec::new();
                CallCollector {
                    typeck,
                    out: &mut callees,
                }
                .visit_body(tcx.hir_body_owned_by(owner));
                c.ffi_calls += callees
                    .iter()
                    .filter(|(d, _)| tcx.is_foreign_item(*d))
                    .count() as u64;
            }
            let in_wrapper = in_wrapper_impl(tcx, did);
            let in_ffi = in_ffi_export(tcx, did);
            let in_impl = in_any_impl(tcx, did);
            {
                let typeck = tcx.typeck(owner);
                let body = tcx.hir_body_owned_by(owner);
                let mut v = BodyVisitor {
                    tcx,
                    typeck,
                    depth: 0,
                    in_wrapper,
                    in_ffi,
                    in_impl,
                    wrapped_c: &wrapped_c,
                    c: &mut c,
                    sites: &mut sites,
                };
                v.visit_body(body);
            }
            // Signature analysis (fns only).
            if matches!(tcx.def_kind(did), DefKind::Fn | DefKind::AssocFn) {
                let sig = tcx.fn_sig(did).skip_binder().skip_binder();
                let seam = is_seam_fn(tcx, did);
                // `&mut <wrapper>` and `*c_void` anywhere in the signature.
                for t in sig
                    .inputs()
                    .iter()
                    .copied()
                    .chain(std::iter::once(sig.output()))
                {
                    if is_ref_to_type_wrapper(tcx, t) {
                        c.ref_to_type_wrapper += 1;
                    }
                    if is_void_ptr(tcx, t) {
                        if seam || in_ffi {
                            c.void_ptr_sanctioned += 1;
                        } else {
                            c.void_ptr_smell += 1;
                            sites.void_ptr.push(span_site(tcx, tcx.def_span(did)));
                        }
                    }
                }
                // `unsafe fn` DECLARATIONS. Sanctioned the same way a pointer
                // position is: a seam conversion (`from_ptr`, `from_raw`,
                // `from_void_ptr`) and the C-ABI gateway are expected to be
                // unsafe; anything else is exporting an obligation.
                if sig.safety().is_unsafe() {
                    c.unsafe_fns += 1;
                    if seam || in_ffi {
                        c.unsafe_fns_seam += 1
                    }
                    if tcx.visibility(did).is_public() {
                        c.unsafe_fns_pub += 1
                    }
                }
                // Raw-pointer args/rets: count EVERY position, then name the
                // sanctioned subset. No position goes unreported.
                let sanctioned = seam || in_ffi;
                // Resolution-based self-boundary: a raw ptr to the method's OWN
                // wrapper type (`*mut Self` in `free`/`dispose`/`dup`/…) is the
                // type's raw-form lifecycle seam, not a "use the wrapper" smell
                // (you can't pass `&Self` while destroying/duplicating it). Skip.
                let own_self = enclosing_impl_self(tcx, did);
                {
                    let mut tally = |p: Ty<'_>, is_ret: bool, c: &mut Counts| {
                        if is_ret {
                            c.raw_ptr_rets += 1
                        } else {
                            c.raw_ptr_args += 1
                        }
                        // A raw ptr to the method's OWN wrapper type (`*mut Self`
                        // in `free`/`dup`) is the type's raw-form lifecycle seam —
                        // you cannot pass `&Self` while destroying it. Sanctioned,
                        // and now COUNTED as such instead of dropped silently.
                        let is_own =
                            own_self.is_some_and(|s| p.ty_adt_def().map(|d| d.did()) == Some(s));
                        if sanctioned || is_own {
                            c.raw_ptr_seam += 1;
                            return;
                        }
                        // The actionable smell is a raw ptr to the *C type* when a
                        // wrapper exists (`*mut ffi::git_oid` → should be GitOid).
                        // A raw ptr to the *wrapper itself* (`*mut GitOid`) already
                        // uses the wrapper — kept raw deliberately (stored back-ptr
                        // / array boundary), not a smell. So count only the C case.
                        let w = matches!(p.kind(),
                            ty::TyKind::Adt(def, _) if wrapped_c.contains(&def.did()));
                        if w {
                            c.raw_ptr_wrapped += 1
                        }
                        if in_wrapper {
                            c.raw_ptr_in_wrapper += 1
                        }
                        if w {
                            sites.raw_ptr.push(span_site(tcx, tcx.def_span(did)));
                        }
                    };
                    for inp in sig.inputs() {
                        if let Some(p) = raw_pointee(*inp) {
                            tally(p, false, &mut c);
                        }
                    }
                    if let Some(p) = raw_pointee(sig.output()) {
                        tally(p, true, &mut c);
                    }
                }
            }
        }
        // Crate-wide code LoC — the denominator. Built from the COMPILED crate
        // (the union of HIR definition spans), not from the raw file text.
        //
        // Reading the text counted every line physically present, including
        // items `cfg` removed before HIR: an inline `#[cfg(test)] mod tests`
        // put its lines in the denominator while its bodies — never compiled
        // under `cargo build` — could reach no numerator, so the unsafe ratio
        // read low in proportion to test coverage. A definition that did not
        // survive `cfg` has no `DefId`, so unioning def spans excludes it by
        // construction, and generalises to every `#[cfg(..)]`-disabled item
        // (feature gates, platform gates) rather than special-casing `test`.
        //
        // Two details make the union tight:
        //  * `source_callsite()` maps a macro-expanded def back to its
        //    invocation, so generated items are charged to the `define_ctype!`
        //    line that produced them rather than to ffibox's source.
        //  * CONTAINERS (`mod` / `impl` / `trait` / `extern` blocks) contribute
        //    only their opening and closing lines, never their full span: a
        //    container's span still covers the text of items `cfg` stripped out
        //    of it (the crate-root module's span is the whole file), so
        //    unioning it whole would reinstate exactly what this is removing.
        //    Their members are separate definitions and bring their own bodies.
        //  * BODIES are unioned separately, because `def_span` on a fn is its
        //    SIGNATURE span, not the item's. Definitions alone counted headers
        //    and no statements -- 4208 lines where git2's source has 20990,
        //    which read as an 83% unsafe ratio.
        //
        // The line set is per file and deduplicated, so items sharing a line
        // (or several defs from one macro invocation) count it once. The
        // blank / `//`-comment filter is the same one `unsafe_block_code_lines`
        // applies, so numerator and denominator stay apples-to-apples.
        {
            let sm = tcx.sess.source_map();
            // file (keyed by its source-map start offset) -> covered 1-based lines
            let mut covered: HashMap<u32, HashSet<usize>> = HashMap::new();
            for ld in tcx.hir_crate_items(()).definitions() {
                let did = ld.to_def_id();
                let container = matches!(
                    tcx.def_kind(did),
                    DefKind::Mod | DefKind::Impl { .. } | DefKind::Trait | DefKind::ForeignMod
                );
                let sp = tcx.def_span(did).source_callsite();
                let lo = sm.lookup_char_pos(sp.lo());
                let hi = sm.lookup_char_pos(sp.hi());
                // Local crate only (imported files carry `src == None`), and
                // skip the pathological span that straddles two files.
                if lo.file.src.is_none() || lo.file.start_pos != hi.file.start_pos {
                    continue;
                }
                let e = covered.entry(lo.file.start_pos.0).or_default();
                if container {
                    e.insert(lo.line);
                    e.insert(hi.line);
                } else {
                    e.extend(lo.line..=hi.line);
                }
            }
            // Bodies: fn / const / static initializers and closures. Same
            // `cfg` guarantee -- a stripped item owns no body -- and the same
            // dedup, so a body overlapping its own signature line counts once.
            for owner in tcx.hir_body_owners() {
                let sp = tcx.hir_body_owned_by(owner).value.span.source_callsite();
                let lo = sm.lookup_char_pos(sp.lo());
                let hi = sm.lookup_char_pos(sp.hi());
                if lo.file.src.is_none() || lo.file.start_pos != hi.file.start_pos {
                    continue;
                }
                covered
                    .entry(lo.file.start_pos.0)
                    .or_default()
                    .extend(lo.line..=hi.line);
            }
            for sf in sm.files().iter() {
                let (Some(src), Some(set)) = (&sf.src, covered.get(&sf.start_pos.0)) else {
                    continue;
                };
                c.code_lines += src
                    .lines()
                    .enumerate()
                    .filter(|(i, l)| {
                        let t = l.trim();
                        set.contains(&(i + 1)) && !t.is_empty() && !t.starts_with("//")
                    })
                    .count() as u64;
            }
        }
        println!(
            "{{\"crate\":\"{}\",\"unsafe_blocks\":{},\"unsafe_block_stmts\":{},\"unsafe_block_lines\":{},\"unsafe_block_code_lines\":{},\"unsafe_blocks_wrapper_impl\":{},\"unsafe_blocks_ffi_export\":{},\"unsafe_fns\":{},\"unsafe_fns_seam\":{},\"unsafe_fns_pub\":{},\"unsafe_impls\":{},\"unsafe_traits\":{},\"ffi_calls\":{},\"wrapper_newtypes\":{},\"wrapper_newtypes_declared\":{},\"wrapper_declared_nonconformant\":{},\"wrapper_newtypes_undeclared\":{},\"raw_ptr_args\":{},\"raw_ptr_rets\":{},\"raw_ptr_seam\":{},\"raw_ptr_wrapped\":{},\"raw_ptr_in_wrapper\":{},\"ref_to_type_wrapper\":{},\"field_proj_wrapped\":{},\"field_proj_outside_impl\":{},\"field_ref_wrapped\":{},\"void_ptr_sanctioned\":{},\"void_ptr_smell\":{},\"raw_ptr_derefs\":{},\"raw_ptr_derefs_outside_impl\":{},\"total_stmts\":{},\"code_lines\":{},\"raw_ptr_sites\":{},\"void_ptr_sites\":{},\"field_proj_sites\":{},\"field_ref_sites\":{},\"raw_deref_sites\":{}}}",
            krate, c.unsafe_blocks, c.unsafe_block_stmts, c.unsafe_block_lines, c.unsafe_block_code_lines, c.unsafe_blocks_wrapper_impl, c.unsafe_blocks_ffi_export, c.unsafe_fns, c.unsafe_fns_seam, c.unsafe_fns_pub, c.unsafe_impls, c.unsafe_traits, c.ffi_calls, c.wrapper_newtypes, c.wrapper_newtypes_declared, c.wrapper_declared_nonconformant, c.wrapper_newtypes_undeclared, c.raw_ptr_args, c.raw_ptr_rets, c.raw_ptr_seam, c.raw_ptr_wrapped, c.raw_ptr_in_wrapper, c.ref_to_type_wrapper, c.field_proj_wrapped, c.field_proj_outside_impl, c.field_ref_wrapped, c.void_ptr_sanctioned, c.void_ptr_smell, c.raw_ptr_derefs, c.raw_ptr_derefs_outside_impl, c.total_stmts, c.code_lines,
            sites_json(&sites.raw_ptr), sites_json(&sites.void_ptr), sites_json(&sites.field_proj), sites_json(&sites.field_ref), sites_json(&sites.raw_deref)
        );
        Compilation::Continue
    }
}

fn main() {
    let mut args: Vec<String> = std::env::args().collect();
    if !args
        .iter()
        .any(|a| a == "--sysroot" || a.starts_with("--sysroot="))
    {
        // `SYSROOT` env (set to the *nightly* sysroot) wins; else ask rustc.
        let sysroot = std::env::var("SYSROOT").unwrap_or_else(|_| {
            let out = std::process::Command::new("rustc")
                .args(["--print", "sysroot"])
                .output()
                .expect("run rustc --print sysroot");
            String::from_utf8(out.stdout).unwrap().trim().to_string()
        });
        args.push("--sysroot".into());
        args.push(sysroot);
    }
    rustc_driver::run_compiler(&args, &mut MetricsCallbacks);
}
