# Choreo System Prompt Guide

You have access to a tool called **Choreo** — an event-sourced data store you interact with over HTTP. This guide is everything you need to send data to it and parse what comes back.

---

## What Choreo Is (In One Paragraph)

Choreo is an append-only event store. You write **operations** to it (there are exactly nine kinds). It maintains a **projected state** — a materialized current view of all entities — by replaying those operations. The log is the truth; the projection is derived and rebuildable. There is one way in (`POST /{instance}/operations`) and one way out (`GET /{instance}/stream` via SSE). Everything else is convenience.

---

## Connection Details

- **Base URL**: `https://choreo.intelechia.com` (default; configurable)
- **Content-Type**: Always `application/json` for requests and responses
- **CORS**: Enabled on all endpoints
- **Auth**: None (designed for local/trusted network use)

---

## The One Thing You Send: An Operation

Every mutation and every query is a single JSON object POSTed to the same endpoint:

```
POST /{instance}/operations
Content-Type: application/json
```

The shape is always:

```json
{
  "op": "<OPERATOR>",
  "target": "tables.{table}.{entityId}",
  "operand": { "fields.key": "value" },
  "frame": { }
}
```

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `op` | string | Yes | One of exactly nine operators (see below) |
| `target` | string | Yes | Dot-notation address — **what** and **where**: `tables.{table}.{entityId}` |
| `operand` | object | Yes | The payload — dot-notation keys: `fields.*` for entity data, `conn.*` for CON edges, `merge.*` for SYN, `meta.*` for routing metadata |
| `frame` | object | No | **Why** — interpretive metadata (authority, epistemic status, source) |

**Operand namespaces:**

| Prefix | Maps to | Used by |
|--------|---------|---------|
| `fields.{name}` | Entity field data | INS, ALT, DES, SUP, REC |
| `conn.{key}` | CON relationship properties (`source`, `target`, `stance`, `coupling`) | CON |
| `merge.{key}` | SYN merge properties (`merge_into`, `merge_from`) | SYN |
| `meta.{key}` | Routing metadata (`type`, `boundary`, `source`, etc.) | All |

**Target address formats:**

| Format | Meaning |
|--------|---------|
| `tables.{table}.{entityId}` | Entity-level operation |
| `tables.{table}` | Table-level operation (CON, SYN, REC) |
| `tables.{table}.{entityId}.fields.{field}` | Field-level operation (NUL field) |

That's it. There is no other write format. No PUT, no PATCH, no GraphQL. One endpoint, one shape.

---

## The Nine Operators

There are exactly nine. Every operation uses one. They are organized in three triads:

### Identity Triad — "What exists?"

| Op | Name | What It Does |
|----|------|-------------|
| `NUL` | Destruction | Kills an entity or a field. The only operator that truly destroys. |
| `DES` | Designation | Applies an interpretive frame to an entity. Also used for **all queries**. |
| `INS` | Instantiation | Creates an entity. If it already exists, merges new fields into it. |

### Structure Triad — "How do things relate?"

| Op | Name | What It Does |
|----|------|-------------|
| `SEG` | Segmentation | Tags an entity with a boundary/scope for filtering. |
| `CON` | Connection | Creates a directed edge between two entities with a stance and coupling. |
| `SYN` | Synthesis | Merges entity B into entity A. B dies. A absorbs B's data and edges. |

### Time Triad — "How do things change?"

| Op | Name | What It Does |
|----|------|-------------|
| `ALT` | Alternation | Updates specific fields on an existing entity. Partial update. |
| `SUP` | Superposition | Stores multiple simultaneous values for a field (genuine ambiguity). |
| `REC` | Reconfiguration | Snapshot ingest, feedback rules, or emergence detection. System operating on itself. |

---

## Instances

Choreo supports multiple isolated databases called **instances**. Each is a separate SQLite file. You must create an instance before posting operations to it.

### Create an Instance

```
POST /instances
Content-Type: application/json

{"slug": "my-project"}
```

Slug rules: lowercase alphanumeric, hyphens, underscores.

**Response:**
```json
{
  "slug": "my-project",
  "operations": 0,
  "entities": 0,
  "tables": [],
  "db_size": 12288
}
```

### List All Instances

```
GET /instances
```

**Response:**
```json
{
  "instances": [
    {"slug": "my-project", "operations": 247, "entities": 34, "tables": ["places", "people"], "db_size": 102400}
  ]
}
```

### Delete an Instance (Irreversible)

```
DELETE /instances/{slug}
```

---

## Sending Operations: Complete Reference

Every example below is a `POST /{instance}/operations` with `Content-Type: application/json`.

### INS — Create an Entity

```json
{
  "op": "INS",
  "target": "tables.places.pl0",
  "operand": {
    "fields.name": "Listening Room",
    "fields.type": "venue",
    "fields.status": "open",
    "fields.capacity": 200,
    "meta.source": "manual-entry"
  }
}
```

**Rules:**
- The entity ID is the last segment of the `target` address (`pl0`). It must be unique within its table.
- The table is the second segment (`places`). Tables are created implicitly — no setup needed.
- All `fields.*` keys in `operand` become the entity's data.
- If an entity with this ID already exists in this table, INS **merges** new fields into existing data (does NOT replace).

**Response:**
```json
{"op_id": 1, "ts": "2026-02-15T16:22:01.000Z", "op": "INS"}
```

---

### ALT — Update Fields on an Existing Entity

```json
{
  "op": "ALT",
  "target": "tables.places.pl0",
  "operand": {
    "fields.status": "closed",
    "fields.capacity": 180
  },
  "frame": {"reason": "Renovation underway"}
}
```

**Rules:**
- Only `fields.*` keys in `operand` are updated. All other fields on the entity are untouched.
- If the entity doesn't exist, ALT is a **silent no-op** (not an error).
- `frame` is optional metadata stored in the log about *why* this change happened.

**Response:**
```json
{"op_id": 12, "ts": "2026-02-15T16:25:00.000Z", "op": "ALT"}
```

---

### NUL — Destroy an Entity or a Field

**Entity-level destruction:**
```json
{
  "op": "NUL",
  "target": "tables.places.pl0"
}
```
Marks entity as dead. Kills all its CON edges.

**Field-level destruction** (5-part address):
```json
{
  "op": "NUL",
  "target": "tables.places.pl0.fields.capacity"
}
```
Marks a single field as explicitly destroyed. Stores `{"_nul": true, "op_id": N}` on that field in the projection. This is distinct from a field being absent (never observed) or null.

**Response:**
```json
{"op_id": 13, "ts": "2026-02-15T16:26:00.000Z", "op": "NUL"}
```

---

### CON — Connect Two Entities

```json
{
  "op": "CON",
  "target": "tables.places",
  "operand": {
    "conn.source": "pl0",
    "conn.target": "pe0",
    "conn.stance": "essential",
    "conn.coupling": 0.8,
    "conn.type": "works_at"
  }
}
```

**Rules:**
- `target` is table-scoped (no entity ID) — CON creates an edge, not an entity state.
- `conn.source` and `conn.target` are entity IDs. They can be in different tables.
- `conn.stance` is one of: `"accidental"`, `"essential"`, `"generative"`. If omitted, inferred from coupling (>=0.7 essential, >=0.55 generative, else accidental).
- `conn.coupling` is a float from 0.0 to 1.0. Defaults to 0.5.
- Additional `conn.*` keys (like `conn.type`) are stored as edge metadata.
- Edges are directional (source -> target) but traversal queries follow both directions.
- CON between nonexistent entities still creates the edge (leniency by design).

**The three stances mean:**

| Stance | Meaning | Example |
|--------|---------|---------|
| `accidental` | Contingent. Remove it and both entities are fine. | "person frequents venue" |
| `essential` | Necessary. One/both entities incomplete without it. | "defendant named in case" |
| `generative` | Productive. Creates capacity neither has alone. | "co-investigators on story" |

**Response:**
```json
{"op_id": 14, "ts": "2026-02-15T16:27:00.000Z", "op": "CON"}
```

---

### SYN — Merge Two Entities

```json
{
  "op": "SYN",
  "target": "tables.places",
  "operand": {
    "merge.merge_into": "pl0",
    "merge.merge_from": "pl7"
  }
}
```

**Rules:**
- Merges `merge.merge_from` into `merge.merge_into`. Data from the surviving entity wins on field conflicts.
- `merge_from` is marked dead.
- All CON edges pointing to `merge_from` are reassigned to `merge_into`.
- A `_syn_from` array on the surviving entity tracks merge history.

**Response:**
```json
{"op_id": 15, "ts": "2026-02-15T16:28:00.000Z", "op": "SYN"}
```

---

### SUP — Store Multiple Simultaneous Values

```json
{
  "op": "SUP",
  "target": "tables.places.pl0",
  "operand": {
    "fields.field": "capacity",
    "fields.variants": [
      {"source": "permit", "value": 200},
      {"source": "website", "value": 180},
      {"source": "fire_marshal", "value": 195}
    ]
  }
}
```

**Rules:**
- `fields.field` names the field to put in superposition. `fields.variants` is the list of simultaneous values.
- The field's projected value becomes `{"_sup": [...variants], "op_id": N}`.
- SUP is for genuine ambiguity (two sources disagree), not for versioning (the log already tracks every change).
- Queries match against any variant's value.

**Response:**
```json
{"op_id": 16, "ts": "2026-02-15T16:29:00.000Z", "op": "SUP"}
```

---

### DES — Designate (Frame an Entity) or Query

DES has **two modes**. This is the most important operator to understand.

**Mode 1: Designation (framing an entity)**
```json
{
  "op": "DES",
  "target": "tables.people.pe0",
  "operand": {
    "fields.title": "Community Lead",
    "fields.appointed_by": "neighborhood_council"
  },
  "frame": {"authority": "city_council", "epistemic": "meant"}
}
```
Stores the fields in a `_des` namespace on the entity — separate from its data fields. Designations are about interpretation, not observation.

**Warning:** A DES without a frame returns `"_warning": "unframed_designation"` in the response. The system flags frame-hiding.

**Mode 2: Query (designating attention) — THIS IS HOW YOU READ DATA**

> DES queries use the legacy object form for `target` (not dot-notation). This is intentional — a query designates attention, not an entity address.

```json
{
  "op": "DES",
  "target": {"query": "state(context.table=\"places\")"},
  "context": {"type": "query"}
}
```
This is the primary way to read data. Queries use a language called EOQL (covered in detail below).

**Queries are NOT logged by default.** To create an audit trail entry, add `"audit": true` to the frame:
```json
{
  "op": "DES",
  "target": {"query": "state(context.table=\"places\")"},
  "context": {"type": "query"},
  "frame": {"audit": true}
}
```
The response will include `"audited": true` and `"audit_op_id"`.

---

### SEG — Tag an Entity with a Boundary

```json
{
  "op": "SEG",
  "target": "tables.places.pl0",
  "operand": {
    "meta.boundary": "downtown-district"
  }
}
```

Records boundary metadata on the entity (stored in a `_seg` array). Used for filtering at query time.

**Response:**
```json
{"op_id": 17, "ts": "2026-02-15T16:30:00.000Z", "op": "SEG"}
```

---

### REC — Reconfiguration (Three Modes)

REC is the system operating on its own output. It has three context types:

#### Mode 1: Snapshot Ingest (`meta.type: "snapshot_ingest"`)

Feed Choreo raw data and let it figure out what changed:

```json
{
  "op": "REC",
  "target": "tables.places",
  "operand": {
    "meta.type": "snapshot_ingest",
    "fields.rows": [
      {"name": "Listening Room", "hours": "6p-12a", "status": "open"},
      {"name": "Five Points Pizza", "hours": "11a-10p", "status": "open"},
      {"name": "Brand New Place", "hours": "9a-5p", "status": "open"}
    ]
  },
  "frame": {
    "match_on": "name",
    "absence_means": "unchanged",
    "null_fields_mean": "unchanged",
    "ignore_fields": ["last_scraped"]
  }
}
```

**Frame parameters (all optional with defaults):**

| Parameter | Values | Default | What It Does |
|-----------|--------|---------|-------------|
| `match_on` | any field name | `"id"` | Which field in incoming rows to use as entity key for matching against existing entities |
| `absence_means` | `"unchanged"`, `"deleted"`, `"investigate"` | `"unchanged"` | What to do when an entity exists in projection but is missing from the snapshot |
| `null_fields_mean` | `"unchanged"`, `"cleared"`, `"unknown"` | `"unchanged"` | What to do when a field in an incoming row is null |
| `ignore_fields` | array of field names | `[]` | Fields to skip during diffing |

**What Choreo generates from the diff:**

| Situation | Generated Op |
|-----------|-------------|
| New entity in snapshot (no match in projection) | `INS` |
| Existing entity with changed field values | `ALT` |
| Missing entity + `absence_means: "deleted"` | `NUL` |
| Missing entity + `absence_means: "investigate"` | `DES` with `flag: "absent_from_snapshot"` |
| Null field + `null_fields_mean: "cleared"` | `NUL` (field-level) |
| Null field + `null_fields_mean: "unknown"` | `SUP` with both the current and null values |

**Response:**
```json
{
  "op_id": 47,
  "ts": "2026-02-15T17:00:00.000Z",
  "op": "REC",
  "generated_count": 3,
  "generated": [
    {"id": 48, "op": "ALT", "target": {"id": "Listening Room", "hours": "6p-12a"}, "context": {"table": "places", "generated_by": 47}},
    {"id": 49, "op": "INS", "target": {"id": "Brand New Place", "hours": "9a-5p", "status": "open"}, "context": {"table": "places", "generated_by": 47}}
  ]
}
```

Every generated operation carries `"generated_by": <rec_op_id>` in its context for traceability.

#### Mode 2: Feedback Rule (`meta.type: "feedback_rule"`)

Register a rule that watches for operations and generates new operations in response:

```json
{
  "op": "REC",
  "target": "tables._rules.rule-flag-absent",
  "operand": {
    "meta.type": "feedback_rule",
    "fields.trigger": {
      "op": "DES",
      "match": {"target.flag": "absent_from_snapshot"}
    },
    "fields.action": {
      "op": "ALT",
      "target_template": {"id": "{trigger.target.id}", "status": "needs_investigation"},
      "context": {"table": "{trigger.context.table}"}
    }
  },
  "frame": {"authority": "michael"}
}
```

**Rules:**
- The rule is stored as an entity in the `_rules` table.
- Template values like `{trigger.target.id}` are interpolated from the triggering operation.
- Rules fire once per incoming operation. **No cascading** — a rule-generated operation will NOT trigger other rules in the same pass.
- Generated operations carry `generated_by_rule` in their context.

#### Mode 3: Emergence Scan (`meta.type: "emergence_scan"`)

Scan the CON graph for unnamed clusters:

```json
{
  "op": "REC",
  "target": "tables._emergent",
  "operand": {
    "meta.type": "emergence_scan",
    "fields.algorithm": "connected_components",
    "fields.min_cluster_size": 3,
    "fields.min_internal_coupling": 0.5
  },
  "frame": {"authority": "system", "confidence": "algorithmic"}
}
```

Finds clusters in the CON graph meeting size/coupling thresholds and generates DES operations for unnamed clusters in the `_emergent` table.

---

## Reading Data: Three Methods

### Method 1: EOQL Queries (Via DES)

The primary way to read. POST a DES operation with `context.type: "query"`:

```json
{
  "op": "DES",
  "target": {"query": "<EOQL string>"},
  "context": {"type": "query"}
}
```

#### EOQL Function: `state()` — Get Current Projected Entities

```
state(context.table="places")
state(context.table="places", status="open")
state(context.table="places", at="2025-06-15T00:00:00Z")
state(target.id="pl0")
```

- `context.table` selects the table.
- `target.*` or bare keys filter on entity field values. Filters are AND'd.
- `at` enables time travel — replays state as of that timestamp.
- The `target.` prefix is optional — bare keys are treated as target fields.

**Response:**
```json
{
  "type": "state",
  "table": "places",
  "count": 2,
  "entities": [
    {"id": "pl0", "name": "Listening Room", "status": "open", "capacity": 200},
    {"id": "pl1", "name": "Five Points Pizza", "status": "open"}
  ]
}
```

#### Special Filters for `state()`

| Filter | Purpose |
|--------|---------|
| `_ops.CON=0` | Entities never connected |
| `_ops.DES=0` | Entities never designated |
| `_seg.boundary="X"` | Entities within a specific boundary |
| `_has_sup=true` | Entities with at least one field in superposition |
| `_has_nul=true` | Entities with at least one explicitly destroyed field |
| `_include_dead=true` | Include NUL'd (dead) entities in results |
| `_only_dead=true` | Show ONLY dead entities |
| `frame.epistemic="given"` | Filter by provenance epistemic status |
| `frame.source="court-records"` | Filter by provenance source |

#### EOQL Function: `stream()` — Get Raw Operations from the Log

```
stream(op=ALT, context.table="places")
stream(op=CON, limit=50)
stream(after="2026-01-01", before="2026-02-01")
```

- Returns operations themselves, not projected entities.
- `limit` defaults to 100.
- `after`/`before` filter by timestamp.
- Results in reverse chronological order.

**Response:**
```json
{
  "type": "stream",
  "count": 3,
  "operations": [
    {"id": 48, "ts": "2026-02-15T17:00:00.000Z", "op": "ALT", "target": {"id": "pl0", "status": "closed"}, "context": {"table": "places"}, "frame": {}}
  ]
}
```

#### EOQL Function: `meta()` — Query System Tables

```
meta(types)     → state(context.table="_types")
meta(rules)     → state(context.table="_rules")
meta(fields)    → state(context.table="_fields")
meta(emergent)  → state(context.table="_emergent")
```

Shorthand for querying Choreo's own structure.

#### EOQL: `>> CON()` — Graph Traversal

Chain onto a `state()` query to traverse the CON graph:

```
state(target.id="pl0") >> CON(hops=2)
state(target.id="pl0") >> CON(hops=2, stance="essential")
state(target.id="pl0") >> CON(hops=2, exclude="accidental")
state(target.id="pl0") >> CON(hops=1, min_coupling=0.5)
```

- `hops` controls traversal depth (default 1). Uses breadth-first search.
- `stance` filters to only follow edges with a specific stance.
- `exclude` excludes edges with a specific stance.
- `min_coupling` filters edges below a threshold.
- Traversal follows edges in both directions regardless of declared source/target.
- Traversal crosses table boundaries.

**Response (augmented with `con_chain`):**
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
      {"id": "pe0", "table": "people", "name": "Tomas", "role": "musician"},
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

**Key fields to parse:**
- `entities` — the original query results
- `con_chain.reached` — entities discovered by traversal (from any table)
- `con_chain.edges` — all CON edges encountered, with stance, coupling, and metadata

#### EOQL String Escaping

EOQL query strings live inside the `target.query` JSON field. Quotes inside the EOQL string must be escaped:

```json
{"query": "state(context.table=\"places\", status=\"open\")"}
```

Or use single quotes in the EOQL string if your JSON encoder handles it:
```json
{"query": "state(context.table='places', status='open')"}
```

---

### Method 2: Convenience GET Endpoints

These are syntactic sugar. They bypass the DES operation and hit the projection directly.

| Endpoint | What It Returns |
|----------|----------------|
| `GET /{instance}/state/{table}` | All alive entities in a table |
| `GET /{instance}/state/{table}?at=2025-06-15T00:00:00Z` | Time-travel query |
| `GET /{instance}/state/{table}?replay=investigative` | Query with named replay profile |
| `GET /{instance}/state/{table}/{entity_id}` | Single entity lookup |
| `GET /{instance}/biography/{entity_id}` | Full operation history for an entity |
| `GET /{instance}/gaps/{table}` | Operator-typed absence report |

**`GET /state/{table}` response:**
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

**`GET /state/{table}/{entity_id}` response:**
```json
{"id": "pl0", "table": "places", "alive": true, "name": "Listening Room", "status": "open"}
```

**`GET /biography/{entity_id}` response:**
```json
{
  "entity_id": "pl0",
  "operation_count": 5,
  "operations": [
    {"id": 1, "ts": "...", "op": "INS", "target": {"id": "pl0", "name": "Listening Room"}, "context": {"table": "places"}, "frame": {}},
    {"id": 12, "ts": "...", "op": "CON", "target": {"source": "pe0", "target": "pl0", "coupling": 0.7}, "context": {"table": "places"}, "frame": {}}
  ],
  "current_state": {
    "id": "pl0", "table": "places", "alive": true,
    "name": "Listening Room", "status": "open",
    "_ops": {"INS": 1, "ALT": 1, "CON": 2}
  }
}
```

**`GET /gaps/{table}` response:**
```json
{
  "table": "places",
  "gaps": {
    "never_designated": ["pl1", "pl2"],
    "never_connected": ["pl4"],
    "never_segmented": ["pl0", "pl2"],
    "never_synthesized": ["pl0", "pl1", "pl2"],
    "pending_transition": ["pl2"],
    "has_contradictions": ["pl7"],
    "has_destroyed_fields": [],
    "never_validated": ["pl0", "pl1"]
  }
}
```

---

### Method 3: SSE Stream (Real-Time)

```
GET /{instance}/stream
GET /{instance}/stream?last_id=47
```

Server-Sent Events endpoint. Every operation appended to the log is emitted in real time.

**Event format:**
```
id: 48
event: op
data: {"id":48,"ts":"2026-02-15T16:22:01.000Z","op":"ALT","target":{"id":"pl0","status":"closed"},"context":{"table":"places"},"frame":{"reason":"Renovation"}}
```

**Parsing rules:**
- Event type is `op`. Listen with `addEventListener("op", ...)`.
- `data` is a JSON string. Parse it with `JSON.parse()`.
- Pass `last_id` to resume from a specific point (all operations after that ID are sent).
- The stream sends `: heartbeat\n\n` comments when idle to keep the connection alive.
- The stream never closes server-side. It runs until the client disconnects.

**JavaScript:**
```javascript
const sse = new EventSource("https://choreo.intelechia.com/my-instance/stream");
sse.addEventListener("op", (event) => {
  const op = JSON.parse(event.data);
  console.log(op.op, op.target, op.context);
});
```

**Python:**
```python
import requests, json
with requests.get("https://choreo.intelechia.com/my-instance/stream", stream=True) as r:
    for line in r.iter_lines():
        if line and line.startswith(b"data: "):
            op = json.loads(line[6:])
            print(f"{op['op']} {op['target']}")
```

---

## Parsing Projected Entities: What Fields to Expect

When you read entities back (via `state()`, convenience endpoints, or SSE), they may contain special metadata fields. Here's what each looks like and what it means:

### Normal Fields

```json
{"id": "pl0", "name": "Listening Room", "status": "open", "capacity": 200}
```

### `_sup` — Superposition (Multi-Value Fields)

A field with multiple simultaneous values from different sources:
```json
{
  "capacity": {
    "_sup": [
      {"source": "permit", "value": 200},
      {"source": "website", "value": 180}
    ],
    "op_id": 23
  }
}
```
**How to handle:** Check if a field value is an object with a `_sup` key. If so, it's a list of variants. Choose a resolution strategy: take the first, prefer a specific source, present all to the user, etc.

### `_nul` — Explicitly Destroyed Fields

A field that existed and was then explicitly destroyed:
```json
{
  "old_field": {"_nul": true, "op_id": 45}
}
```
**How to handle:** Check if a field value is an object with `_nul: true`. This is NOT the same as the field being absent (never existed) or null (empty). It means the field was deliberately killed.

### `_des` — Designation Metadata

Interpretive frames applied via DES operations (separate from data fields):
```json
{
  "_des": {
    "title": "Community Lead",
    "appointed_by": "neighborhood_council"
  }
}
```
**How to handle:** These are metadata about how an entity has been framed/interpreted, not its observable data.

### `_syn_from` — Merge History

Array of entity IDs that were merged into this entity via SYN:
```json
{
  "_syn_from": ["pl7", "pl12"]
}
```

### `_seg` — Segmentation Boundaries

Array of boundary tags applied via SEG:
```json
{
  "_seg": [
    {"boundary": "downtown-district", "op_id": 33}
  ]
}
```

### `_ops` — Operator History Counter

Tracks which operators have been applied to this entity and how many times:
```json
{
  "_ops": {"INS": 1, "ALT": 2, "CON": 3, "SEG": 1}
}
```
Useful for detecting developmental stage: has this entity ever been designated? connected? validated?

### `_provenance` — Per-Field Provenance

When operations carry frame metadata with `epistemic`, `source`, or `authority` keys, per-field provenance is recorded:
```json
{
  "_provenance": {
    "name": {"op_id": 1, "epistemic": "given", "source": "manual-entry"},
    "capacity": {"op_id": 42, "epistemic": "given", "source": "manual-entry", "authority": "michael"}
  }
}
```
**How to handle:** For each field you care about, check `_provenance[field_name]` to see where it came from, under whose authority, and with what epistemic status.

**Epistemic values:** `"given"` (observed fact), `"meant"` (human interpretation), `"derived"` (computed value).

---

## Frame Provenance: Tracking Where Data Came From

When writing operations, include `epistemic`, `source`, and/or `authority` in the `frame` to activate provenance tracking:

```json
{
  "op": "INS",
  "target": "tables.places.pl0",
  "operand": {
    "fields.name": "Listening Room",
    "fields.capacity": 200
  },
  "frame": {
    "epistemic": "given",
    "source": "manual-entry",
    "authority": "michael"
  }
}
```

This writes `_provenance` entries for every field in the target. For per-field epistemic values:

```json
{
  "frame": {
    "epistemic": {"name": "given", "capacity": "derived"},
    "source": "scraper-v2"
  }
}
```

---

## Replay Profiles: Read-Time Interpretation

Control how SUP and SEG data is presented at query time:

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

> DES queries retain the legacy object-form `target`/`context` — dot-notation is for mutation operations only.

| Setting | Values | Effect |
|---------|--------|--------|
| `conflict` | `"preserve"` (default) | SUP fields returned as-is with all variants |
| | `"collapse"` | First variant wins; `_collapsed` array lists affected fields |
| | `"suspend"` | Entities with SUP fields marked `_suspended: true` |
| `boundary` | `"respect"` (default) | SEG boundaries honored as filters |
| | `"unified"` | All boundaries ignored; everything visible |

Named profiles are stored as entities in `_rules` and referenced by name:
```
GET /{instance}/state/{table}?replay=investigative
```

---

## Batch Import

Seed an instance with multiple operations at once:

```
POST /instances/{slug}/seed
Content-Type: application/json

{
  "operations": [
    {"op": "INS", "target": "tables.people.p1", "operand": {"fields.name": "Alice"}},
    {"op": "INS", "target": "tables.people.p2", "operand": {"fields.name": "Bob"}},
    {"op": "CON", "target": "tables.people", "operand": {"conn.source": "p1", "conn.target": "p2", "conn.coupling": 0.6}}
  ]
}
```

Operations are appended and projected in order.

---

## Outbound Webhooks

Register URLs to receive POSTs for operations as they happen:

**Register:**
```
POST /{instance}/webhooks
Content-Type: application/json

{"url": "https://example.com/webhook", "filter": ["ALT", "INS", "NUL"], "active": true}
```
- `filter` is optional (omit to receive all operations).
- `active` defaults to `true`.

**What your webhook receives:**
```
POST https://example.com/webhook
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

**List:** `GET /{instance}/webhooks`

**Remove:**
```
DELETE /{instance}/webhooks
Content-Type: application/json

{"url": "https://example.com/webhook"}
```

---

## Rebuild Projections

If projections get corrupted or you update projection logic:

```
POST /instances/{slug}/rebuild
```

Drops all derived tables and replays the entire operations log. The log is never touched.

---

## Error Handling

| Status Code | Meaning |
|-------------|---------|
| `200` | Success. Parse the JSON response body. |
| `400` | Bad request. Invalid `op` value, missing required fields (like `target.id` for INS). |
| `404` | Instance not found. |

**Choreo is lenient by design.** These are NOT errors:
- ALT on a nonexistent entity → silent no-op (returns 200)
- CON between nonexistent entities → creates the edge (returns 200)
- INS on an existing entity → merges fields (returns 200)
- NUL on an already-dead entity → returns 200

Operations are facts about what happened. The log records them regardless of current projection state.

---

## Absence Types: Why Something Is Missing

Every operator implies its own kind of absence. When data is missing from Choreo, the *type* of missing matters:

| Absence Type | Operator Not Applied | What It Means |
|-------------|---------------------|---------------|
| Unknown | ¬INS | Never observed. No record exists. |
| Undesignated | ¬DES | Exists but hasn't been named/categorized. |
| Inapplicable | ¬SEG | Outside the current boundary/scope. |
| Unconnected | ¬CON | No relationships. Structurally isolated. |
| Unfused | ¬SYN | Components present but not merged. |
| Pending | ¬ALT | Between states. Transition hasn't happened. |
| Withheld | ¬SUP | Multiple possibilities being preserved. |
| Pre-recursive | ¬REC | Not yet part of a feedback loop. |
| **Destroyed** | **NUL** | **Actually gone. Explicitly removed.** |

Only NUL is true destruction. All other absences are inferred from which operators haven't been applied. Use `GET /{instance}/gaps/{table}` or the `_ops` counter to detect these.

---

## Quick Reference: Complete Endpoint Summary

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/instances` | List all instances |
| `POST` | `/instances` | Create instance (`{"slug": "..."}`) |
| `DELETE` | `/instances/{slug}` | Delete instance |
| `POST` | `/instances/{slug}/seed` | Batch import operations |
| `POST` | `/instances/{slug}/rebuild` | Rebuild projections from log |
| `POST` | `/{instance}/operations` | **THE ONE WAY IN** — all mutations and queries |
| `GET` | `/{instance}/stream` | **THE ONE WAY OUT** — SSE real-time stream |
| `GET` | `/{instance}/state/{table}` | Get all entities in table |
| `GET` | `/{instance}/state/{table}/{id}` | Get single entity |
| `GET` | `/{instance}/biography/{id}` | Full history for entity |
| `GET` | `/{instance}/gaps/{table}` | Absence analysis |
| `GET` | `/{instance}/webhooks` | List webhooks |
| `POST` | `/{instance}/webhooks` | Register webhook |
| `DELETE` | `/{instance}/webhooks` | Remove webhook |

---

## Quick Reference: Operation Shapes

Copy-paste templates for every operator:

```jsonc
// INS — Create entity
{"op": "INS", "target": "tables.TABLE.ID", "operand": {"fields.field": "value"}}

// ALT — Update fields
{"op": "ALT", "target": "tables.TABLE.ID", "operand": {"fields.field": "new_value"}}

// NUL — Destroy entity
{"op": "NUL", "target": "tables.TABLE.ID"}

// NUL — Destroy field
{"op": "NUL", "target": "tables.TABLE.ID.fields.FIELD"}

// CON — Connect entities
{"op": "CON", "target": "tables.TABLE", "operand": {"conn.source": "ID_A", "conn.target": "ID_B", "conn.stance": "essential", "conn.coupling": 0.8}}

// SYN — Merge entities
{"op": "SYN", "target": "tables.TABLE", "operand": {"merge.merge_into": "KEEP_ID", "merge.merge_from": "KILL_ID"}}

// SUP — Multi-value field
{"op": "SUP", "target": "tables.TABLE.ID", "operand": {"fields.field": "FIELD", "fields.variants": [{"source": "SRC", "value": "VAL"}]}}

// DES — Designate entity
{"op": "DES", "target": "tables.TABLE.ID", "operand": {"fields.label": "value"}, "frame": {"authority": "WHO"}}

// DES — Query (legacy object form — dot-notation not used for queries)
{"op": "DES", "target": {"query": "state(context.table=\"TABLE\")"}, "context": {"type": "query"}}

// SEG — Boundary tag
{"op": "SEG", "target": "tables.TABLE.ID", "operand": {"meta.boundary": "BOUNDARY"}}

// REC — Snapshot ingest
{"op": "REC", "target": "tables.TABLE", "operand": {"meta.type": "snapshot_ingest", "fields.rows": [{"id": "1", "field": "val"}]}, "frame": {"match_on": "id", "absence_means": "unchanged"}}

// REC — Feedback rule
{"op": "REC", "target": "tables._rules.rule-name", "operand": {"meta.type": "feedback_rule", "fields.trigger": {"op": "ALT", "match": {"target.field": "value"}}, "fields.action": {"op": "ALT", "target_template": {"id": "{trigger.target.id}", "field": "new_value"}, "context": {"table": "{trigger.context.table}"}}}}

// REC — Emergence scan
{"op": "REC", "target": "tables._emergent", "operand": {"meta.type": "emergence_scan", "fields.algorithm": "connected_components", "fields.min_cluster_size": 3, "fields.min_internal_coupling": 0.5}}
```

---

## Common Patterns

### Pattern: Create Some Entities Then Query Them

```python
import requests

BASE = "https://choreo.intelechia.com"
INST = "my-project"

# Create instance
requests.post(f"{BASE}/instances", json={"slug": INST})

# Insert entities
for place in [("pl0", "Listening Room"), ("pl1", "Five Points Pizza")]:
    requests.post(f"{BASE}/{INST}/operations", json={
        "op": "INS",
        "target": f"tables.places.{place[0]}",
        "operand": {"fields.name": place[1], "fields.status": "open"}
    })

# Query them back
r = requests.post(f"{BASE}/{INST}/operations", json={
    "op": "DES",
    "target": {"query": 'state(context.table="places")'},
    "context": {"type": "query"}
})
result = r.json()
for entity in result["entities"]:
    print(f"{entity['id']}: {entity['name']}")
```

### Pattern: Feed Raw Data and Let Choreo Diff It

```python
scraped = [
    {"name": "Listening Room", "hours": "6p-12a", "status": "open"},
    {"name": "New Coffee Shop", "hours": "7a-4p", "status": "open"},
]

r = requests.post(f"{BASE}/{INST}/operations", json={
    "op": "REC",
    "target": "tables.places",
    "operand": {"meta.type": "snapshot_ingest", "fields.rows": scraped},
    "frame": {"match_on": "name", "absence_means": "unchanged"}
})
result = r.json()
print(f"Generated {result['generated_count']} ops from snapshot")
```

### Pattern: Explore a Graph

```python
r = requests.post(f"{BASE}/{INST}/operations", json={
    "op": "DES",
    "target": {"query": 'state(target.id="pl0") >> CON(hops=2, stance="essential")'},
    "context": {"type": "query"}
})
result = r.json()

# Original entity
print("Base:", result["entities"])

# Everything reachable via essential connections
for reached in result["con_chain"]["reached"]:
    print(f"  -> {reached['table']}/{reached['id']}: {reached.get('name', '?')}")

# The edges that connect them
for edge in result["con_chain"]["edges"]:
    print(f"  {edge['source']} --[{edge['stance']}]--> {edge['target']}")
```

### Pattern: Real-Time UI Updates

```javascript
// Load initial state, then subscribe to changes
const entities = new Map();

// Initial load
const res = await fetch(`${BASE}/${INST}/state/places`);
const data = await res.json();
data.entities.forEach(e => entities.set(e.id, e));

// Subscribe to live updates
const sse = new EventSource(`${BASE}/${INST}/stream`);
sse.addEventListener("op", (event) => {
  const op = JSON.parse(event.data);
  if (op.context.table !== "places") return;

  switch (op.op) {
    case "INS":
      entities.set(op.target.id, op.target);
      break;
    case "ALT":
      const existing = entities.get(op.target.id);
      if (existing) Object.assign(existing, op.target);
      break;
    case "NUL":
      entities.delete(op.target.id);
      break;
  }
  renderUI();
});
```

---

## Key Behavioral Notes

1. **Projection is synchronous.** When you POST an operation, the response only returns after projection is updated. A GET immediately after a POST will see the change.

2. **Tables are implicit.** The first time you use `context.table: "whatever"`, that table exists. No setup needed.

3. **IDs are yours to assign.** Choreo does not auto-generate IDs. You provide `target.id` and it must be unique within the table.

4. **The log is immutable.** Nothing in the `operations` table is ever updated or deleted. This is the source of truth.

5. **Projection is disposable.** The projected state can be rebuilt at any time from the log via `POST /instances/{slug}/rebuild`.

6. **Frame data triggers provenance.** Include `epistemic`, `source`, or `authority` in the frame to get per-field provenance tracking. Omit the frame entirely for backward-compatible behavior with no provenance.

7. **Queries are DES operations.** There is no separate "read" API. Reading is designating attention, which is a DES. This is by design.

8. **Snapshot ingest generates operations.** REC with `snapshot_ingest` doesn't write to projection directly — it generates INS/ALT/NUL operations that go through the normal log, making every change traceable.

9. **Time travel via `at` parameter.** Any `state()` query or `GET /state/{table}` endpoint supports `?at=<ISO_TIMESTAMP>` to see past state.

10. **Webhooks are fire-and-forget.** 10-second timeout, background thread pool. If your endpoint is down, operations are not retried.
