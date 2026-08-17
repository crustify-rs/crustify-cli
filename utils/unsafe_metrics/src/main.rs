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
use rustc_hir::intravisit::{self, Visitor};
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
    "as_ptr", "as_mut_ptr", "as_void_ptr",
    "from_ptr", "from_raw", "from_raw_parts", "from_raw_uninit",
    "into_raw", "into_raw_parts", "into_raw_uninit",
    // ported trees: safe callback wrapper -> raw C fn pointer
    "to_raw",
];

fn is_seam_fn(tcx: TyCtxt<'_>, did: DefId) -> bool {
    tcx.opt_item_name(did).is_some_and(|n| SEAM_FNS.contains(&n.as_str()))
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
            "CPtr" | "NonNull" | "CBox" | "CBoxUninit" | "CVoidBox"),
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
    if !is_wrapper(tcx, did) {
        return false;
    }
    for f in tcx.adt_def(did).all_fields() {
        let ft = tcx.type_of(f.did).skip_binder();
        if is_phantom(tcx, ft) {
            continue;
        }
        return !is_ptr_storage(tcx, ft);
    }
    true
}

/// True if `t` is `&W` or `&mut W` where `W` is a TYPE wrapper (implements
/// `CCell` and stores the C object inline). Catches the `&self` / `&mut self`
/// receiver (whose type is `&Self` / `&mut Self` = `&W` / `&mut W`) and
/// explicit reference params alike.
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
        ty::TyKind::Adt(def, _) => {
            wrapped_c.contains(&def.did()) || is_wrapper(tcx, def.did())
        }
        _ => false,
    }
}

/// The wrapper inventory of the current crate: `wrapper ADT -> its C type ADT`.
type Wrappers = HashMap<DefId, DefId>;

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
fn scan_wrappers(tcx: TyCtxt<'_>) -> Wrappers {
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
        let ty::TyKind::Adt(sdef, _) = tr.self_ty().kind() else { continue };
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
}

fn with_wrappers<R>(tcx: TyCtxt<'_>, f: impl FnOnce(&Wrappers) -> R) -> R {
    WRAPPERS.with(|c| {
        let mut b = c.borrow_mut();
        if b.is_none() {
            *b = Some(scan_wrappers(tcx));
        }
        f(b.as_ref().unwrap())
    })
}

/// Is `did` a wrapper type?
fn is_wrapper(tcx: TyCtxt<'_>, did: DefId) -> bool {
    with_wrappers(tcx, |w| w.contains_key(&did))
}

/// The C type `did` wraps, from its seam's `type C`.
fn wrapper_c_ty(tcx: TyCtxt<'_>, did: DefId) -> Option<DefId> {
    with_wrappers(tcx, |w| w.get(&did).copied())
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

/// True if `did` — or an enclosing item — is exported under a C symbol name:
/// `#[unsafe(no_mangle)]` or an explicit symbol name (`#[export_name]`).
///
/// This is the port stage's re-export seam. A function carrying a C symbol name
/// IS the C-ABI boundary — C callers reach it by that name — so raw and void
/// pointers in its signature are sanctioned rather than a discipline smell.
///
/// Replaces an earlier `mod ffi_export` region check: that named a module
/// convention neither ported tree uses, so the sanctioning branch was
/// unreachable and every void pointer fell through to the smell bucket.
/// Reading the codegen attrs also covers `#[export_name]`, which renames a
/// symbol without `no_mangle` (`CodegenFnAttrs::symbol_name`).
fn in_ffi_export(tcx: TyCtxt<'_>, mut did: DefId) -> bool {
    loop {
        if matches!(tcx.def_kind(did), DefKind::Fn | DefKind::AssocFn) {
            let attrs = tcx.codegen_fn_attrs(did);
            if attrs.flags.contains(CodegenFnAttrFlags::NO_MANGLE)
                || attrs.symbol_name.is_some()
            {
                return true;
            }
        }
        match tcx.opt_parent(did) {
            // Keep walking out of closures / nested bodies into the owning fn;
            // stop at the module boundary.
            Some(p) if !matches!(tcx.def_kind(p), DefKind::Mod | DefKind::ForeignMod) => {
                did = p
            }
            _ => return false,
        }
    }
}

/// True if `did` is an `extern "C"` fn — a callback shim or a C export.
///
/// Its `*mut c_void` parameters are the C callback ABI (`OPENSSL_sk_freefunc`
/// and friends take type-erased pointers by contract), so they are sanctioned
/// rather than counted as a void-pointer smell.
fn is_extern_c_fn(tcx: TyCtxt<'_>, did: DefId) -> bool {
    if !matches!(tcx.def_kind(did), DefKind::Fn | DefKind::AssocFn) {
        return false;
    }
    tcx.fn_sig(did).skip_binder().skip_binder().abi() != ExternAbi::Rust
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
                return impl_self_def(tcx, parent)
                    .is_some_and(|s| is_wrapper(tcx, s));
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
    unsafe_block_lines: u64,      // raw brace-to-brace span (incl. blanks/comments)
    unsafe_block_code_lines: u64, // non-blank, non-`//`-comment lines only
    unsafe_blocks_wrapper_impl: u64, // unsafe blocks inside `impl <wrapper T>`
    wrapper_impl_macro: u64,         //   of which macro-generated (get/get_mut)
    wrapper_impl_handwritten: u64,   //   of which hand-written methods
    unsafe_blocks_ffi_export: u64,   // unsafe blocks inside `mod ffi_export`
    // Signature raw pointers. ONE family, with the sanctioned subset named
    // rather than excluded: `rp_args` + `rp_rets` is every raw-pointer position
    // in a signature (the denominator), `rp_seam` the subset that is legitimate
    // by construction, so the smell is `rp_args + rp_rets - rp_seam`. The old
    // scheme split `rp_wrap_nonseam_*` from `rp_outside_*` and counted the seam
    // region in NEITHER, so it reported a numerator with no denominator.
    rp_args: u64,
    rp_rets: u64,
    rp_seam: u64,      // seam fn / `mod ffi_export` / `extern "C"` / ptr-to-own-Self
    rp_wrapped: u64,   // of the NON-seam remainder, pointee is a wrapped C type
    rp_in_wrapper: u64, // of the NON-seam remainder, inside `impl <wrapper T>`
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
/// the `crustify audit` consumer (wrap/port agents) act on. Mirrors audit.py's
/// `naked_sites` / `*_smell_sites` shape after `sites_json` aggregation.
#[derive(Default)]
struct Sites {
    naked: Vec<(String, usize)>,       // tree-wide naked `ffi::C` / `*-sys` use
    raw_ptr: Vec<(String, usize)>,     // raw ptr to a wrapped C type in a signature
    void_ptr: Vec<(String, usize)>,    // `*c_void` smell
    field_proj: Vec<(String, usize)>,  // `(*p).field` bypassing the accessor
    field_ref: Vec<(String, usize)>,   // `&(*p).field` -- a reference INTO the C object
    raw_deref: Vec<(String, usize)>,   // `*p` (raw ptr) outside any impl/trait body
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
    in_ffi: bool,     // this body is inside a `mod ffi_export`
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
                    self.c.unsafe_blocks_wrapper_impl += 1;
                    if b.span.from_expansion() {
                        self.c.wrapper_impl_macro += 1;
                    } else {
                        self.c.wrapper_impl_handwritten += 1;
                    }
                }
                if self.in_ffi {
                    self.c.unsafe_blocks_ffi_export += 1;
                }
                if self.depth == 0 {
                    // count lines only for the outermost unsafe block (avoid
                    // double-counting nested blocks)
                    let sm = self.tcx.sess.source_map();
                    let lo = sm.lookup_char_pos(b.span.lo()).line;
                    let hi = sm.lookup_char_pos(b.span.hi()).line;
                    self.c.unsafe_block_lines += (hi.saturating_sub(lo) + 1) as u64;
                    // filtered: drop blank + `//`-comment lines (same filter as
                    // the code_LOC denominator, so the ratio is apples-to-apples)
                    if let Ok(snip) = sm.span_to_snippet(b.span) {
                        self.c.unsafe_block_code_lines += snip
                            .lines()
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
                        if let ty::TyKind::RawPtr(pointee, _) =
                            self.typeck.expr_ty(inner).kind()
                        {
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
    "define_ctype", "impl_dropped", "impl_cloned", "impl_cvalued",
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
        CallCollector { typeck, out: &mut callees }.visit_body(tcx.hir_body_owned_by(owner));
        for (did, sp) in callees {
            if tcx.is_foreign_item(did) {
                let key = format!("{}::{}", tcx.crate_name(did.krate), tcx.item_name(did));
                *ffi_calls.entry(key.clone()).or_default() += 1;
                ffi_sites.entry(key).or_default().entry(region.clone()).or_default()
                    .push(span_site(tcx, sp));
            }
        }
    }

    for ld in tcx.hir_crate_items(()).definitions() {
        let did = ld.to_def_id();
        match tcx.def_kind(did) {
            DefKind::Fn | DefKind::AssocFn => {
                let sig = tcx.fn_sig(did).skip_binder().skip_binder();
                for t in sig.inputs().iter().copied().chain(std::iter::once(sig.output())) {
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
                    *trait_impls.entry(tcx.item_name(tdid).to_string()).or_default() += 1;
                }
            }
            _ => {}
        }
        // distinct macro invocations (items from one invocation share an ExpnId)
        let ctxt = tcx.def_span(did).ctxt();
        if let ExpnKind::Macro(MacroKind::Bang, name) = ctxt.outer_expn_data().kind {
            if let Some(last) = name.as_str().rsplit("::").next() {
                if CRUSTIFY_MACROS.contains(&last) {
                    macros.entry(last.to_string()).or_default().insert(ctxt.outer_expn());
                }
            }
        }
    }

    let obj = |m: &BTreeMap<String, u64>| {
        m.iter().map(|(k, v)| format!("\"{k}\":{v}")).collect::<Vec<_>>().join(",")
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

#[derive(Clone, Copy, PartialEq)]
enum SeedKind {
    Type,
    Func,
}

struct Seed {
    name: String,
    kind: SeedKind,
    repr: DefId,          // wrapper struct (Type) or fn (Func)
    c_did: Option<DefId>, // wrapped C type (Type seed) — naked type-ref target
    c_name: String,       // C tag — naked match for `*-sys` fn calls (Func seed)
}

fn def_file(tcx: TyCtxt<'_>, did: DefId) -> String {
    tcx.sess.source_map().span_to_filename(tcx.def_span(did)).into_local_path()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// Resolve the seed set from `UM_SEED_*` env filters (union semantics).
fn resolve_seeds(tcx: TyCtxt<'_>) -> Vec<Seed> {
    let names: Vec<String> = std::env::var("UM_SEED_NAME")
        .unwrap_or_default().split_whitespace().map(str::to_string).collect();
    let file = std::env::var("UM_SEED_FILE").ok().filter(|s| !s.is_empty());
    let dir = std::env::var("UM_SEED_DIR").ok().filter(|s| !s.is_empty());
    let all = std::env::var_os("UM_SEED_ALL").is_some();

    let mut seeds = Vec::new();
    for ld in tcx.hir_crate_items(()).definitions() {
        let did = ld.to_def_id();
        let kind = match tcx.def_kind(did) {
            DefKind::Struct if is_wrapper(tcx, did) => SeedKind::Type,
            DefKind::Fn | DefKind::AssocFn => SeedKind::Func,
            _ => continue,
        };
        let rust_name = tcx.item_name(did).to_string();
        let (c_did, c_name) = match kind {
            SeedKind::Type => {
                let c = wrapper_c_ty(tcx, did);
                let cn = c.map(|c| tcx.item_name(c).to_string()).unwrap_or_default();
                (c, cn)
            }
            // For a fn, the C tag is its name (ffi_export re-exports carry the C name).
            SeedKind::Func => (None, rust_name.clone()),
        };
        let matched = if !names.is_empty() {
            names.iter().any(|n| *n == rust_name || *n == c_name)
        } else if let Some(f) = &file {
            def_file(tcx, did).ends_with(f.as_str())
        } else if let Some(d) = &dir {
            def_file(tcx, did).contains(d.as_str())
        } else {
            all
        };
        if matched {
            seeds.push(Seed { name: rust_name, kind, repr: did, c_did, c_name });
        }
    }
    seeds
}

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

/// Recursive count of `target` Adt occurrences in a type.
fn count_ty_did(t: Ty<'_>, target: DefId) -> u64 {
    let mut n = 0;
    match t.kind() {
        ty::TyKind::Adt(def, args) => {
            if def.did() == target {
                n += 1;
            }
            for a in args.types() {
                n += count_ty_did(a, target);
            }
        }
        ty::TyKind::RawPtr(p, _) | ty::TyKind::Ref(_, p, _) => n += count_ty_did(*p, target),
        ty::TyKind::Slice(e) | ty::TyKind::Array(e, _) => n += count_ty_did(*e, target),
        _ => {}
    }
    n
}

/// Collect free-fn call callee `DefId`s + their call-site spans in a body
/// (for `ffi::S(` / `*-sys` naked detection with locations).
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
/// `*mut ffi::ENGINE` resolves to the ALIAS `ENGINE`, never to `engine_st`.
/// Typeck normalizes, so the naked COUNT sees through it — without this the
/// SITES would not, and a count would land with nowhere to look. bindgen
/// re-emits OpenSSL's typedefs (`pub type ENGINE = engine_st;`), so this is the
/// common spelling rather than a corner case.
fn alias_target(tcx: TyCtxt<'_>, did: DefId) -> Option<DefId> {
    if !matches!(tcx.def_kind(did), DefKind::TyAlias) {
        return None;
    }
    match tcx.type_of(did).skip_binder().kind() {
        ty::TyKind::Adt(def, _) => Some(def.did()),
        _ => None,
    }
}

/// Collect spans of HIR type-references that resolve to `target` (a seed's
/// wrapped C type), for type-seed `naked_sites`. The naked COUNT is
/// typeck-based (`count_ty_did`); these spans are the syntactic occurrences the
/// agent actually edits, matched directly or through one typedef hop.
struct NakedTyVisitor<'a, 'tcx> {
    tcx: TyCtxt<'tcx>,
    target: DefId,
    out: &'a mut Vec<Span>,
}
impl<'a, 'tcx> Visitor<'tcx> for NakedTyVisitor<'a, 'tcx> {
    fn visit_ty(&mut self, t: &'tcx hir::Ty<'tcx, hir::AmbigArg>) {
        if let hir::TyKind::Path(hir::QPath::Resolved(_, path)) = t.kind {
            if let hir::def::Res::Def(_, did) = path.res {
                if did == self.target
                    || alias_target(self.tcx, did) == Some(self.target)
                {
                    self.out.push(t.span);
                }
            }
        }
        intravisit::walk_ty(self, t);
    }
}

/// `UM_MODE=seed`: per-seed audit metrics (own-region + naked footprint).
fn seed_json(tcx: TyCtxt<'_>, krate: rustc_span::Symbol) -> String {
    // wrapped C types (for naked-ref matching + wrapper detection reuse).
    let wrapped_c: HashSet<DefId> = with_wrappers(tcx, |w| w.values().copied().collect());
    let seeds = resolve_seeds(tcx);
    let mut metrics: Vec<Counts> = (0..seeds.len()).map(|_| Counts::default()).collect();
    let mut sites: Vec<Sites> = (0..seeds.len()).map(|_| Sites::default()).collect();
    let mut region_owners: Vec<u64> = vec![0; seeds.len()];
    let mut naked: Vec<u64> = vec![0; seeds.len()];

    for owner in tcx.hir_body_owners() {
        let did = owner.to_def_id();
        let impl_self = enclosing_impl_self(tcx, did);
        // which seed (if any) owns this body?
        let region = seeds.iter().position(|s| match s.kind {
            SeedKind::Type => impl_self == Some(s.repr),
            SeedKind::Func => did == s.repr,
        });

        if let Some(i) = region {
            region_owners[i] += 1;
            let in_wrapper = in_wrapper_impl(tcx, did);
            let in_ffi = in_ffi_export(tcx, did);
            let in_impl = in_any_impl(tcx, did);
            {
                let typeck = tcx.typeck(owner);
                let body = tcx.hir_body_owned_by(owner);
                let mut v = BodyVisitor {
                    tcx, typeck, depth: 0, in_wrapper, in_ffi, in_impl,
                    wrapped_c: &wrapped_c, c: &mut metrics[i], sites: &mut sites[i],
                };
                v.visit_body(body);
            }
            if matches!(tcx.def_kind(did), DefKind::Fn | DefKind::AssocFn) {
                let sig = tcx.fn_sig(did).skip_binder().skip_binder();
                let seam = is_seam_fn(tcx, did);
                let extern_c = is_extern_c_fn(tcx, did);
                let span = tcx.def_span(did);
                for t in sig.inputs().iter().copied().chain(std::iter::once(sig.output())) {
                    if is_ref_to_type_wrapper(tcx, t) {
                        metrics[i].ref_to_type_wrapper += 1;
                    }
                    if is_void_ptr(tcx, t) && !(seam || in_ffi || extern_c) {
                        metrics[i].void_ptr_smell += 1;
                        sites[i].void_ptr.push(span_site(tcx, span));
                    }
                    // raw ptr to a wrapped C type in a non-seam region signature:
                    // a "use the wrapper" smell, recorded with its site.
                    if !seam {
                        if let Some(p) = raw_pointee(t) {
                            if matches!(p.kind(), ty::TyKind::Adt(def, _)
                                if wrapped_c.contains(&def.did()))
                            {
                                sites[i].raw_ptr.push(span_site(tcx, span));
                            }
                        }
                    }
                }
            }
        }

        // naked: only consider non-sanctioned owners.
        let sanctioned = is_seam_fn(tcx, did)
            || in_ffi_export(tcx, did)
            || tcx.def_span(did).from_expansion();
        if sanctioned {
            continue;
        }
        // type-seed naked: refs to `ffi::C` in this fn's signature. COUNT via
        // typeck (`count_ty_did`, alias-proof); SITES via the HIR signature
        // (`NakedTyVisitor`, the syntactic occurrences the agent edits).
        if matches!(tcx.def_kind(did), DefKind::Fn | DefKind::AssocFn) {
            let sig = tcx.fn_sig(did).skip_binder().skip_binder();
            let hir_id = tcx.local_def_id_to_hir_id(owner);
            let decl = tcx.hir_node(hir_id).fn_decl();
            for (i, s) in seeds.iter().enumerate() {
                if let (SeedKind::Type, Some(c)) = (s.kind, s.c_did) {
                    for t in sig.inputs().iter().copied().chain(std::iter::once(sig.output())) {
                        naked[i] += count_ty_did(t, c);
                    }
                    if let Some(decl) = decl {
                        let mut spans: Vec<Span> = Vec::new();
                        let mut v = NakedTyVisitor { tcx, target: c, out: &mut spans };
                        for t in decl.inputs {
                            if let Some(at) = t.try_as_ambig_ty() {
                                v.visit_ty(at);
                            }
                        }
                        if let hir::FnRetTy::Return(t) = decl.output {
                            if let Some(at) = t.try_as_ambig_ty() {
                                v.visit_ty(at);
                            }
                        }
                        for sp in spans {
                            sites[i].naked.push(span_site(tcx, sp));
                        }
                    }
                }
            }
        }
        // func-seed naked: calls to a `*-sys` fn matching the C tag, in this body.
        let typeck = tcx.typeck(owner);
        let mut callees: Vec<(DefId, Span)> = Vec::new();
        CallCollector { typeck, out: &mut callees }.visit_body(tcx.hir_body_owned_by(owner));
        for (cdid, cspan) in callees {
            let kn = tcx.crate_name(cdid.krate);
            if kn.as_str().ends_with("_sys") {
                let nm = tcx.item_name(cdid);
                for (i, s) in seeds.iter().enumerate() {
                    if s.kind == SeedKind::Func && s.c_name == nm.as_str() {
                        naked[i] += 1;
                        sites[i].naked.push(span_site(tcx, cspan));
                    }
                }
            }
        }
    }

    let entries: Vec<String> = seeds.iter().enumerate().map(|(i, s)| {
        let m = &metrics[i];
        let st = &sites[i];
        let kind = if s.kind == SeedKind::Type { "type" } else { "func" };
        format!(
            "{{\"name\":\"{}\",\"c_name\":\"{}\",\"kind\":\"{}\",\"region_owners\":{},\"unsafe_blocks\":{},\"unsafe_block_code_lines\":{},\"wrapper_macro\":{},\"wrapper_handwritten\":{},\"raw_ptr_derefs\":{},\"field_proj\":{},\"field_proj_outside_impl\":{},\"field_ref_wrapped\":{},\"ref_to_type_wrapper\":{},\"void_ptr_smell\":{},\"naked\":{},\"naked_sites\":{},\"raw_ptr_sites\":{},\"void_ptr_sites\":{},\"field_proj_sites\":{}}}",
            s.name, s.c_name, kind, region_owners[i], m.unsafe_blocks, m.unsafe_block_code_lines,
            m.wrapper_impl_macro, m.wrapper_impl_handwritten, m.raw_ptr_derefs,
            m.field_proj_wrapped, m.field_proj_outside_impl, m.field_ref_wrapped, m.ref_to_type_wrapper,
            m.void_ptr_smell, naked[i],
            sites_json(&st.naked), sites_json(&st.raw_ptr),
            sites_json(&st.void_ptr), sites_json(&st.field_proj),
        )
    }).collect();
    format!("{{\"crate\":\"{krate}\",\"seeds\":[{}]}}", entries.join(","))
}

struct MetricsCallbacks;

impl Callbacks for MetricsCallbacks {
    fn after_analysis(&mut self, _compiler: &rustc_interface::interface::Compiler, tcx: TyCtxt<'_>) -> Compilation {
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
        // Seed mode prints the per-seed JSON, then FALLS THROUGH to also emit the
        // tree-wide global block below — so `crustify audit` gets both the seed
        // surface and the global section + totals from a single compilation
        // (the dispatcher merges the two stdout lines per crate).
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
        let wrapped_c: HashSet<DefId> = with_wrappers(tcx, |w| w.values().copied().collect());

        let mut c = Counts::default();
        let mut sites = Sites::default();
        // Every body owner (fn, closure, const/static initializer, ...). Each is
        // a separate typeck context; intravisit does not descend into nested
        // bodies, so visiting every owner covers the whole crate exactly once.
        for owner in tcx.hir_body_owners() {
            let did = owner.to_def_id();
            let in_wrapper = in_wrapper_impl(tcx, did);
            let in_ffi = in_ffi_export(tcx, did);
            let in_impl = in_any_impl(tcx, did);
            {
                let typeck = tcx.typeck(owner);
                let body = tcx.hir_body_owned_by(owner);
                let mut v = BodyVisitor {
                    tcx, typeck, depth: 0, in_wrapper, in_ffi, in_impl,
                    wrapped_c: &wrapped_c, c: &mut c, sites: &mut sites,
                };
                v.visit_body(body);
            }
            // Signature analysis (fns only).
            if matches!(tcx.def_kind(did), DefKind::Fn | DefKind::AssocFn) {
                let sig = tcx.fn_sig(did).skip_binder().skip_binder();
                let seam = is_seam_fn(tcx, did);
                let extern_c = is_extern_c_fn(tcx, did);
                // `&mut <wrapper>` and `*c_void` anywhere in the signature.
                for t in sig.inputs().iter().copied().chain(std::iter::once(sig.output())) {
                    if is_ref_to_type_wrapper(tcx, t) {
                        c.ref_to_type_wrapper += 1;
                    }
                    if is_void_ptr(tcx, t) {
                        if seam || in_ffi || extern_c {
                            c.void_ptr_sanctioned += 1;
                        } else {
                            c.void_ptr_smell += 1;
                            sites.void_ptr.push(span_site(tcx, tcx.def_span(did)));
                        }
                    }
                }
                // Raw-pointer args/rets: count EVERY position, then name the
                // sanctioned subset. No position goes unreported.
                let sanctioned = seam || in_ffi || extern_c;
                // Resolution-based self-boundary: a raw ptr to the method's OWN
                // wrapper type (`*mut Self` in `free`/`dispose`/`dup`/…) is the
                // type's raw-form lifecycle seam, not a "use the wrapper" smell
                // (you can't pass `&Self` while destroying/duplicating it). Skip.
                let own_self = enclosing_impl_self(tcx, did);
                {
                    let mut tally = |p: Ty<'_>, is_ret: bool, c: &mut Counts| {
                        if is_ret { c.rp_rets += 1 } else { c.rp_args += 1 }
                        // A raw ptr to the method's OWN wrapper type (`*mut Self`
                        // in `free`/`dup`) is the type's raw-form lifecycle seam —
                        // you cannot pass `&Self` while destroying it. Sanctioned,
                        // and now COUNTED as such instead of dropped silently.
                        let is_own = own_self
                            .is_some_and(|s| p.ty_adt_def().map(|d| d.did()) == Some(s));
                        if sanctioned || is_own {
                            c.rp_seam += 1;
                            return;
                        }
                        // The actionable smell is a raw ptr to the *C type* when a
                        // wrapper exists (`*mut ffi::git_oid` → should be GitOid).
                        // A raw ptr to the *wrapper itself* (`*mut GitOid`) already
                        // uses the wrapper — kept raw deliberately (stored back-ptr
                        // / array boundary), not a smell. So count only the C case.
                        let w = matches!(p.kind(),
                            ty::TyKind::Adt(def, _) if wrapped_c.contains(&def.did()));
                        if w { c.rp_wrapped += 1 }
                        if in_wrapper { c.rp_in_wrapper += 1 }
                        if w {
                            sites.raw_ptr.push(span_site(tcx, tcx.def_span(did)));
                        }
                    };
                    for inp in sig.inputs() {
                        if let Some(p) = raw_pointee(*inp) { tally(p, false, &mut c); }
                    }
                    if let Some(p) = raw_pointee(sig.output()) { tally(p, true, &mut c); }
                }
            }
        }
        // Crate-wide physical LoC: count non-blank, non-`//`-comment source lines
        // across the local crate's own files. Imported/external-crate files carry
        // `src == None` (their text lives in `external_src`), so filtering on
        // `src.is_some()` isolates exactly the crate being compiled. Same filter
        // as `unsafe_block_code_lines`, so the two are apples-to-apples.
        {
            let sm = tcx.sess.source_map();
            for sf in sm.files().iter() {
                if let Some(src) = &sf.src {
                    c.code_lines += src
                        .lines()
                        .filter(|l| {
                            let t = l.trim();
                            !t.is_empty() && !t.starts_with("//")
                        })
                        .count() as u64;
                }
            }
        }
        println!(
            "{{\"crate\":\"{}\",\"unsafe_blocks\":{},\"unsafe_block_stmts\":{},\"unsafe_block_lines\":{},\"unsafe_block_code_lines\":{},\"unsafe_blocks_wrapper_impl\":{},\"wrapper_impl_macro\":{},\"wrapper_impl_handwritten\":{},\"unsafe_blocks_ffi_export\":{},\"rp_args\":{},\"rp_rets\":{},\"rp_seam\":{},\"rp_wrapped\":{},\"rp_in_wrapper\":{},\"ref_to_type_wrapper\":{},\"field_proj_wrapped\":{},\"field_proj_outside_impl\":{},\"field_ref_wrapped\":{},\"void_ptr_sanctioned\":{},\"void_ptr_smell\":{},\"raw_ptr_derefs\":{},\"raw_ptr_derefs_outside_impl\":{},\"total_stmts\":{},\"code_lines\":{},\"raw_ptr_sites\":{},\"void_ptr_sites\":{},\"field_proj_sites\":{},\"field_ref_sites\":{},\"raw_deref_sites\":{}}}",
            krate, c.unsafe_blocks, c.unsafe_block_stmts, c.unsafe_block_lines, c.unsafe_block_code_lines, c.unsafe_blocks_wrapper_impl, c.wrapper_impl_macro, c.wrapper_impl_handwritten, c.unsafe_blocks_ffi_export, c.rp_args, c.rp_rets, c.rp_seam, c.rp_wrapped, c.rp_in_wrapper, c.ref_to_type_wrapper, c.field_proj_wrapped, c.field_proj_outside_impl, c.field_ref_wrapped, c.void_ptr_sanctioned, c.void_ptr_smell, c.raw_ptr_derefs, c.raw_ptr_derefs_outside_impl, c.total_stmts, c.code_lines,
            sites_json(&sites.raw_ptr), sites_json(&sites.void_ptr), sites_json(&sites.field_proj), sites_json(&sites.field_ref), sites_json(&sites.raw_deref)
        );
        Compilation::Continue
    }
}

fn main() {
    let mut args: Vec<String> = std::env::args().collect();
    if !args.iter().any(|a| a == "--sysroot" || a.starts_with("--sysroot=")) {
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
