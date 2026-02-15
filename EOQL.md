# EOQL Reference

EOQL (EO Query Language) is how you ask Choreo questions. Queries are operations — when you query, you perform a DES (designate your attention). Send EOQL queries by POSTing to the operations endpoint:

```bash
curl -X POST localhost:8420/my-instance/operations \
  -H "Content-Type: application/json" \
  -d '{
    "op": "DES",
    "target": {"query": "state(context.table=\"places\")"},
    "context": {"type": "query"}
  }'
```

---

## state() — Current projected state

Returns entities from the materialized projection.

### Basic usage

```
state(context.table="places")
```

Returns all alive entities in the places table.

### Filtering by field values

```
state(context.table="places", target.status="open")
state(context.table="people", target.role="musician")
```

The `target.` prefix is optional — bare keys are treated as target fields:

```
state(context.table="places", status="open")
```

Filters are AND'd. All conditions must match.

### Filtering by entity ID

```
state(target.id="pl0")
```

Returns entities with that ID across all tables.

### Time travel

```
state(context.table="places", at="2025-06-15T00:00:00Z")
```

Projects state as of the given timestamp. The engine finds the nearest snapshot checkpoint before the timestamp and replays forward.

### SEG boundary filtering

```
state(context.table="places", _seg.boundary="downtown-district")
```

Returns only entities that have been SEG'd with the specified boundary.

### Absence type filters

```
state(context.table="places", _has_sup=true)     # fields in superposition
state(context.table="places", _has_nul=true)      # explicitly destroyed fields
state(context.table="places", _include_dead=true)  # include NUL'd entities
state(context.table="places", _only_dead=true)     # only destroyed entities
```

### Operator history filters

```
state(context.table="people", _ops.DES=0)   # never designated
state(context.table="people", _ops.CON=0)   # no connections
```

### Frame provenance filters

```
state(context.table="places", frame.epistemic="given")
state(context.table="places", frame.source="court-records")
```

---

## stream() — Raw operations from the log

Returns operations themselves, not projected state.

```
stream(op=ALT)
stream(op=CON, limit=50)
stream(after="2026-01-01", before="2026-02-01")
```

- `op` filters by operator type.
- `limit` caps the result count (default 100).
- `after` and `before` filter by timestamp.

Operations are returned in reverse chronological order (most recent first).

---

## >> CON() — Graph traversal

Chain onto a state() query to traverse the CON graph outward from the result entities.

### Basic usage

```
state(target.id="pl0") >> CON(hops=2)
```

Finds entity pl0, then follows CON edges outward for 2 hops via breadth-first search. Returns the base entity plus everything reached.

### Filtering by stance

```
state(target.id="pl0") >> CON(hops=2, stance="essential")
```

Only traverses edges with the specified stance. "Show me everything essentially connected to this entity within 2 hops."

```
state(target.id="pl0") >> CON(hops=2, exclude="accidental")
```

Traverses all edges except those with the excluded stance. "Show me everything that isn't just incidentally connected."

The three stances — accidental, essential, generative — describe the nature of the connection, not its strength. See OPERATORS.md for the full dialectic.

### Backward-compatible coupling filter

```
state(target.id="pl0") >> CON(hops=2, min_coupling=0.5)
```

Filters edges by the legacy `coupling` float. Stance-based filtering is preferred.

### Response format

The base `state()` response is augmented with a `con_chain` object:

```json
{
  "type": "state",
  "table": "places",
  "count": 1,
  "entities": [
    {"id": "pl0", "name": "Listening Room"}
  ],
  "con_chain": {
    "hops": 2,
    "stance": "essential",
    "reached": [
      {"id": "pe0", "table": "people", "name": "Tomás", "role": "musician"},
      {"id": "ev0", "table": "events", "name": "Open Mic Night"}
    ],
    "edges": [
      {"source": "pl0", "target": "pe0", "stance": "essential", "data": {"type": "performs_at"}},
      {"source": "ev0", "target": "pl0", "stance": "generative", "data": {"type": "hosted_at"}}
    ],
    "reached_count": 2
  }
}
```

- `reached` contains entities discovered by traversal (not in the original result set).
- `edges` contains all CON edges encountered during traversal.
- Edges are traversed in both directions regardless of their declared source/target.
- Reached entities may come from any table — CON traversal crosses table boundaries.

---

## meta() — System self-reference

Query the system's own structure.

```
meta(types)       →  state(context.table="_types")
meta(rules)       →  state(context.table="_rules")
meta(fields)      →  state(context.table="_fields")
meta(emergent)    →  state(context.table="_emergent")
```

These are shorthand for querying reserved tables where the system stores its own type definitions, feedback rules, replay profiles, and algorithmically detected clusters.

---

## Replay Profiles (Read-Time Frames)

When a DES query includes a frame with a `replay` key, the frame modifies how results are returned:

```json
{
  "op": "DES",
  "target": {"query": "state(context.table=\"places\")"},
  "context": {"type": "query"},
  "frame": {
    "replay": {
      "conflict": "preserve",
      "boundary": "unified"
    }
  }
}
```

### Conflict stance

| Stance | Behavior |
|--------|----------|
| `preserve` | SUP fields returned as-is with all variants. Default. |
| `collapse` | First variant wins. Field shows a single value. `_collapsed` array lists which fields were collapsed. |
| `suspend` | Entities with SUP fields are marked `_suspended: true`. Projection blocked until human resolution. |

### Boundary stance

| Stance | Behavior |
|--------|----------|
| `respect` | SEG boundaries honored as filters. Default. |
| `unified` | All boundaries ignored. Everything visible. Investigative mode. |

### Named profiles

Common configurations stored as entities in `_rules`:

```
GET /my-instance/state/places?replay=investigative
```

Profiles are themselves entities — INS'd, ALT'd, NUL'd, auditable.

---

## Audit Trail

By default, DES queries are not appended to the operations log. To create an audit trail entry:

```json
{
  "op": "DES",
  "target": {"query": "state(context.table=\"places\")"},
  "context": {"type": "query"},
  "frame": {"audit": true}
}
```

The response includes `"audited": true` and `"audit_op_id"` pointing to the logged operation. Use this for intentional queries (reports, investigations) but not for UI interactions.

---

## Convenience Endpoints

These bypass EOQL syntax for common operations:

```
GET /{instance}/state/{table}                    — all alive entities
GET /{instance}/state/{table}?at=2025-06-15      — time travel
GET /{instance}/state/{table}/{entity_id}        — single entity lookup
GET /{instance}/biography/{entity_id}            — full operation history
GET /{instance}/gaps/{table}                     — operator-typed absence report
```

The `biography` endpoint returns every operation that ever touched an entity, plus its current projected state. The `gaps` endpoint groups entities by which operators have not been applied: never designated, never connected, has contradictions, never validated.

---

## Combining Queries

EOQL queries are composable through the CON chain operator. Start with any `state()` query, then traverse:

```
state(context.table="people", role="musician") >> CON(hops=2, exclude="accidental")
```

"Find all musicians, then show me everything within 2 hops that isn't just incidentally connected." This might return venues they perform at, events they're essential to, other people they collaborate with generatively — across tables, following the relationship graph.
