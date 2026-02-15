#!/usr/bin/env python3
"""
choreo_stance_patch.py — Adds CON stance support to Choreo Runtime

Run: python3 choreo_stance_patch.py /path/to/choreo.py
Produces: choreo.py with stance support added

Changes:
  1. _con_edges schema: adds `stance TEXT DEFAULT 'accidental'`
  2. CON projection: extracts and stores stance from target
  3. CON chain traversal: supports stance/exclude filters
  4. EOQL parser: parses stance/exclude params in >> CON()
  5. Emergence scan: filters by stance
  6. Demo seed: uses stances instead of bare coupling floats
"""

import re, sys, shutil

def patch(src: str) -> str:
    # ---------- 1. Schema: add stance column to _con_edges ----------
    src = src.replace(
        """        -- CON adjacency index
        CREATE TABLE IF NOT EXISTS _con_edges (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            coupling  REAL DEFAULT 0,
            data      TEXT DEFAULT '{}',
            op_id     INTEGER NOT NULL,
            alive     INTEGER NOT NULL DEFAULT 1
        );""",
        """        -- CON adjacency index
        CREATE TABLE IF NOT EXISTS _con_edges (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            stance    TEXT DEFAULT 'accidental' CHECK(stance IN('accidental','essential','generative')),
            coupling  REAL DEFAULT 0,
            data      TEXT DEFAULT '{}',
            op_id     INTEGER NOT NULL,
            alive     INTEGER NOT NULL DEFAULT 1
        );"""
    )

    # ---------- 2. CON projection: extract stance ----------
    src = src.replace(
        """    elif op == "CON":
        # Create a connection between entities
        src = target.get("source") or target.get("from")
        tgt = target.get("target") or target.get("to")
        coupling = target.get("coupling", 0.5)
        con_data = {k: v for k, v in target.items()
                    if k not in ("source", "target", "from", "to", "coupling", "id")}
        if src and tgt:
            db.execute(
                "INSERT INTO _con_edges(source_id, target_id, coupling, data, op_id, alive) "
                "VALUES(?,?,?,?,?,1)",
                (src, tgt, coupling, json.dumps(con_data), op_id)
            )""",
        """    elif op == "CON":
        # Create a connection between entities
        src = target.get("source") or target.get("from")
        tgt = target.get("target") or target.get("to")
        coupling = target.get("coupling", 0.5)
        # Stance: dialectical position (accidental/essential/generative)
        # Infer from coupling if stance not explicit (backward compat)
        stance = target.get("stance", None)
        if stance is None:
            if coupling >= 0.7:
                stance = "essential"
            elif coupling >= 0.4:
                stance = "generative" if coupling >= 0.55 else "accidental"
            else:
                stance = "accidental"
        con_data = {k: v for k, v in target.items()
                    if k not in ("source", "target", "from", "to", "coupling", "stance", "id")}
        if src and tgt:
            db.execute(
                "INSERT INTO _con_edges(source_id, target_id, stance, coupling, data, op_id, alive) "
                "VALUES(?,?,?,?,?,?,1)",
                (src, tgt, stance, coupling, json.dumps(con_data), op_id)
            )"""
    )

    # ---------- 3. CON chain: stance/exclude filters ----------
    src = src.replace(
        '''def _exec_con_chain(db: sqlite3.Connection, base_result: dict, chain: dict) -> dict:
    """BFS over _con_edges from base result entities."""
    hops = chain.get("hops", 1)
    min_coupling = chain.get("min_coupling", 0)

    seed_ids = {e["id"] for e in base_result.get("entities", [])}
    visited = set(seed_ids)
    frontier = set(seed_ids)
    edges_found = []

    for hop in range(hops):
        next_frontier = set()
        for eid in frontier:
            rows = db.execute(
                "SELECT source_id, target_id, coupling, data FROM _con_edges "
                "WHERE (source_id=? OR target_id=?) AND alive=1 AND coupling>=?",
                (eid, eid, min_coupling)
            ).fetchall()
            for row in rows:
                other = row["target_id"] if row["source_id"] == eid else row["source_id"]
                edges_found.append({
                    "source": row["source_id"], "target": row["target_id"],
                    "coupling": row["coupling"], "data": json.loads(row["data"])
                })
                if other not in visited:
                    visited.add(other)
                    next_frontier.add(other)
        frontier = next_frontier''',
        '''def _exec_con_chain(db: sqlite3.Connection, base_result: dict, chain: dict) -> dict:
    """BFS over _con_edges from base result entities."""
    hops = chain.get("hops", 1)
    min_coupling = chain.get("min_coupling", 0)
    stance_filter = chain.get("stance")       # only follow this stance
    exclude_filter = chain.get("exclude")     # exclude this stance

    seed_ids = {e["id"] for e in base_result.get("entities", [])}
    visited = set(seed_ids)
    frontier = set(seed_ids)
    edges_found = []

    for hop in range(hops):
        next_frontier = set()
        for eid in frontier:
            rows = db.execute(
                "SELECT source_id, target_id, stance, coupling, data FROM _con_edges "
                "WHERE (source_id=? OR target_id=?) AND alive=1 AND coupling>=?",
                (eid, eid, min_coupling)
            ).fetchall()
            for row in rows:
                # Stance filtering
                edge_stance = row["stance"] or "accidental"
                if stance_filter and edge_stance != stance_filter:
                    continue
                if exclude_filter and edge_stance == exclude_filter:
                    continue
                other = row["target_id"] if row["source_id"] == eid else row["source_id"]
                edges_found.append({
                    "source": row["source_id"], "target": row["target_id"],
                    "stance": edge_stance, "coupling": row["coupling"],
                    "data": json.loads(row["data"])
                })
                if other not in visited:
                    visited.add(other)
                    next_frontier.add(other)
        frontier = next_frontier'''
    )

    # ---------- 4. EOQL parser: stance/exclude in CON chain ----------
    src = src.replace(
        '''        base["chain"] = {
            "type": "CON",
            "hops": int(con_params.get("hops", 1)),
            "min_coupling": float(con_params.get("min_coupling", 0))
        }''',
        '''        base["chain"] = {
            "type": "CON",
            "hops": int(con_params.get("hops", 1)),
            "min_coupling": float(con_params.get("min_coupling", 0)),
        }
        if "stance" in con_params:
            base["chain"]["stance"] = con_params["stance"]
        if "exclude" in con_params:
            base["chain"]["exclude"] = con_params["exclude"]'''
    )

    # ---------- 5. CON chain result: include stance in metadata ----------
    src = src.replace(
        '''    base_result["con_chain"] = {
        "hops": hops, "min_coupling": min_coupling,
        "reached": reached_entities,
        "edges": edges_found,
        "reached_count": len(reached_entities)
    }''',
        '''    chain_meta = {"hops": hops, "min_coupling": min_coupling,
        "reached": reached_entities, "edges": edges_found,
        "reached_count": len(reached_entities)}
    if stance_filter:
        chain_meta["stance"] = stance_filter
    if exclude_filter:
        chain_meta["exclude"] = exclude_filter
    base_result["con_chain"] = chain_meta'''
    )

    # ---------- 6. Emergence scan: filter by stance ----------
    src = src.replace(
        '''    edges = db.execute(
        "SELECT source_id, target_id, coupling FROM _con_edges WHERE alive=1 AND coupling>=?",
        (min_coupling,)
    ).fetchall()''',
        '''    min_stance = target.get("min_stance", None)  # accidental, essential, or generative
    stance_sql = ""
    params = [min_coupling]
    if min_stance:
        if min_stance == "essential":
            stance_sql = " AND stance IN('essential','generative')"
        elif min_stance == "generative":
            stance_sql = " AND stance='generative'"
    edges = db.execute(
        f"SELECT source_id, target_id, coupling FROM _con_edges WHERE alive=1 AND coupling>=?{stance_sql}",
        params
    ).fetchall()'''
    )

    # ---------- 7. Demo seed: add stances ----------
    # Replace a few key CON entries to show stances
    src = src.replace(
        '{"ts": "2025-01-05T12:00:00Z", "op": "CON", "target": {"source": "pe0", "target": "pe1", "coupling": 0.9, "type": "partners"}, "context": {"table": "people"}, "frame": {}},',
        '{"ts": "2025-01-05T12:00:00Z", "op": "CON", "target": {"source": "pe0", "target": "pe1", "coupling": 0.9, "stance": "essential", "type": "partners"}, "context": {"table": "people"}, "frame": {}},'
    )
    src = src.replace(
        '{"ts": "2025-01-20T12:00:00Z", "op": "CON", "target": {"source": "pe0", "target": "pl0", "coupling": 0.8, "type": "performs_at"}, "context": {"table": "cross"}, "frame": {}},',
        '{"ts": "2025-01-20T12:00:00Z", "op": "CON", "target": {"source": "pe0", "target": "pl0", "coupling": 0.8, "stance": "generative", "type": "performs_at"}, "context": {"table": "cross"}, "frame": {}},'
    )
    src = src.replace(
        '{"ts": "2025-01-20T12:01:00Z", "op": "CON", "target": {"source": "pe1", "target": "pl1", "coupling": 0.85, "type": "works_at"}, "context": {"table": "cross"}, "frame": {}},',
        '{"ts": "2025-01-20T12:01:00Z", "op": "CON", "target": {"source": "pe1", "target": "pl1", "coupling": 0.85, "stance": "essential", "type": "works_at"}, "context": {"table": "cross"}, "frame": {}},'
    )
    src = src.replace(
        '{"ts": "2025-01-25T20:00:00Z", "op": "CON", "target": {"source": "pe2", "target": "pl3", "coupling": 0.5, "type": "frequents"}, "context": {"table": "cross"}, "frame": {}},',
        '{"ts": "2025-01-25T20:00:00Z", "op": "CON", "target": {"source": "pe2", "target": "pl3", "coupling": 0.5, "stance": "accidental", "type": "frequents"}, "context": {"table": "cross"}, "frame": {}},'
    )
    src = src.replace(
        '{"ts": "2025-01-25T20:01:00Z", "op": "CON", "target": {"source": "pe3", "target": "pl4", "coupling": 0.7, "type": "works_at"}, "context": {"table": "cross"}, "frame": {}},',
        '{"ts": "2025-01-25T20:01:00Z", "op": "CON", "target": {"source": "pe3", "target": "pl4", "coupling": 0.7, "stance": "essential", "type": "works_at"}, "context": {"table": "cross"}, "frame": {}},'
    )
    src = src.replace(
        '{"ts": "2025-02-15T10:00:00Z", "op": "CON", "target": {"source": "pe5", "target": "pe3", "coupling": 0.4, "type": "knows"}, "context": {"table": "cross"}, "frame": {}},',
        '{"ts": "2025-02-15T10:00:00Z", "op": "CON", "target": {"source": "pe5", "target": "pe3", "coupling": 0.4, "stance": "accidental", "type": "knows"}, "context": {"table": "cross"}, "frame": {}},'
    )
    src = src.replace(
        '{"ts": "2025-02-20T10:00:00Z", "op": "CON", "target": {"source": "pe6", "target": "pe1", "coupling": 0.55, "type": "mentored_by"}, "context": {"table": "cross"}, "frame": {}},',
        '{"ts": "2025-02-20T10:00:00Z", "op": "CON", "target": {"source": "pe6", "target": "pe1", "coupling": 0.55, "stance": "generative", "type": "mentored_by"}, "context": {"table": "cross"}, "frame": {}},'
    )
    src = src.replace(
        '{"ts": "2025-03-10T10:00:00Z", "op": "CON", "target": {"source": "ev0", "target": "pl0", "coupling": 0.8, "type": "hosted_at"}, "context": {"table": "cross"}, "frame": {}},',
        '{"ts": "2025-03-10T10:00:00Z", "op": "CON", "target": {"source": "ev0", "target": "pl0", "coupling": 0.8, "stance": "essential", "type": "hosted_at"}, "context": {"table": "cross"}, "frame": {}},'
    )

    return src


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 choreo_stance_patch.py /path/to/choreo.py")
        print("       Outputs patched file to choreo.py (backs up original to choreo.py.bak)")
        sys.exit(1)

    path = sys.argv[1]
    with open(path, "r") as f:
        original = f.read()

    shutil.copy2(path, path + ".bak")
    patched = patch(original)

    with open(path, "w") as f:
        f.write(patched)

    # Count changes
    import difflib
    diff = list(difflib.unified_diff(original.splitlines(), patched.splitlines(), lineterm=""))
    additions = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    deletions = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    print(f"Patched {path}: +{additions} -{deletions} lines")
    print(f"Backup: {path}.bak")
    print()
    print("Changes applied:")
    print("  1. _con_edges schema: added stance column (accidental/essential/generative)")
    print("  2. CON projection: extracts stance from target, infers from coupling if absent")
    print("  3. CON chain traversal: supports stance= and exclude= filters")
    print("  4. EOQL parser: parses stance/exclude in >> CON(...)")
    print("  5. Emergence scan: supports min_stance filter")
    print("  6. Demo seed: CON edges now carry explicit stances")
    print()
    print("NOTE: Existing instances need rebuild (POST /instances/{slug}/rebuild)")
    print("      or recreate the DB to get the new schema.")
