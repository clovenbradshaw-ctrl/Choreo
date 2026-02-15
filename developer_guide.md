# Choreo Developer Guide

Choreo is an EO-native event store. Everything is an operation in an append-only log. Projected state is derived by replaying operations, not by mutating tables. There is one way in (POST an operation) and one way out (SSE stream). This document covers everything you need to build an application that interfaces with Choreo.

---

## Core Concepts

### The operations log

There is one table. Every event that has ever occurred lives here.

```
id      INTEGER     auto-incrementing
ts      TIMESTAMP   server-assigned (ISO 8601, UTC)
op      TEXT        one of nine operators
target  JSON        what is being operated on
context JSON        where/how it's happening
frame   JSON        interpretive lens (optional)
```

The log is append-only. Nothing is ever mutated or deleted from it. Projected state (the "current view" of entities) is derived by replaying operations. This projection is materialized for performance but is conceptually disposable — it can always be rebuilt from the log.

### The nine operators

Every operation uses exactly one of these nine operators, organized in three triads:

| Triad | Op | Meaning | What it does to projection |
|-------|----|---------|---------------------------|
| **Identity** | `NUL` | Destruction / absence | Marks entity as dead. Kills associated CON edges. Can also NUL individual fields. |
| | `DES` | Designation / naming | Stores designations in a `_des` namespace on the entity. Also used for queries. |
| | `INS` | Instantiation | Creates an entity or merges fields into an existing one. |
| **Structure** | `SEG` | Segmentation / filtering | Records boundary metadata. Mainly used at query time. |
| | `CON` | Connection / joining | Creates an edge between two entities with a coupling strength (0.0–1.0). |
| | `SYN` | Synthesis / merging | Merges entity B into entity A. B is killed, its edges are reassigned to A. |
| **Time** | `ALT` | Alternation / transition | Updates specific fields on an existing entity. |
| | `SUP` | Superposition / layering | Stores multiple simultaneous values for a field. |
| | `REC` | Reconfiguration | Snapshot ingest, feedback rules, emergence detection. The system operating on its own output. |

### Instances

Choreo supports multiple isolated databases called instances. Each instance has its own operations log, projected state, and CON graph. An instance is a single SQLite file.

### CON stance & coupling

Every CON edge carries a **dialectical stance** that describes the nature of the connection:

- **Accidental**: The connection is contingent. "Person frequents venue." Remove it and both entities remain fully coherent.
- **Essential**: The connection is necessary. "Expenditure funded by grant." Remove it and one or both entities lose their identity.
- **Generative**: The connection produces something neither entity has alone. "Co-investigators on a story." It's productive — it generates a capacity that only exists because of the relation.

Stance-based filtering is the primary query mechanism: `>> CON(hops=2, stance="essential")` or `>> CON(hops=2, exclude="accidental")`.

Every CON edge also retains a `coupling` float from 0.0 to 1.0 for backward compatibility. When a CON operation includes an explicit `stance`, it's stored directly. When stance is absent, it's inferred from coupling (≥0.7 → essential, ≥0.55 → generative, else accidental). The `min_coupling` filter remains available for hybrid queries.

---

## API Reference

Base URL: `http://localhost:8420` (default, configurable via `CHOREO_PORT` env var or `--port` flag).

All request and response bodies are JSON. All endpoints support CORS.

### Instance Management

#### List instances

```
GET /instances
```

Response:
```json
{
  "instances": [
    {
      "slug": "my-project",
      "operations": 247,
      "entities": 34,
      "tables": ["places", "people", "events"],
      "db_size": 102400
    }
  ]
}
```

#### Create instance

```
POST /instances
Content-Type: application/json

{"slug": "my-project"}
```

Slug must be lowercase alphanumeric with hyphens/underscores. Returns the instance metadata.

#### Delete instance

```
DELETE /instances/{slug}
```

Permanently deletes the SQLite database file. This is irreversible.

#### Seed instance (batch import)

```
POST /instances/{slug}/seed
Content-Type: application/json

{
  "operations": [
    {"op": "INS", "target": {"id": "p1", "name": "Alice"}, "context": {"table": "people"}},
    {"op": "INS", "target": {"id": "p2", "name": "Bob"}, "context": {"table": "people"}},
    {"op": "CON", "target": {"source": "p1", "target": "p2", "coupling": 0.6}, "context": {"table": "people"}}
  ]
}
```

Appends all operations in order. Each operation is projected synchronously. Returns count of operations inserted.

#### Rebuild projections

```
POST /instances/{slug}/rebuild
```

Drops all projection data and replays the entire operations log. Useful if projections are corrupted or the projection logic has been updated.

### Operations (The One Way In)

```
POST /{instance}/operations
Content-Type: application/json
```

This is the single point of entry. Every mutation, query, and ingest comes through here.

#### INS — Create an entity

```json
{
  "op": "INS",
  "target": {
    "id": "pl0",
    "name": "Listening Room",
    "type": "venue",
    "status": "open",
    "capacity": 200
  },
  "context": {
    "table": "places",
    "source": "manual-entry"
  }
}
```

- `target.id` is **required**. You assign it. It must be unique within its `context.table`.
- All other fields in `target` become the entity's projected data.
- If an entity with that ID already exists in the table, INS **merges** new fields into the existing data (does not replace).
- `context.table` determines which table the entity lives in. Tables are created implicitly — you don't declare them.

Response:
```json
{"op_id": 1, "ts": "2026-02-15T16:22:01.000Z", "op": "INS"}
```

#### ALT — Update fields

```json
{
  "op": "ALT",
  "target": {
    "id": "pl0",
    "status": "closed",
    "capacity": 180
  },
  "context": {"table": "places"},
  "frame": {"reason": "Renovation underway"}
}
```

- Only the fields present in `target` are updated. Other fields are untouched.
- If the entity doesn't exist, ALT is a no-op (no error, just nothing happens).
- `frame` is optional metadata about *why* this change happened. It's stored in the log. If frame contains `epistemic`, `source`, or `authority` keys, per-field provenance is recorded on the projected entity (see Frame Provenance).

#### NUL — Destroy an entity or field

Entity-level destruction:
```json
{
  "op": "NUL",
  "target": {"id": "pl0"},
  "context": {"table": "places"}
}
```

Field-level destruction:
```json
{
  "op": "NUL",
  "target": {"entity_id": "pl0", "field": "capacity"},
  "context": {"table": "places"}
}
```

- Entity-level NUL marks the entity as dead (`alive=0`) and kills all its CON edges.
- Field-level NUL stores a `{"_nul": true}` marker on the field. This is distinct from the field being absent (which is `¬INS` — never observed) or being null/empty.
- NUL is the **only** operator that truly destroys. All other absence types are operator-typed (see Absence Types below).

#### CON — Connect two entities

```json
{
  "op": "CON",
  "target": {
    "source": "pl0",
    "target": "pe0",
    "coupling": 0.7,
    "stance": "essential",
    "type": "works_at"
  },
  "context": {"table": "places"}
}
```

- `source` and `target` are entity IDs. They can be in different tables.
- `stance` is `"accidental"`, `"essential"`, or `"generative"`. If omitted, inferred from coupling.
- `coupling` is a float from 0.0 to 1.0. Default is 0.5.
- Any additional fields in `target` (like `type`) are stored as edge metadata.
- CON edges are directional but traversal queries follow them in both directions.
- Alternate field names `from`/`to` are also accepted instead of `source`/`target`.

#### SYN — Merge two entities

```json
{
  "op": "SYN",
  "target": {
    "merge_into": "pl0",
    "merge_from": "pl7"
  },
  "context": {"table": "places"}
}
```

- Merges `merge_from` into `merge_into`. Data from `merge_into` wins on field conflicts.
- `merge_from` is marked dead.
- All CON edges pointing to `merge_from` are reassigned to `merge_into`.
- A `_syn_from` array on the surviving entity tracks merge history.
- Alternate field names: `primary`/`secondary`.

#### SUP — Store multiple simultaneous values

```json
{
  "op": "SUP",
  "target": {
    "id": "pl0",
    "field": "capacity",
    "variants": [
      {"source": "permit", "value": 200},
      {"source": "website", "value": 180},
      {"source": "fire_marshal", "value": 195}
    ]
  },
  "context": {"table": "places"}
}
```

- The field's value becomes `{"_sup": [...variants], "op_id": N}`.
- When filtering on a SUP'd field, the query matches against any variant's value.
- SUP is for genuine ambiguity or multi-source disagreement — not for versioning (that's what the log is for).

#### DES — Designate / name / query

DES has two modes. As a designation on an entity:
```json
{
  "op": "DES",
  "target": {
    "id": "pe0",
    "title": "Community Lead",
    "appointed_by": "neighborhood_council"
  },
  "context": {"table": "people"}
}
```

This stores the fields in a `_des` namespace on the entity's projected data.

As a query (more common):
```json
{
  "op": "DES",
  "target": {
    "query": "state(context.table=\"places\")"
  },
  "context": {"type": "query"}
}
```

**Audit flag:** DES queries are not logged by default. To create an audit trail entry, include `"audit": true` in the frame:

```json
{
  "op": "DES",
  "target": {"query": "state(context.table=\"places\")"},
  "context": {"type": "query"},
  "frame": {"audit": true}
}
```

The response will include `"audited": true` and `"audit_op_id"` pointing to the logged operation. Use this for intentional queries (reports, investigations) but not for UI interactions (scrubber drags, live filtering).

**Unframed DES warning:** When a DES operation (non-query) has an empty frame, the response includes `"_warning": "unframed_designation"`. This makes frame-hiding visible without blocking the operation.

See the EOQL section below for query syntax.

#### REC — Reconfigure / recursive feedback

REC means recursion — the moment a derived structure enters the system's feedback loop. REC has three context types: snapshot ingest, feedback rules, and emergence detection.

##### REC: snapshot_ingest

The most common REC use — accepting raw state from an external source and decomposing it into granular operations:

```json
{
  "op": "REC",
  "target": {
    "rows": [
      {"name": "Listening Room", "hours": "6p-12a", "status": "open"},
      {"name": "Five Points Pizza", "hours": "11a-10p", "status": "open"},
      {"name": "Brand New Place", "hours": "9a-5p", "status": "open"}
    ]
  },
  "context": {
    "type": "snapshot_ingest",
    "table": "places"
  },
  "frame": {
    "match_on": "name",
    "absence_means": "unchanged",
    "null_fields_mean": "unchanged",
    "ignore_fields": ["last_scraped"]
  }
}
```

**Frame parameters:**

| Parameter | Values | Default | Meaning |
|-----------|--------|---------|---------|
| `match_on` | any field name | `"id"` | Which field to use as the entity key for diffing |
| `absence_means` | `"unchanged"`, `"deleted"`, `"investigate"` | `"unchanged"` | What to do when an existing entity is missing from the snapshot |
| `null_fields_mean` | `"unchanged"`, `"cleared"`, `"unknown"` | `"unchanged"` | What to do when a field in the incoming row is null |
| `ignore_fields` | array of field names | `[]` | Fields to skip during diffing |

**What the runtime generates:**

| Situation | Generated op |
|-----------|-------------|
| New entity in snapshot | `INS` |
| Existing entity with changed fields | `ALT` |
| Missing entity + `absence_means: "deleted"` | `NUL` |
| Missing entity + `absence_means: "investigate"` | `DES` with `flag: "absent_from_snapshot"` |
| Null field + `null_fields_mean: "cleared"` | `NUL` (field-level) |
| Null field + `null_fields_mean: "unknown"` | `SUP` with both values |

Response includes the generated operations:
```json
{
  "op_id": 47,
  "op": "REC",
  "generated_count": 3,
  "generated": [
    {"id": 48, "op": "ALT", "target": {"id": "Listening Room", "hours": "6p-12a"}, ...},
    {"id": 49, "op": "INS", "target": {"id": "Brand New Place", ...}, ...},
    ...
  ]
}
```

Each generated operation has `"generated_by": 47` in its context, creating a traceable chain back to the snapshot REC.

#### SEG — Segment / set boundary

```json
{
  "op": "SEG",
  "target": {"id": "pl0"},
  "context": {"table": "places", "boundary": "downtown-district"}
}
```

Records a boundary tag on the entity. Queryable via `_seg.boundary` filter (see EOQL section).

##### REC: feedback_rule

A feedback rule watches for operations matching a pattern and generates new operations in response. The system operating on its own output.

```json
{
  "op": "REC",
  "target": {
    "id": "rule-flag-absent",
    "trigger": {
      "op": "DES",
      "match": {"target.flag": "absent_from_snapshot"}
    },
    "action": {
      "op": "ALT",
      "target_template": {"id": "{trigger.target.id}", "status": "needs_investigation"},
      "context": {"table": "{trigger.context.table}"}
    }
  },
  "context": {"type": "feedback_rule"},
  "frame": {"authority": "michael"}
}
```

This says: when a DES operation appears whose target has `flag: "absent_from_snapshot"`, generate an ALT setting `status: "needs_investigation"` on that entity.

- Rules are stored as entities in the `_rules` table via `_projected`
- Template values like `{trigger.target.id}` are interpolated from the triggering operation
- **Safeguard:** Rules cannot trigger other rules in the same evaluation pass (prevents infinite loops). One pass per incoming operation.
- Generated operations carry `generated_by_rule` in their context for traceability

##### REC: emergence_scan

Scans the CON graph for unnamed clusters — structure that exists but nobody has named yet.

```json
{
  "op": "REC",
  "target": {
    "algorithm": "connected_components",
    "min_cluster_size": 3,
    "min_internal_coupling": 0.5
  },
  "context": {"type": "emergence_scan"},
  "frame": {"authority": "system", "confidence": "algorithmic"}
}
```

The runtime performs BFS/connected-components on `_con_edges`, finds clusters meeting the size/coupling threshold, and generates DES operations in the `_emergent` table for any clusters not already designated. Each cluster gets a deterministic ID based on its member set.

### SSE Stream (The One Way Out)

```
GET /{instance}/stream
GET /{instance}/stream?last_id=47
```

Server-Sent Events endpoint. Every operation appended to the log is emitted in real time.

Event format:
```
id: 48
event: op
data: {"id":48,"ts":"2026-02-15T16:22:01.000Z","op":"ALT","target":{"id":"pl0","status":"closed"},"context":{"table":"places"},"frame":{"reason":"Renovation"}}
```

- Events have type `op`. Listen with `sse.addEventListener("op", ...)`.
- Pass `last_id` to resume from a specific point. All operations after that ID are sent.
- The stream sends `: heartbeat\n\n` comments every 200ms when idle to keep the connection alive.
- The stream never closes. It runs until the client disconnects.

JavaScript example:
```javascript
const sse = new EventSource("http://localhost:8420/my-instance/stream");

sse.addEventListener("op", (event) => {
  const op = JSON.parse(event.data);
  console.log(op.op, op.target, op.context);

  // Client-side filtering (this is your SEG)
  if (op.op === "ALT" && op.context.table === "places") {
    updateUI(op);
  }
});
```

Python example:
```python
import requests, json

with requests.get("http://localhost:8420/my-instance/stream", stream=True) as r:
    for line in r.iter_lines():
        if line and line.startswith(b"data: "):
            op = json.loads(line[6:])
            print(f"{op['op']} {op['target']}")
```

### Convenience State Endpoints

These are syntactic sugar over EOQL. They don't go through the log.

#### Get all entities in a table

```
GET /{instance}/state/{table}
GET /{instance}/state/{table}?at=2025-06-15T00:00:00Z
```

Response:
```json
{
  "type": "state",
  "table": "places",
  "count": 7,
  "entities": [
    {"id": "pl0", "name": "Listening Room", "status": "open", "capacity": 200},
    {"id": "pl1", "name": "Five Points Pizza", "status": "open"}
  ]
}
```

The `at` parameter enables time travel — the projection is replayed from the nearest snapshot to the specified timestamp.

#### Get a single entity

```
GET /{instance}/state/{table}/{entity_id}
```

Response:
```json
{"id": "pl0", "table": "places", "alive": true, "name": "Listening Room", "status": "open"}
```

### Outbound Webhooks

Register URLs to receive a POST for every operation (or filtered subset).

#### Register

```
POST /{instance}/webhooks
Content-Type: application/json

{
  "url": "https://n8n.example.com/webhook/choreo-ingest",
  "filter": ["ALT", "INS", "NUL"],
  "active": true
}
```

- `filter` is optional. If omitted, all operations are sent.
- `active` defaults to `true`.
- Webhooks are fire-and-forget (background thread pool, 10s timeout).
- Webhook config is stored in `instances/webhooks.json`.

#### List

```
GET /{instance}/webhooks
```

#### Remove

```
DELETE /{instance}/webhooks
Content-Type: application/json

{"url": "https://n8n.example.com/webhook/choreo-ingest"}
```

#### Webhook POST body

Every matching operation is sent as:
```
POST {your-url}
Content-Type: application/json
X-Choreo-Instance: my-instance

{
  "op_id": 48,
  "ts": "2026-02-15T16:22:01.000Z",
  "op": "ALT",
  "target": {"id": "pl0", "status": "closed"},
  "context": {"table": "places"},
  "frame": {"reason": "Renovation"}
}
```

---

## EOQL (EO Query Language)

Queries are operations. When you query, you perform a DES (designate your attention). The runtime performs a SEG (filters the stream). The response is an INS (projected result). Every query is itself an event in the log.

Send EOQL queries by POSTing to the operations endpoint:

```json
{
  "op": "DES",
  "target": {"query": "state(context.table=\"places\")"},
  "context": {"type": "query"}
}
```

### state() — Project current state

```
state(context.table = "places")
state(context.table = "places", target.status = "open")
state(context.table = "places", at = "2025-06-15")
state(target.id = "pl0")
```

- Returns projected entities matching the filters.
- `context.table` selects the table. `target.*` filters on entity field values.
- `at` enables time travel — replays from nearest snapshot to the timestamp.
- Bare keys (without `target.` prefix) are treated as target fields.

### stream() — Return raw operations

```
stream(op = ALT, context.table = "places")
stream(op = CON, limit = 50)
stream(op = DES, context.type = "query")
stream(after = "2026-01-01", before = "2026-02-01")
```

- Returns the operations themselves, not projected state.
- `op` filters by operator type.
- `limit` defaults to 100.
- `after` and `before` filter by timestamp.

### meta() — System table shorthand

```
meta(types)        → state(context.table="_types")
meta(rules)        → state(context.table="_rules")
meta(fields)       → state(context.table="_fields")
meta(emergent)     → state(context.table="_emergent")
```

Syntactic sugar for querying the system's own structure.

### >> CON() — Graph traversal

```
state(target.id = "pl0") >> CON(hops = 2)
state(target.id = "pl0") >> CON(hops = 2, stance = "essential")
state(target.id = "pl0") >> CON(hops = 2, exclude = "accidental")
state(target.id = "pl0") >> CON(hops = 1, min_coupling = 0.5)
```

- Follows CON edges outward from the result entities via BFS.
- `hops` controls traversal depth (default 1).
- `stance` filters to only follow edges with the specified stance.
- `exclude` excludes edges with the specified stance.
- `min_coupling` filters edges below the threshold (default 0).
- Returns the base result plus a `con_chain` object:

```json
{
  "type": "state",
  "table": "places",
  "count": 1,
  "entities": [{"id": "pl0", "name": "Listening Room", ...}],
  "con_chain": {
    "hops": 2,
    "stance": "essential",
    "reached": [
      {"id": "pe0", "table": "people", "name": "Tomás", "role": "musician"},
      {"id": "ev0", "table": "events", "name": "Open Mic Night"}
    ],
    "edges": [
      {"source": "pl0", "target": "pe0", "stance": "essential", "coupling": 0.7, "data": {"type": "works_at"}},
      {"source": "ev0", "target": "pl0", "stance": "essential", "coupling": 0.8, "data": {"type": "hosted_at"}}
    ],
    "reached_count": 2
  }
}
```

---

## Projected State Format

### Normal fields

Entity fields are stored as JSON key-value pairs:
```json
{"id": "pl0", "name": "Listening Room", "status": "open", "capacity": 200}
```

### SUP'd fields (superposition)

When a field has multiple simultaneous values:
```json
{
  "id": "pl0",
  "capacity": {
    "_sup": [
      {"source": "permit", "value": 200},
      {"source": "website", "value": 180}
    ],
    "op_id": 23
  }
}
```

Your application should handle `_sup` fields by choosing a resolution strategy: take the first variant, prefer a specific source, show all variants, etc.

### NUL'd fields

When a field has been explicitly destroyed:
```json
{
  "id": "pl0",
  "old_field": {"_nul": true, "op_id": 45}
}
```

This is **not** the same as the field being absent. Absent means never observed. NUL'd means observed and then explicitly destroyed.

### DES metadata

Designations are stored in a `_des` namespace:
```json
{
  "id": "pe0",
  "name": "Tomás",
  "_des": {
    "title": "Community Lead",
    "appointed_by": "neighborhood_council"
  }
}
```

### SYN history

Entities that are the result of a merge carry merge history:
```json
{
  "id": "pl0",
  "name": "Listening Room",
  "_syn_from": ["pl7", "pl12"]
}
```

### SEG boundaries

Segmentation metadata:
```json
{
  "id": "pl0",
  "_seg": [
    {"boundary": "downtown-district", "op_id": 33}
  ]
}
```

Queryable via `_seg.boundary` filter in EOQL.

### Operator history

Tracks which operators have been applied and how many times:
```json
{
  "id": "pl0",
  "_ops": {"INS": 1, "ALT": 2, "CON": 3}
}
```

### Frame provenance

When operations carry epistemic frame data, per-field provenance is recorded:
```json
{
  "id": "pl0",
  "_provenance": {
    "capacity": {"op_id": 42, "epistemic": "given", "source": "manual-entry", "authority": "michael"}
  }
}
```

---

## Operator-Typed Absence

Every operator implies its own form of absence. This is philosophically important and practically useful — your application can distinguish *why* something is missing.

| Absence type | Negated operator | Meaning | Example |
|-------------|-----------------|---------|---------|
| Unknown | ¬INS | Never observed | No record of this entity exists |
| Undesignated | ¬DES | No frame applied | Entity exists but hasn't been named or categorized |
| Inapplicable | ¬SEG | Outside boundary | Entity excluded by a filter or scope |
| Unconstituted | ¬CON | No relation | No edges connect to this entity |
| Unfused | ¬SYN | Not merged | Components haven't been combined |
| Pending | ¬ALT | Between states | Entity exists but hasn't transitioned yet |
| Withheld | ¬SUP | Ambiguity held | Multiple possibilities are being preserved |
| Unvalidated | ¬REC | Not self-sustaining | Schema hasn't been applied |
| **Destroyed** | **NUL** | **Actually gone** | **Entity explicitly removed** |

Only NUL represents true destruction. All other absence types are inferred from which operators haven't been applied.

---

## Design Patterns

### Pattern: Scraper → Snapshot Ingest

Your scraper collects raw state. Choreo diffs it.

```python
import requests, json

scraped_rows = [
    {"name": "Listening Room", "hours": "6p-12a", "status": "open"},
    {"name": "New Coffee Shop", "hours": "7a-4p", "status": "open"},
]

response = requests.post("http://localhost:8420/my-instance/operations", json={
    "op": "REC",
    "target": {"rows": scraped_rows},
    "context": {"type": "snapshot_ingest", "table": "places"},
    "frame": {
        "match_on": "name",
        "absence_means": "unchanged",
        "null_fields_mean": "unchanged"
    }
})

result = response.json()
print(f"Generated {result['generated_count']} operations from snapshot")
for op in result['generated']:
    print(f"  {op['op']} {op['target']}")
```

The advantage: your scraper doesn't need to track what changed. It just says "here's what I see now" and Choreo figures out the diff. The interpretive assumptions (`absence_means`, `null_fields_mean`) are data in the log, not code in your scraper.

### Pattern: Real-Time Dashboard

Connect to the SSE stream and update your UI on every operation.

```javascript
const INSTANCE = "my-instance";
const BASE = "http://localhost:8420";

// Load initial state
async function loadState(table) {
  const res = await fetch(`${BASE}/${INSTANCE}/state/${table}`);
  return (await res.json()).entities;
}

// Connect to live stream
function connectStream(onOp) {
  const sse = new EventSource(`${BASE}/${INSTANCE}/stream`);
  sse.addEventListener("op", (e) => onOp(JSON.parse(e.data)));
  return sse;
}

// Usage
let places = await loadState("places");
connectStream((op) => {
  if (op.context.table === "places") {
    if (op.op === "INS") {
      places.push({id: op.target.id, ...op.target});
    } else if (op.op === "ALT") {
      const entity = places.find(p => p.id === op.target.id);
      if (entity) Object.assign(entity, op.target);
    } else if (op.op === "NUL") {
      places = places.filter(p => p.id !== op.target.id);
    }
    renderPlaces(places);
  }
});
```

### Pattern: n8n Workflow Integration

**Choreo → n8n** (event-driven): Register an outbound webhook. In n8n, create a Webhook trigger node and use the production URL.

```bash
curl -X POST http://localhost:8420/my-instance/webhooks \
  -H "Content-Type: application/json" \
  -d '{"url": "https://n8n.example.com/webhook/abc123", "filter": ["ALT", "NUL"]}'
```

**n8n → Choreo** (action): Use an HTTP Request node in n8n to POST operations.

```json
{
  "method": "POST",
  "url": "http://localhost:8420/my-instance/operations",
  "body": {
    "op": "INS",
    "target": {
      "id": "={{ $json.id }}",
      "name": "={{ $json.name }}"
    },
    "context": {
      "table": "scraped-data",
      "source": "n8n-workflow"
    }
  }
}
```

### Pattern: Time Travel Comparison

Compare state at two points in time:

```python
import requests

def state_at(instance, table, timestamp):
    r = requests.get(f"http://localhost:8420/{instance}/state/{table}",
                     params={"at": timestamp})
    return {e["id"]: e for e in r.json()["entities"]}

before = state_at("my-instance", "places", "2026-01-01T00:00:00Z")
after  = state_at("my-instance", "places", "2026-02-01T00:00:00Z")

for eid in after:
    if eid not in before:
        print(f"NEW: {after[eid]['name']}")
    elif before[eid] != after[eid]:
        for key in after[eid]:
            if before[eid].get(key) != after[eid][key]:
                print(f"CHANGED: {after[eid]['name']}.{key}: {before[eid].get(key)} → {after[eid][key]}")

for eid in before:
    if eid not in after:
        print(f"GONE: {before[eid]['name']}")
```

### Pattern: Graph Exploration

Find everything connected to an entity within N hops:

```python
import requests

r = requests.post("http://localhost:8420/my-instance/operations", json={
    "op": "DES",
    "target": {"query": 'state(target.id = "pl0") >> CON(hops = 2, min_coupling = 0.3)'},
    "context": {"type": "query"}
})

result = r.json()
for entity in result["con_chain"]["reached"]:
    print(f"  {entity['table']}: {entity.get('name', entity['id'])}")

for edge in result["con_chain"]["edges"]:
    coupling_type = "constitutive" if edge["coupling"] >= 0.65 else "associative"
    print(f"  {edge['source']} --({coupling_type} {edge['coupling']})-- {edge['target']}")
```

---

## Entity ID Conventions

Choreo doesn't enforce any ID format, but these conventions work well:

- **Short prefixed IDs**: `pl0`, `pe1`, `ev2` — good for demo/test data
- **Slug IDs**: `listening-room`, `five-points-pizza` — good when `match_on` uses name
- **UUID**: `550e8400-e29b-41d4-a716-446655440000` — good for programmatic sources
- **Natural keys**: Use `match_on` in snapshot ingest to match on any field (name, URL, etc.)

The only requirement is uniqueness within a `context.table`.

---

## Error Handling

| Status | Meaning |
|--------|---------|
| 200 | Success |
| 400 | Invalid operation (bad `op` value, missing required fields) |
| 404 | Instance not found |

Choreo is lenient by design. ALT on a nonexistent entity is a no-op, not an error. CON between nonexistent entities still creates the edge. This is intentional — operations are facts about what happened, and the log records them regardless of current projection state.

---

## Choreo 2.0 Features

### Frame Provenance

When operations carry frame metadata with `epistemic`, `source`, or `authority` keys, the projection engine records provenance per field:

```json
{
  "id": "pl0",
  "name": "Listening Room",
  "capacity": 200,
  "_provenance": {
    "name": {"op_id": 1, "epistemic": "given", "source": "manual-entry"},
    "capacity": {"op_id": 42, "epistemic": "given", "source": "manual-entry", "authority": "michael"}
  }
}
```

Operations with empty frames produce no provenance entries (backward compatible). The `epistemic` value can be a blanket string or a per-field dict.

You can filter by provenance in EOQL:
```
state(context.table="places", frame.epistemic="given")
state(context.table="people", frame.source="court-records")
```

### Operator History

Every projected entity carries an `_ops` counter tracking which operators have been applied and how many times:

```json
{
  "id": "pl0",
  "name": "Listening Room",
  "_ops": {"INS": 1, "ALT": 2, "CON": 3, "SEG": 1}
}
```

Query by operator history:
```
state(context.table="places", _ops.DES=0)     -- never designated
state(context.table="people", _ops.CON=0)     -- no connections
```

This makes the developmental cascade (NUL→DES→INS→SEG→CON→SYN→ALT→SUP→REC) queryable.

### SEG Boundary Filtering

Filter entities by their segmentation boundary in EOQL:

```
state(context.table="places", _seg.boundary="east-nashville")
```

### Absence Filters

Query by absence type — distinguish *why* something is missing:

| Filter | Meaning |
|--------|---------|
| `_has_sup=true` | Entity has at least one field in superposition (contradictory data) |
| `_has_nul=true` | Entity has at least one explicitly NUL'd field |
| `_include_dead=true` | Include dead (NUL'd) entities in results (with `_alive: false` marker) |
| `_only_dead=true` | Show only dead entities |

```
state(context.table="places", _has_sup=true)
state(context.table="places", _include_dead=true)
state(context.table="places", _only_dead=true)
```

### Replay Profiles (Read-Time Frames)

A replay profile is a frozen configuration of stances applied at query time — the interpretive context of the reading, symmetric with the write-time frame.

**Stances:**

- **Conflict**: `preserve` (show SUPs as-is), `collapse` (pick first variant), `suspend` (mark entity as suspended)
- **Boundary**: `respect` (honor SEG boundaries), `unified` (ignore all boundaries)

Apply a replay profile via DES query frame:

```json
{
  "op": "DES",
  "target": {"query": "state(context.table=\"places\")"},
  "context": {"type": "query"},
  "frame": {
    "replay": {
      "conflict": "collapse",
      "boundary": "unified"
    }
  }
}
```

**Named profiles** are stored as entities in `_rules` table and referenced by name in convenience endpoints:

```
GET /{instance}/state/{table}?replay=investigative
GET /{instance}/state/{table}?replay=publicDashboard
```

Create a named profile:
```json
{
  "op": "INS",
  "target": {"id": "profile-investigative", "conflict": "preserve", "boundary": "unified"},
  "context": {"table": "_rules"}
}
```

Because profiles are entities in the log, they can be ALT'd, DES'd, NUL'd, and their history is auditable.

### Self-Reference (Entities Targeting Entities)

The same nine operators work on the system's own structure. Reserved tables by convention:

| Table | Purpose |
|-------|---------|
| `_types` | Entity type definitions |
| `_fields` | Field definitions with policies |
| `_rules` | Feedback rules and replay profiles |
| `_emergent` | Algorithmically detected clusters |

Example — define a type:
```json
{"op": "INS", "target": {"id": "type-case", "label": "Court Case", "fields": ["filing_date", "case_number"]}, "context": {"table": "_types"}, "frame": {"authority": "michael", "epistemic": "meant"}}
```

Example — CON between types:
```json
{"op": "CON", "target": {"source": "type-case", "target": "type-defendant", "coupling": 0.9, "type": "has_defendant"}, "context": {"table": "_types"}}
```

EOQL shorthand for system tables:
```
meta(types)        → state(context.table="_types")
meta(rules)        → state(context.table="_rules")
meta(fields)       → state(context.table="_fields")
meta(emergent)     → state(context.table="_emergent")
```

### Bootstrapping Axioms (Fixed Ground)

1. The nine operators exist and have fixed semantics
2. Operations are ordered by integer ID (temporal sequence)
3. `_rules` table entities are replayed with LATEST policy (no frame applied)
4. Feedback rules fire once per incoming operation (no cascading within a pass)
5. Projection is always rebuildable from the log

Everything else — type definitions, field policies, replay profiles, feedback rules, emergence patterns — is data in the log, operated on by the same nine verbs.

### Biography Endpoint

```
GET /{instance}/biography/{entity_id}
```

Returns the full operation history for a single entity across all tables — every INS, ALT, CON, DES, NUL, etc. that ever mentioned this entity, plus its current projected state.

```json
{
  "entity_id": "pl0",
  "operation_count": 5,
  "operations": [
    {"id": 1, "ts": "...", "op": "INS", "target": {...}, "context": {...}, "frame": {...}},
    {"id": 12, "ts": "...", "op": "CON", "target": {"source": "pe0", "target": "pl0", ...}, ...}
  ],
  "current_state": {
    "id": "pl0", "table": "places", "alive": true,
    "name": "Listening Room", "status": "open", "_ops": {"INS": 1, "ALT": 1, "CON": 2}
  }
}
```

### Gap Analysis Endpoint

```
GET /{instance}/gaps/{table}
```

Returns entities grouped by which operators have NOT been applied — operator-typed absence made accessible:

```json
{
  "table": "places",
  "gaps": {
    "never_designated": ["pl1", "pl2", "pl3"],
    "never_connected": ["pl4", "pl7"],
    "never_segmented": ["pl0", "pl2", "pl7"],
    "never_synthesized": ["pl0", "pl1", "pl2", "pl3"],
    "pending_transition": ["pl2"],
    "has_contradictions": ["pl7"],
    "has_destroyed_fields": [],
    "never_validated": ["pl0", "pl1", "pl2"]
  }
}
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CHOREO_PORT` | `8420` | HTTP port |
| `CHOREO_DIR` | `./instances` | Directory for SQLite databases |
| `CHOREO_SNAPSHOT_INTERVAL` | `1000` | Operations between time-travel snapshots |

---

## Files on Disk

```
instances/
  my-instance.db        SQLite database (operations log + projection)
  another-instance.db
  webhooks.json         Outbound webhook registrations
```

Each `.db` file contains:

| Table | Purpose |
|-------|---------|
| `operations` | Append-only log (source of truth) |
| `_projected` | Materialized entity state (derived, rebuildable) |
| `_con_edges` | CON adjacency index (derived, rebuildable) |
| `_snapshots` | Periodic checkpoints for time-travel performance |
| `_meta` | Runtime metadata (projection watermark, snapshot interval) |

---

## Running in Production

```bash
# Generate nginx config with SSE-aware proxy settings
python3 choreo_runtime.py --nginx choreo.yourdomain.com > /etc/nginx/sites-available/choreo

# Enable and get SSL
ln -s /etc/nginx/sites-available/choreo /etc/nginx/sites-enabled/
certbot --nginx -d choreo.yourdomain.com
systemctl reload nginx

# Run with PM2
pm2 start choreo_runtime.py --interpreter python3 -- --port 8420 --dir /home/admin/choreo/instances
pm2 save && pm2 startup
```

The nginx config **must** disable buffering for the SSE stream endpoint, otherwise real-time events will be held by nginx and never reach clients. The `--nginx` flag generates this correctly.
