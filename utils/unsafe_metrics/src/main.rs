//! PoC: a rustc driver (HIR + typeck) that counts three properties of a Rust
//! crate that the regex/`syn` approaches cannot do precisely:
//!
//!   * `unsafe_blocks`        - number of `unsafe { ... }` blocks
//!   * `unsafe_block_stmts`   - statements lexically inside unsafe blocks
//!   * `unsafe_block_lines`   - source lines spanned by unsafe blocks (outermost)
//!   * `raw_ptr_derefs`       - dereferences `*p` where `p: *const T | *mut T`
//!                              (decided by the *type* of the operand via typeck,
//!                              so `*Box`/`*&`/`Deref` impls are NOT counted)
//!
//! Run as a rustc-compatible front end: it compiles the given file/crate and
//! prints the metrics as JSON in `after_analysis`. See `run.sh`.
#![feature(rustc_private)]

extern crate rustc_driver;
extern crate rustc_hir;
extern crate rustc_interface;
extern crate rustc_middle;
extern crate rustc_span;

use rustc_driver::{Callbacks, Compilation};
use rustc_hir as hir;
use rustc_hir::def::DefKind;
use rustc_hir::intravisit::{self, Visitor};
use rustc_middle::ty::{self, Ty, TyCtxt, TypeckResults};
use rustc_span::def_id::DefId;
use rustc_span::hygiene::{ExpnKind, MacroKind};
use rustc_span::Span;
use std::collections::HashSet;

/// FFI-seam conversion routines (audit `_SEAM_FN_NAMES`): raw pointers in
/// these signatures are the expected boundary, not a smell.
const SEAM_FNS: &[&str] = &[
    "as_ptr", "as_mut_ptr", "as_c_ptr", "as_raw", "as_buf_ptr",
    "from_ptr", "from_ptr_mut", "from_raw",
    "to_ptr", "to_raw", "into_raw", "from_foreign", "into_foreign",
];

fn is_seam_fn(tcx: TyCtxt<'_>, did: DefId) -> bool {
    tcx.opt_item_name(did).is_some_and(|n| SEAM_FNS.contains(&n.as_str()))
}

/// True if `t` is `&mut W` where `W` is a `define_type!` wrapper. Catches both
/// the `&mut self` receiver (its type is `&mut Self` = `&mut W`) and explicit
/// `&mut W` params. A discipline smell: wrappers interior-mutate via `&self`.
fn is_mut_ref_wrapper(tcx: TyCtxt<'_>, t: Ty<'_>) -> bool {
    if let ty::TyKind::Ref(_, pointee, m) = t.kind() {
        if m.is_mut() {
            if let ty::TyKind::Adt(def, _) = pointee.kind() {
                return is_define_type_wrapper(tcx, def.did());
            }
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

/// True if the pointee `p` is a C type that has a `define_type!` wrapper
/// (i.e. a safe wrapper exists for it) — or is itself such a wrapper.
fn pointee_has_wrapper(tcx: TyCtxt<'_>, p: Ty<'_>, wrapped_c: &HashSet<DefId>) -> bool {
    match p.kind() {
        ty::TyKind::Adt(def, _) => {
            wrapped_c.contains(&def.did()) || is_define_type_wrapper(tcx, def.did())
        }
        _ => false,
    }
}

/// Is `did` a struct produced by `crustify::define_type!`? (The macro expands
/// to `pub struct $name(CType<$c_type>)`, so the struct's def-span carries the
/// `define_type` expansion context.)
fn is_define_type_wrapper(tcx: TyCtxt<'_>, did: DefId) -> bool {
    if !matches!(tcx.def_kind(did), DefKind::Struct) {
        return false;
    }
    let data = tcx.def_span(did).ctxt().outer_expn_data();
    // invoked either bare (`define_type!`) or path-qualified
    // (`crustify::define_type!`) -> match the last path segment.
    matches!(data.kind, ExpnKind::Macro(MacroKind::Bang, name)
        if name.as_str().rsplit("::").next() == Some("define_type"))
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

/// True if `did` is (transitively) inside a `mod ffi_export { .. }` — the
/// sanctioned C-ABI re-export region (DISCIPLINE sec 1.4).
fn in_ffi_export(tcx: TyCtxt<'_>, mut did: DefId) -> bool {
    while let Some(parent) = tcx.opt_parent(did) {
        if matches!(tcx.def_kind(parent), DefKind::Mod)
            && tcx.opt_item_name(parent).is_some_and(|n| n.as_str() == "ffi_export")
        {
            return true;
        }
        did = parent;
    }
    false
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
/// whose `T` is a `define_type!` wrapper.
fn in_wrapper_impl(tcx: TyCtxt<'_>, mut did: DefId) -> bool {
    while let Some(parent) = tcx.opt_parent(did) {
        match tcx.def_kind(parent) {
            DefKind::Impl { .. } => {
                return impl_self_def(tcx, parent)
                    .is_some_and(|s| is_define_type_wrapper(tcx, s));
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
    unsafe_blocks_wrapper_impl: u64, // unsafe blocks inside `impl <define_type! T>`
    wrapper_impl_macro: u64,         //   of which macro-generated (get/get_mut)
    wrapper_impl_handwritten: u64,   //   of which hand-written methods
    unsafe_blocks_ffi_export: u64,   // unsafe blocks inside `mod ffi_export`
    // signature raw pointers (args/rets), region-classified:
    rp_wrap_nonseam_args: u64, // in `impl <define_type! T>`, non-seam methods
    rp_wrap_nonseam_rets: u64,
    rp_wrap_nonseam_wrapped: u64, //   of those, pointee has a define_type! wrapper
    rp_outside_args: u64, // outside wrapper impls AND outside `mod ffi_export`
    rp_outside_rets: u64,
    rp_outside_wrapped: u64, //        of those, pointee has a define_type! wrapper
    mut_borrow_wrapper: u64, // `&mut W` (incl. `&mut self`) in signatures, W a wrapper
    // `(*p).field` where `p: *C` and `C` has a define_type! wrapper (bypasses the
    // accessor): total, and the subset outside any impl/trait (the smell).
    field_proj_wrapped: u64,
    field_proj_outside_impl: u64,
    // `*c_void` in signatures: sanctioned (seam / ffi_export) vs smell (elsewhere)
    void_ptr_sanctioned: u64,
    void_ptr_smell: u64,
    raw_ptr_derefs: u64,
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
    in_wrapper: bool, // this body is inside an `impl <define_type! T>`
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
            _ => {}
        }
        intravisit::walk_expr(self, e);
    }
}

const CRUSTIFY_MACROS: &[&str] = &[
    "define_type", "impl_ref_counted", "impl_freed", "impl_cvalued", "impl_cloned",
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
fn usage_json(tcx: TyCtxt<'_>, krate: rustc_span::Symbol) -> String {
    use std::collections::BTreeMap;
    let mut types: BTreeMap<String, u64> = BTreeMap::new();
    let mut trait_impls: BTreeMap<String, u64> = BTreeMap::new();
    let mut macros: BTreeMap<String, HashSet<rustc_span::ExpnId>> = BTreeMap::new();

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
    format!(
        "{{\"crate\":\"{krate}\",\"types\":{{{}}},\"trait_impls\":{{{}}},\"macros\":{{{}}}}}",
        obj(&types), obj(&trait_impls), macros_obj
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

/// Map a `define_type!` wrapper struct to the C type it wraps (`CType<C>`'s `C`).
fn wrapper_c(tcx: TyCtxt<'_>, w: DefId) -> Option<DefId> {
    if !matches!(tcx.def_kind(w), DefKind::Struct) {
        return None;
    }
    let field = tcx.adt_def(w).all_fields().next()?;
    if let ty::TyKind::Adt(_, args) = tcx.type_of(field.did).skip_binder().kind() {
        if let Some(c0) = args.types().next() {
            if let ty::TyKind::Adt(cdef, _) = c0.kind() {
                return Some(cdef.did());
            }
        }
    }
    None
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
            DefKind::Struct if is_define_type_wrapper(tcx, did) => SeedKind::Type,
            DefKind::Fn | DefKind::AssocFn => SeedKind::Func,
            _ => continue,
        };
        let rust_name = tcx.item_name(did).to_string();
        let (c_did, c_name) = match kind {
            SeedKind::Type => {
                let c = wrapper_c(tcx, did);
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

/// Collect spans of HIR type-references that resolve to `target` (a seed's
/// wrapped C type), for type-seed `naked_sites`. The naked COUNT stays
/// typeck-based (`count_ty_did`, alias-proof); these spans are the syntactic
/// occurrences the agent actually edits.
struct NakedTyVisitor<'a> {
    target: DefId,
    out: &'a mut Vec<Span>,
}
impl<'a, 'v> Visitor<'v> for NakedTyVisitor<'a> {
    fn visit_ty(&mut self, t: &'v hir::Ty<'v, hir::AmbigArg>) {
        if let hir::TyKind::Path(hir::QPath::Resolved(_, path)) = t.kind {
            if let hir::def::Res::Def(_, did) = path.res {
                if did == self.target {
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
    let mut wrapped_c: HashSet<DefId> = HashSet::new();
    for ld in tcx.hir_crate_items(()).definitions() {
        if let Some(c) = wrapper_c(tcx, ld.to_def_id()) {
            if is_define_type_wrapper(tcx, ld.to_def_id()) {
                wrapped_c.insert(c);
            }
        }
    }
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
                let span = tcx.def_span(did);
                for t in sig.inputs().iter().copied().chain(std::iter::once(sig.output())) {
                    if is_mut_ref_wrapper(tcx, t) {
                        metrics[i].mut_borrow_wrapper += 1;
                    }
                    if is_void_ptr(tcx, t) && !(seam || in_ffi) {
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
                        let mut v = NakedTyVisitor { target: c, out: &mut spans };
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
            "{{\"name\":\"{}\",\"kind\":\"{}\",\"region_owners\":{},\"unsafe_blocks\":{},\"unsafe_block_code_lines\":{},\"wrapper_macro\":{},\"wrapper_handwritten\":{},\"raw_ptr_derefs\":{},\"field_proj\":{},\"field_proj_outside_impl\":{},\"mut_borrow_wrapper\":{},\"void_ptr_smell\":{},\"naked\":{},\"naked_sites\":{},\"raw_ptr_sites\":{},\"void_ptr_sites\":{},\"field_proj_sites\":{}}}",
            s.name, kind, region_owners[i], m.unsafe_blocks, m.unsafe_block_code_lines,
            m.wrapper_impl_macro, m.wrapper_impl_handwritten, m.raw_ptr_derefs,
            m.field_proj_wrapped, m.field_proj_outside_impl, m.mut_borrow_wrapper,
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
                    if is_define_type_wrapper(tcx, did) {
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
        // Set of C types that have a `define_type!` wrapper (a safe wrapper
        // exists). Each wrapper `struct W(CType<C>)` contributes its `C`.
        let mut wrapped_c: HashSet<DefId> = HashSet::new();
        for ld in tcx.hir_crate_items(()).definitions() {
            let did = ld.to_def_id();
            if is_define_type_wrapper(tcx, did) {
                if let Some(field) = tcx.adt_def(did).all_fields().next() {
                    if let ty::TyKind::Adt(_, args) = tcx.type_of(field.did).skip_binder().kind() {
                        if let Some(c0) = args.types().next() {
                            if let ty::TyKind::Adt(cdef, _) = c0.kind() {
                                wrapped_c.insert(cdef.did());
                            }
                        }
                    }
                }
            }
        }

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
                // `&mut <wrapper>` and `*c_void` anywhere in the signature.
                for t in sig.inputs().iter().copied().chain(std::iter::once(sig.output())) {
                    if is_mut_ref_wrapper(tcx, t) {
                        c.mut_borrow_wrapper += 1;
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
                // Raw-pointer args/rets, region-classified.
                let cat_wrap = in_wrapper && !seam;
                let cat_out = !in_wrapper && !in_ffi;
                // Resolution-based self-boundary: a raw ptr to the method's OWN
                // wrapper type (`*mut Self` in `free`/`dispose`/`dup`/…) is the
                // type's raw-form lifecycle seam, not a "use the wrapper" smell
                // (you can't pass `&Self` while destroying/duplicating it). Skip.
                let own_self = enclosing_impl_self(tcx, did);
                if cat_wrap || cat_out {
                    let mut tally = |p: Ty<'_>, is_ret: bool, c: &mut Counts| {
                        if let Some(s) = own_self {
                            if p.ty_adt_def().map(|d| d.did()) == Some(s) { return; }
                        }
                        // The actionable smell is a raw ptr to the *C type* when a
                        // wrapper exists (`*mut ffi::git_oid` → should be GitOid).
                        // A raw ptr to the *wrapper itself* (`*mut GitOid`) already
                        // uses the wrapper — kept raw deliberately (stored back-ptr
                        // / array boundary), not a smell. So count only the C case.
                        let w = matches!(p.kind(),
                            ty::TyKind::Adt(def, _) if wrapped_c.contains(&def.did()));
                        if cat_wrap {
                            if is_ret { c.rp_wrap_nonseam_rets += 1 } else { c.rp_wrap_nonseam_args += 1 }
                            if w { c.rp_wrap_nonseam_wrapped += 1 }
                        } else {
                            if is_ret { c.rp_outside_rets += 1 } else { c.rp_outside_args += 1 }
                            if w { c.rp_outside_wrapped += 1 }
                        }
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
            "{{\"crate\":\"{}\",\"unsafe_blocks\":{},\"unsafe_block_stmts\":{},\"unsafe_block_lines\":{},\"unsafe_block_code_lines\":{},\"unsafe_blocks_wrapper_impl\":{},\"wrapper_impl_macro\":{},\"wrapper_impl_handwritten\":{},\"unsafe_blocks_ffi_export\":{},\"rp_wrap_nonseam_args\":{},\"rp_wrap_nonseam_rets\":{},\"rp_wrap_nonseam_wrapped\":{},\"rp_outside_args\":{},\"rp_outside_rets\":{},\"rp_outside_wrapped\":{},\"mut_borrow_wrapper\":{},\"field_proj_wrapped\":{},\"field_proj_outside_impl\":{},\"void_ptr_sanctioned\":{},\"void_ptr_smell\":{},\"raw_ptr_derefs\":{},\"total_stmts\":{},\"code_lines\":{},\"raw_ptr_sites\":{},\"void_ptr_sites\":{},\"field_proj_sites\":{}}}",
            krate, c.unsafe_blocks, c.unsafe_block_stmts, c.unsafe_block_lines, c.unsafe_block_code_lines, c.unsafe_blocks_wrapper_impl, c.wrapper_impl_macro, c.wrapper_impl_handwritten, c.unsafe_blocks_ffi_export, c.rp_wrap_nonseam_args, c.rp_wrap_nonseam_rets, c.rp_wrap_nonseam_wrapped, c.rp_outside_args, c.rp_outside_rets, c.rp_outside_wrapped, c.mut_borrow_wrapper, c.field_proj_wrapped, c.field_proj_outside_impl, c.void_ptr_sanctioned, c.void_ptr_smell, c.raw_ptr_derefs, c.total_stmts, c.code_lines,
            sites_json(&sites.raw_ptr), sites_json(&sites.void_ptr), sites_json(&sites.field_proj)
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
