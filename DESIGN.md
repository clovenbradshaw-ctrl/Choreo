# Design Decisions

This document explains why Choreo works the way it does. Every decision traces back to a principle from Emergent Ontology or a practical constraint discovered through implementation.

---

## One Log

There is one table that matters: `operations`. Everything else is derived.

The operations log is append-only. Nothing is ever mutated or deleted from it. This isn't just an implementation choice — it's an ontological commitment. Operations are facts about what happened. Facts don't change. Your interpretation of them might change (that's what DES and REC are for), but the record of what occurred is permanent.

The projected state — the "current view" of entities in `_projected` — is a materialization for performance. It can be dropped and rebuilt from the log at any time. If the projection logic changes (say, a bug is fixed in how SYN handles edge reassignment), you rebuild. The log is truth; projection is convenience.

This means Choreo can never lose data from a software bug. A bad projection is fixable. A corrupted log isn't — but the log is append-only SQLite with WAL mode, which is about as safe as single-node storage gets.

## One Way In, One Way Out

```
POST /{instance}/operations  — the one way in
GET  /{instance}/stream      — the one way out
```

Every mutation comes through the operations endpoint. Every real-time subscription comes through the SSE stream. The convenience endpoints (`GET /state/{table}`, `GET /state/{table}/{id}`) are syntactic sugar — they execute the same EOQL queries that a DES-as-query would.

This constraint exists because of EO's principle that queries are operations. When you query, you designate your attention (DES). When the system filters, it segments the stream (SEG). When it returns results, it instantiates a projection (INS). The one-endpoint design makes this explicit: there's no hidden read path that bypasses the operator vocabulary.

## Frame Is a Column, Not a Table

The `frame` JSON column lives on every operation, alongside `target` and `context`. It's not a separate table, not a foreign key to a definitions registry, not a configuration file.

This is because in EO, the frame is part of the operation, not external to it. "Alice is the Community Lead (according to the neighborhood council)" is a single fact with an embedded frame, not a fact plus a pointer to a frame table. The authority, the confidence, the epistemic status — these travel with the operation, in the log, forever.

Storing frames separately would introduce a dependency: to interpret operation #47, you'd need to look up frame #12, which might have been modified since operation #47 was written. That breaks the immutability of the log. With the frame inline, every operation is self-contained. You can read it in isolation and know exactly what it means.

## Leniency

Choreo is lenient by design:

- ALT on a nonexistent entity is a no-op, not an error.
- CON between nonexistent entities still creates the edge.
- INS on an existing entity merges, not replaces.
- NUL on an already-dead entity is fine.

This is intentional. Operations are facts about what happened. If a scraper observed a connection between two entities, and one of those entities hasn't been created yet, the connection is still a fact. The log records it. When the missing entity eventually gets INS'd, the CON edge will be waiting.

The alternative — strict validation, foreign key constraints, referential integrity — is the relational database model. It's useful but it embeds an assumption: that the data model is complete and correct at every point in time. Choreo assumes the opposite: the data model is always incomplete, always being revised, and the log must accept everything because you don't know yet what will turn out to matter.

## Projection Is Synchronous

When you POST an operation, the projection update happens before the response returns. This is a performance tradeoff. Asynchronous projection (via a background worker) would be faster for writes but would create a window where queries return stale state.

For investigative work — where you insert a record and immediately query to see how the graph changed — synchronous projection is essential. The tradeoff is that very large batch imports (thousands of operations via `/seed`) are slower than they would be with async projection.

The snapshot mechanism mitigates this: every N operations (default 1000), a checkpoint is saved. Time-travel queries replay from the nearest snapshot forward rather than from the beginning.

## Snapshot Ingest Generates Operations

When a scraper sends raw data via REC `snapshot_ingest`, Choreo doesn't write directly to the projection. Instead, it generates granular INS, ALT, and NUL operations — one per change detected — and appends them to the log.

This is critical. It means:

1. **The log contains the diff.** You can see exactly what changed between scrapes.
2. **The interpretive frame is data.** `absence_means: "deleted"` vs `absence_means: "unchanged"` is stored in the REC operation's frame. Two scrapers with different assumptions produce different operation sequences, and both are auditable.
3. **Generated operations are traceable.** Each one carries `context.generated_by` pointing back to the REC operation that created it. You can always trace a change back to the snapshot that caused it.

The alternative — having the scraper compute its own diffs and POST individual ALT operations — pushes interpretive logic into the scraper. The scraper then needs to know the current state, decide what "missing" means, decide what "null" means. With snapshot ingest, the scraper just says "here's what I see right now" and Choreo handles the rest.

## Tables Are Implicit

There's no CREATE TABLE in Choreo. The first time you POST an operation with `context.table: "people"`, the people table exists. The first time you add a field to a target, that field exists.

This is because in EO, schema is not prior to data. Schema emerges from the pattern of operations applied over time. The moment you start inserting people with `name` and `role` fields, you've implicitly defined a schema. The moment you CON people to places, you've implicitly defined a relationship type.

If you want explicit schema — type definitions, field constraints, relationship declarations — those are entities in the log, operated on by the same nine operators. INS a type definition into `_types`. CON two types to declare that the relationship can exist. ALT the definition to add a field. The schema is data, not configuration.

## CON Stance Is a Dialectic, Not a Number

Every CON edge carries a stance — one of three dialectical positions: accidental, essential, or generative. This was a deliberate correction to an earlier version where CON used a 0.0–1.0 coupling float.

The problem with the numeric approach: a number tells you "how much" but not "what kind." It compresses away exactly the distinction that matters. An essential connection (the expenditure depends on the grant for its identity) and a generative connection (two investigators produce a capacity together that neither has alone) might both get rated "0.8" — but they're fundamentally different kinds of relationship. One is about structural dependence, the other is about productive emergence.

The three stances correspond to CON's internal dialectic. CON sits between SEG (separation) and SYN (fusion), so the tension within a connection is about what the connection does to the entities it joins:

- **Accidental** (thesis): the connection is contingent. Both entities are fully coherent without it.
- **Essential** (antithesis): the connection is necessary. One or both entities are structurally incomplete without it.
- **Generative** (synthesis): the connection produces something new. Neither entity is incomplete, but together they create a capacity that neither has alone.

Graph traversal filters by stance name instead of a threshold. `>> CON(hops=2, stance="essential")` is a more meaningful query than `>> CON(hops=2, min_coupling=0.7)` because it asks about the nature of the dependency, not an arbitrary magnitude.

Two observers can record the same connection with different stances under different frames. This isn't a conflict that needs resolution — it's two observations. The log accumulates observations; it doesn't pretend to be the single truth about things.

## The Bootstrapping Axioms

The system can operate on its own structure (feedback rules, type definitions, replay profiles). This raises a bootstrapping question: if the rules for interpreting operations are themselves operations, how do you interpret the rules?

Five axioms form the fixed ground:

1. The nine operators exist and have fixed semantics. Their behavior is hardcoded in Python, not configurable via the log.
2. Operations are ordered by integer ID. Temporal sequence is determined by insertion order, not by the `ts` field (which is metadata, not ordering).
3. Rules are replayed with LATEST-wins policy. The `_rules` table uses the most recent version of any entity. No frame is applied to rule evaluation.
4. Feedback rules fire once per incoming operation. No cascading within a single evaluation pass. This prevents infinite loops.
5. Projection is always rebuildable from the log. Drop `_projected`, `_con_edges`, and `_snapshots`, replay all operations, get the same result.

Everything else — type definitions, field policies, replay profiles, emergence patterns — is data in the log, operated on by the same nine verbs. The five axioms are the only things that aren't themselves subject to reinterpretation.

## Why SQLite

Choreo uses SQLite (one file per instance) rather than Postgres, MySQL, or a dedicated event store.

- **Zero configuration.** `pip install flask` and you're running. No database server to install or manage.
- **File-per-instance isolation.** Instances are independent. Copy, backup, delete, or move an instance by copying its `.db` file.
- **WAL mode.** SQLite's Write-Ahead Logging gives concurrent read access during writes, which is what the SSE stream needs.
- **Good enough for the scale.** Choreo is for investigative datasets, not Twitter. Hundreds of thousands of operations, not billions. SQLite handles this comfortably.

The tradeoff is no concurrent writes from multiple processes. A single Choreo server process owns the database. If you need horizontal scaling, you'd either shard by instance (each instance on a different server) or migrate to Postgres (which the schema is designed to support — the `operations` table is standard SQL).

## Why Not Event Sourcing Frameworks

Choreo looks like event sourcing and shares many principles, but it's not built on EventStoreDB, Kafka, or similar infrastructure.

- **Operator vocabulary.** Event sourcing frameworks are verb-agnostic — your events can be anything. Choreo constrains events to nine operators. This constraint is the point. It forces every change to be expressed in terms that have known projection behavior.
- **Projection is built-in.** Most event sourcing frameworks require you to write your own projections. Choreo's projection engine understands the nine operators and projects them automatically.
- **Snapshot ingest.** The REC operator with diff semantics is not a standard event sourcing pattern. It bridges the gap between "I have a scraper that gives me current state" and "I need an event log that shows what changed."
- **Single-file deployment.** One Python file, one SQLite file per instance. No Kafka cluster, no ZooKeeper, no infrastructure.
