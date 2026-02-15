# Developer Guide: Stance Update (v2.1 Addendum)

This addendum documents the CON stance system. Append to or update your existing `developer_guide.md`.

---

## CON Stance (replaces coupling-only semantics)

Every CON edge now carries a **stance** — a dialectical position describing the *nature* of the connection:

| Stance | Meaning | Example |
|--------|---------|---------|
| `accidental` | Contingent. Both entities are fully coherent without it. | "person frequents venue" |
| `essential` | Necessary. One or both entities are structurally incomplete without it. | "defendant named in case" |
| `generative` | Productive. Creates a capacity neither entity has alone. | "co-investigators on a story" |

### Creating CON with stance

```json
{
  "op": "CON",
  "target": {
    "source": "pe0",
    "target": "pl0",
    "stance": "essential",
    "coupling": 0.8,
    "type": "works_at"
  },
  "context": {"table": "cross"}
}
```

- `stance` is the primary semantic field (accidental/essential/generative)
- `coupling` (0.0–1.0) is retained for backward compatibility
- If `stance` is omitted, it's inferred from coupling: ≥0.7 → essential, ≥0.55 → generative, else accidental

### Querying by stance

```
state(target.id="pl0") >> CON(hops=2, stance="essential")
state(target.id="pl0") >> CON(hops=2, exclude="accidental")
```

- `stance="essential"` — only follow edges with this stance
- `exclude="accidental"` — follow all edges except this stance
- Both can combine with `min_coupling` for hybrid queries

### Edge response format (updated)

```json
{
  "source": "pl0",
  "target": "pe0",
  "stance": "essential",
  "coupling": 0.8,
  "data": {"type": "works_at"}
}
```

### Emergence scan with stance

```json
{
  "op": "REC",
  "target": {
    "algorithm": "connected_components",
    "min_cluster_size": 3,
    "min_stance": "essential"
  },
  "context": {"type": "emergence_scan"}
}
```

`min_stance` filters edges: `"essential"` includes essential + generative; `"generative"` includes only generative.

### Migration

Existing instances need `POST /instances/{slug}/rebuild` after applying the stance patch to get the new `_con_edges` schema. Existing CON edges without explicit stances will have stance inferred from their coupling value during rebuild.
