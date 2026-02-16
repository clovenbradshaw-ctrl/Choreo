# Choreo

EO-native event store. Nine operators, append-only log, derived projections.

```
POST /{instance}/operations  — the one way in
GET  /{instance}/stream      — the one way out (SSE)
```

## Quick Start

```bash
git clone https://github.com/clovenbradshaw-ctrl/Choreo.git
cd Choreo
pip install -r requirements.txt
python3 choreo_runtime.py --port 8420
# → http://localhost:8420/ui
```

Or with Docker:

```bash
git clone https://github.com/clovenbradshaw-ctrl/Choreo.git
cd Choreo
docker compose up -d
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
| [OPERATORS.md](OPERATORS.md) | Complete operator reference |
| [DESIGN.md](DESIGN.md) | Why Choreo works the way it does |
| [EOQL.md](EOQL.md) | Query language reference |
| [THEORY.md](THEORY.md) | Theoretical foundations (v2) |
| [FRACTAL.md](FRACTAL.md) | Self-similar structure of the framework |
| [EVENT_TYPES.md](EVENT_TYPES.md) | Nine operators replace 200+ event types |
| [developer_guide.md](developer_guide.md) | API reference and integration patterns |
| [DEPLOY.md](DEPLOY.md) | Production deployment, SSL, nginx, Docker, PM2, webhooks |

## Key Concepts

**One log.** The `operations` table is append-only, immutable, and authoritative. Everything else is derived.

**CON stances.** Connections carry a dialectical stance — accidental, essential, or generative — not just a coupling number. `>> CON(hops=2, stance="essential")` asks about the *nature* of dependencies.

**Nine types of absence.** Every operator implies its own form of missing. `gaps/{table}` tells you which entities have never been designated, never been connected, have contradictions, or have never been validated.

**Queries are operations.** Every EOQL query is a DES (designation of attention). With `audit: true`, queries are logged alongside mutations in the same operations table.

## Production

See [DEPLOY.md](DEPLOY.md) for the full deployment guide covering nginx, SSL, PM2, Docker, webhooks, and n8n integration.

```bash
# Quick start with PM2
pm2 start ecosystem.config.js
pm2 save && pm2 startup

# Or with Docker
docker compose up -d
```
