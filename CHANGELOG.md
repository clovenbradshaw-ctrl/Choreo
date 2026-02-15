# Changelog

## 2.1 — CON Stance & Documentation Consolidation

### Runtime: CON Stance Support

The dialectical stance system replaces bare coupling floats as the primary CON edge semantics.

**Schema change:** `_con_edges` table gains a `stance TEXT` column with values `accidental`, `essential`, or `generative`. The `coupling` float is retained for backward compatibility.

**CON projection:** When a CON operation includes `target.stance`, it's stored directly. When stance is absent, it's inferred from coupling (≥0.7 → essential, ≥0.55 → generative, else accidental). This preserves backward compatibility with existing operations that only specify coupling.

**EOQL traversal:** `>> CON()` now supports stance-based filtering:
```
state(target.id="pl0") >> CON(hops=2, stance="essential")
state(target.id="pl0") >> CON(hops=2, exclude="accidental")
```
Both `stance` and `exclude` can be combined with `min_coupling` for hybrid queries.

**Emergence scan:** Accepts `min_stance` parameter to filter cluster detection by edge stance.

**Migration:** Existing instances need `POST /instances/{slug}/rebuild` or DB recreation to get the new schema. Apply via `python3 choreo_stance_patch.py choreo.py`.

### Documentation

New and updated docs in `docs/`:

| File | What it covers |
|------|---------------|
| `OPERATORS.md` | Complete operator reference. Nine operators, three triads, absence types, operation grammar. |
| `DESIGN.md` | Design decisions. One log, leniency, synchronous projection, why SQLite, bootstrapping axioms. CON stance dialectic explained. |
| `EOQL.md` | Query language reference. `state()`, `stream()`, `>> CON()` with stance/exclude, `meta()`, replay profiles, audit trail. |
| `THEORY.md` | Theoretical treatment (v2). Graph traversal as operator pipelines, read/write collapse, differentiator test, open formal work. Updated with Choreo cross-references throughout. |
| `FRACTAL.md` | Fractal disclosure. Self-similar triadic structure, six journalistic questions, investigative matrix, vocabulary frames. |
| `EVENT_TYPES.md` | Event type explosion. Maps ~200 domain-specific event types to nine operators. |

### Key Alignments

- DESIGN.md now describes CON stance as dialectic (accidental/essential/generative), not a numeric coupling float
- EOQL.md documents stance-based traversal as the primary query mechanism
- Runtime supports both stance names and coupling floats for backward compatibility
- Demo seed data carries explicit stances on all CON edges
- THEORY.md v2 addresses all critiques from independent reviews (softened absolute claims, defended DES/SUP primitiveness, added differentiator test, marked open formal work)
