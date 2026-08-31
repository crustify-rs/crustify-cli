# subsystems.json schema

Field meaning for `<repo_root>/crustify/subsystems.json`, the orchestrator's
campaign decomposition. Layout example:
[`specs/subsystems.json`](../../specs/subsystems.json).

The orchestrator emits this repo-tier artifact after it has configured the
campaign-wide oracle target and populated that target's CodeQL inventory. It
describes the subsystem span selected by the user and every imported subsystem
in that span's producer closure.

`build_version` is the `version` of the `build.json` used to configure and
compile the analyzed tree. `oracle_target` names the campaign-wide oracle target
from which the decomposition was derived.

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
| `scope` | `targeted` when selected by the campaign, or `imported` when included as a producer dependency |
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

The targeted span follows the user's campaign selection:

- named target subsystems produce only those targeted subsystems plus their
  imported producer closure;
- a whole-target campaign produces every targeted subsystem plus their imported
  producer closure.

Changing the target selection requires regenerating this artifact from the
campaign-wide oracle target.
