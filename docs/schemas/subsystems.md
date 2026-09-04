# subsystems.json schema

Field meaning for `<repo_root>/crustify/subsystems.json`, the orchestrator's
campaign decomposition. Layout example:
[`specs/subsystems.json`](../../specs/subsystems.json).

The orchestrator emits this repo-tier artifact after it has authored the
campaign-wide Wavefront config and populated its CodeQL inventory. It
describes the subsystem span selected by the user and every imported subsystem
in that span's producer closure.

`build_version` is the `version` of the `build.json` used to configure and
compile the analyzed tree. `oracle_config` records the campaign-wide
`wavefront-config.json` from which the decomposition was derived.

## link_units[*]

`link_units` is an ordered list. Each entry's `name` is its identifier; clients
may build a name index without losing the authored order.

| field | meaning |
|---|---|
| `name` | unique link-unit identifier, normally the linked artifact's filename stem |
| `kind` | `library` or `executable` |
| `linkage` | `shared`, `static`, or `system` for a library; `null` for an executable |
| `target` | linked output path, or `null` when the artifact is supplied by the system |
| `subsystems` | ordered list of the link unit's covered subsystems |

A system link unit may contain imported, header-derived subsystems whose
`implementation_files` list is empty and whose `loc` is zero. It may have an
empty `subsystems` list when no entity from that link unit enters the campaign
closure.

## link_units[*].subsystems[*]

Each subsystem's `name` identifies it within its containing link unit. The
globally addressable identity is therefore `(link_unit.name, subsystem.name)`.

| field | meaning |
|---|---|
| `name` | subsystem identifier, unique within the link unit |
| `scope` | translation intent: `targeted` for native Rust translation; `imported` for an oracle-discovered producer dependency or a selected subsystem deliberately retained behind a wrapped C boundary |
| `implementation_files` | repo-relative translation units homed in the subsystem |
| `loc` | physical nonblank, noncomment lines across `implementation_files` |
| `nr_types` | number of oracle types assigned to the subsystem |
| `nr_symbols` | number of oracle symbols assigned to the subsystem |
| `depends_on` | consumer-to-producer dependency records |

Use the oracle's file, type, symbol, and edge statistics when available instead
of recounting them independently. A subsystem is scope-homogeneous: targeted
and imported translation units do not share one subsystem.

The orchestrator must home every covered translation unit in exactly one
subsystem. This is an authoring instruction, not a separate validation gate.

## link_units[*].subsystems[*].depends_on[*]

| field | meaning |
|---|---|
| `link_unit` | destination `link_units[*].name` |
| `subsystem` | destination subsystem's `name` within that link unit |
| `nr_edges` | number of oracle dependency edges aggregated into this relation |

Every record is directed from a consumer to a producer. Imported producer
subsystems are emitted too, so every destination resolves through the two
lists.

The emitted subsystem graph is acyclic. When an initial grouping produces a
cycle, the orchestrator changes the grouping by rehoming translation units or
merging subsystems; it does not conceal dependency records. Within a cyclic
region, a subsystem with more incoming consumer edges has greater producer
weight and should preferentially remain the producer. `nr_edges` refines that
weight when choosing boundaries.

## Scope

The covered span follows the user's campaign selection, but scope also records
the orchestrator's native-Rust suitability decision:

- selected project-specific subsystems intended for native translation are
  `targeted`;
- selected generic facilities that should eventually be replaced by the Rust
  standard library or another suitable Rust implementation are `imported` and
  remain behind wrapped C boundaries during partial migration;
- oracle-discovered producer dependencies are also `imported`;
- a mixed subsystem is split so each emitted subsystem is scope-homogeneous.

Changing the target selection or native-Rust suitability decision requires
regenerating this artifact from the campaign-wide Wavefront config.
