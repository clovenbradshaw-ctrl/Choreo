# Choreo

EO-native event store. Nine operators, append-only log, derived projections.

```
POST /{instance}/operations  — the one way in
GET  /{instance}/stream      — the one way out (SSE)
```

## Quick Start

```bash
pip install flask
python3 choreo.py --port 8420
# → http://localhost:8420/ui
```

Seed demo data: `POST http://localhost:8420/demo/seed`

## The Nine Operators

| Triad | Operators | Domain |
|-------|-----------|--------|
| Identity | NUL → DES → INS | What exists and what doesn't |
| Structure | SEG → CON → SYN | How things relate |
| Time | ALT → SUP → REC | How things change |

Every mutation is one of these nine. Every query is a DES. Schema is data. The log is truth; projection is convenience.

## Documentation

| Doc | What it covers |
|-----|---------------|
| [docs/OPERATORS.md](docs/OPERATORS.md) | Complete operator reference |
| [docs/DESIGN.md](docs/DESIGN.md) | Why Choreo works the way it does |
| [docs/EOQL.md](docs/EOQL.md) | Query language reference |
| [docs/THEORY.md](docs/THEORY.md) | Theoretical foundations (v2) |
| [docs/FRACTAL.md](docs/FRACTAL.md) | Self-similar structure of the framework |
| [docs/EVENT_TYPES.md](docs/EVENT_TYPES.md) | Nine operators replace 200+ event types |
| [developer_guide.md](developer_guide.md) | API reference and integration patterns |

## Key Concepts

**One log.** The `operations` table is append-only, immutable, and authoritative. Everything else is derived.

**CON stances.** Connections carry a dialectical stance — accidental, essential, or generative — not just a coupling number. `>> CON(hops=2, stance="essential")` asks about the *nature* of dependencies.

**Nine types of absence.** Every operator implies its own form of missing. `gaps/{table}` tells you which entities have never been designated, never been connected, have contradictions, or have never been validated.

**Queries are operations.** Every EOQL query is a DES (designation of attention). With `audit: true`, queries are logged alongside mutations in the same operations table.

## Production

```bash
python3 choreo.py --nginx choreo.yourdomain.com  # generates nginx config
pm2 start choreo.py --interpreter python3 -- --port 8420
```
