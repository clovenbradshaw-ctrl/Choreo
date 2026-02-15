# The Nine Operators

Choreo uses exactly nine operators. Not eight, not ten. They come from Emergent Ontology (EO), a framework where phenomena are relational configurations rather than fixed entities. The claim is that these nine operators are sufficient to describe any transformation to any system.

They're organized in three triads. Each triad addresses a fundamental domain. Within each triad, the three operators form a developmental sequence: the first establishes a ground, the second introduces structure, the third produces emergence.

## The Developmental Cascade

```
NUL → DES → INS → SEG → CON → SYN → ALT → SUP → REC
 ∅     ≝     ⊕     ⊢     ⋈     ⊗     ⇌     ⧦     ↻
```

This ordering matters. You can't meaningfully connect things (CON) that haven't been instantiated (INS). You can't merge things (SYN) that haven't been connected. You can't detect that something is in superposition (SUP) without first observing alternation (ALT). The cascade is a dependency chain, not an arbitrary listing.

---

## Identity Triad

The Identity triad answers: **what exists and what doesn't?**

### NUL — Destruction / Absence

NUL is the only operator that truly destroys. When you NUL an entity, it's gone. When you NUL a field, it's been explicitly erased — not missing, not unknown, but actively removed.

This matters because every other kind of "missing" in the system has a different meaning (see Absence Types below). NUL is unambiguous: the thing was here, and now it isn't.

NUL also kills all CON edges touching the destroyed entity. If you destroy a person, their relationships die with them.

**Entity-level:** marks entity as dead, kills its edges. **Field-level:** stores `{"_nul": true}` on the field. The field was observed and then destroyed — distinct from never having been observed at all.

### DES — Designation / Naming

DES frames how something is seen. It doesn't create the entity or change its data — it applies an interpretive lens. "This person is the Community Lead" is a DES. "I'm querying for all open venues" is also a DES — you're designating your attention.

DES is the operator most likely to be misused. Writing a value into a field is not designation — that's INS or ALT. Designation is the act of framing, naming, categorizing. It lives in a `_des` namespace on the entity, separate from the entity's data fields, because designations are about interpretation, not observation.

In EO, DES precedes INS. The frame comes before the fact. You designate a way of seeing, and then particulars become visible within it. This is why DES also serves as the query operator — every query is a designation of relevance.

**A DES without a frame is suspect.** "This person is the Community Lead" is incomplete without "...according to whom? under what authority? in what context?" A DES that hides its frame is hiding an assumption.

### INS — Instantiation

INS brings something into existence. It's the first observation of a particular entity. All other fields in the target become the entity's projected data.

If you INS an entity that already exists, the new fields merge into the existing data without replacing it. This is intentional — INS is "I observed these things," not "overwrite everything." Multiple observations accumulate.

INS is the emergence operator of the Identity triad. NUL establishes the ground (what's absent), DES introduces the frame (how to see), INS produces the particular (what's actually there).

---

## Structure Triad

The Structure triad answers: **how do things relate?**

### SEG — Segmentation / Boundary

SEG records that an entity belongs to a scope, district, category, or filter boundary. "This venue is in the downtown district" is a SEG. "This record falls within the 2025 audit scope" is a SEG.

SEG doesn't move or change the entity. It tags it with boundary metadata, stored in a `_seg` array. At query time, boundaries become filters — you can ask for all entities within a boundary.

### CON — Connection / Joining

CON creates a relationship between two entities. Every CON edge carries a **dialectical stance** that describes the nature of the connection — not how strong it is, but what kind of tension it embodies.

CON sits between SEG (separation) and SYN (fusion) in the Structure triad. Its internal dialectic is about the tension within a connection itself:

- **Accidental**: The connection is contingent. "Person frequents venue." It could have been a different venue. The connection is real — it happened — but it doesn't change what either thing is. Remove it and both entities are fully coherent on their own.
- **Essential**: The connection is necessary. "Expenditure funded by grant." "Defendant named in case." The case can't exist without the defendant. The expenditure is structurally incomplete without the grant. Remove the connection and one or both entities lose their identity.
- **Generative**: The connection produces something neither entity has alone. "Co-investigators on a story." Neither person is incomplete without the other (not essential), but the connection isn't incidental either (not accidental). It's productive — it generates a capacity, a pattern, a third thing that only exists because of the relation.

The stance isn't decoration. It affects graph traversal queries — you can filter to only follow essential connections, or exclude accidental ones — and it reveals the difference between contingent association, structural dependence, and productive relation. It's a dialectical position, not a number on a scale.

CON edges are directional (source → target) but traversal queries follow them in both directions.

Two observers can CON the same pair of entities with different stances under different frames. The budget office observes an essential connection between an expenditure and a grant. An auditor observes an accidental one — the expenditure could have been funded by a different grant. Both observations go in the log. Neither is more true than the other. The log is not pretending to be the truth about things; it's a log of observations.

CON applies at every scale. A CON between two specific people is an instance-level relationship. A CON between two entity types (stored as entities in a `_types` table) is a structural assertion that this kind of relationship can exist. Both are the same operator on different targets. There's no separate "schema level" — there's just CON.

### SYN — Synthesis / Merging

SYN merges entity B into entity A. B dies. A absorbs B's data (A's fields win on conflict). All of B's CON edges are reassigned to A. A carries a `_syn_from` array tracking its merge history.

SYN is the emergence operator of the Structure triad. SEG establishes boundaries, CON introduces connections, SYN produces wholes from parts.

SYN is irreversible at the projection level (B is dead), but fully reversible at the log level (replay without the SYN operation and B comes back). This is the general pattern: projection is disposable, the log is truth.

---

## Time Triad

The Time triad answers: **how do things change?**

### ALT — Alternation / Transition

ALT updates specific fields on an existing entity. Only the fields present in the target are changed; everything else is untouched. This is the workhorse operator for state transitions: status changes, field updates, corrections.

If the entity doesn't exist, ALT is a no-op. This is intentional. Operations are facts about what happened, and the log records them even if the target doesn't exist yet. The projection simply ignores what it can't apply.

### SUP — Superposition / Layering

SUP stores multiple simultaneous values for a single field. This is for genuine ambiguity — two sources disagree about a capacity number, two agencies report different statuses, a field has a value from the old system and a different value from the new system.

The field's value becomes `{"_sup": [variant1, variant2, ...]}`. Each variant carries a source label and a value. When you query, filters match against any variant.

SUP is not versioning (that's what the log is for — every ALT is already preserved). SUP is for values that are contradictory right now, where the contradiction itself is meaningful data.

### REC — Reconfiguration / Recursion

REC is the most powerful and most misunderstood operator. It's the moment a derived structure enters the system's feedback loop — when history begins to condition future operations.

REC has three modes in Choreo:

**Snapshot ingest** (`context.type: "snapshot_ingest"`): Your scraper collects raw state. Choreo diffs it against the current projection and generates granular INS/ALT/NUL operations. The interpretive assumptions — what does it mean when a row is missing? what does it mean when a field is null? — are data in the frame, not code in your scraper.

**Feedback rules** (`context.type: "feedback_rule"`): The system watches its own output and generates new operations in response. "When a snapshot flags an entity as absent, automatically mark it for investigation." The rule itself is an entity in the log — auditable, modifiable, destroyable with NUL.

**Emergence detection** (`context.type: "emergence_scan"`): The system scans its own CON graph for unnamed clusters of essentially or generatively connected entities and emits DES operations designating them. Structure that exists in the graph but nobody has named yet gets surfaced automatically.

REC is the emergence operator of the Time triad. ALT establishes change, SUP introduces ambiguity, REC produces self-reference. The system operating on its own output.

---

## Absence Types

Every operator implies its own form of absence. This is philosophically important and practically useful — the system can distinguish *why* something is missing.

| Absence | Negated Operator | What It Means |
|---------|-----------------|---------------|
| Unknown | ¬INS | Never observed. No record exists. Go look. |
| Undesignated | ¬DES | Exists but hasn't been named or categorized. Needs framing. |
| Inapplicable | ¬SEG | Outside the current boundary. Not in scope. |
| Unconnected | ¬CON | No relationships. Structurally isolated. |
| Unfused | ¬SYN | Components present but not yet merged. Awaiting synthesis. |
| Pending | ¬ALT | Between states. Transition hasn't happened yet. |
| Withheld | ¬SUP | Multiple possibilities being preserved. Uncollapsed. |
| Pre-recursive | ¬REC | Not yet part of a feedback loop. Pattern is descriptive, not active. |
| Destroyed | NUL | Actually gone. True void. |

Only NUL represents true destruction. All other absences are inferred from which operators haven't been applied. An entity with no CON edges is "unconnected" — that's a finding, not an error. An entity with no DES is "undesignated" — it exists but nobody has framed what it means yet.

---

## The Operation Grammar

Every operation follows the same structure:

```
operator(target, context, frame)
```

- **target**: what is being operated on. Always JSON. Usually contains an `id` field.
- **context**: where and how the operation occurs. Contains `table` (which projection table), and optionally `type`, `source`, and other metadata.
- **frame**: the interpretive lens. Why this operation matters, under whose authority, with what confidence. Optional on most operations, but a DES without a frame is hiding an assumption.

The frame is not decoration. It modifies how the operation affects projection. An INS with `frame.epistemic: "given"` (observed fact) carries different provenance than an INS with `frame.epistemic: "meant"` (human interpretation) or `frame.epistemic: "derived"` (computed value).

---

## Why Nine?

The nine operators are organized as three triads across three domains (Identity, Structure, Time). Each triad has a ground operator, a structuring operator, and an emergence operator. This gives 3 × 3 = 9.

The deeper reason: in Emergent Ontology, these three domains are the minimum required for meaningful distinction. Identity without structure is a list. Structure without time is a photograph. Time without identity is noise. The nine operators are the minimum vocabulary for describing transformation across all three domains simultaneously.

Adding a tenth operator would either duplicate an existing one (every "new" verb you think of maps to one of the nine) or violate the triadic structure (which would mean one domain has more resolution than the others, which would mean the framework privileges that domain).
