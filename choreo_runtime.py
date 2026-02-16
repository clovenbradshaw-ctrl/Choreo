#!/usr/bin/env python3
"""
Choreo Runtime 2.1
EO-native event store with projection engine, EOQL queries, CON stance, and SSE streaming.

Two endpoints per instance:
  POST /{instance}/operations  — the one way in
  GET  /{instance}/stream      — the one way out (SSE)

Plus instance management:
  GET    /instances
  POST   /instances
  DELETE /instances/{slug}
  POST   /instances/{slug}/seed

Run: python choreo_runtime.py [--port 8420] [--dir ./instances]
"""

import sqlite3, json, os, re, time, sys, threading, hashlib
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify, Response
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
try:
    import requests as _requests
except ImportError:
    _requests = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
INSTANCE_DIR = Path(os.environ.get("CHOREO_DIR", "./instances"))
SNAPSHOT_INTERVAL = int(os.environ.get("CHOREO_SNAPSHOT_INTERVAL", "1000"))
PORT = int(os.environ.get("CHOREO_PORT", "8420"))

# ---------------------------------------------------------------------------
# Outbound Webhooks
# ---------------------------------------------------------------------------
_webhook_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook")
_webhooks: dict[str, list[dict]] = {}  # instance -> [{url, filter?, active}]

def _load_webhooks():
    """Load webhook config from webhooks.json in instance dir."""
    global _webhooks
    wh_path = INSTANCE_DIR / "webhooks.json"
    if wh_path.exists():
        try:
            _webhooks = json.loads(wh_path.read_text())
        except Exception:
            _webhooks = {}

def _save_webhooks():
    wh_path = INSTANCE_DIR / "webhooks.json"
    wh_path.write_text(json.dumps(_webhooks, indent=2))

def fire_webhooks(instance: str, op_data: dict):
    """Fire outbound webhooks for an operation (non-blocking)."""
    if not _requests:
        return
    hooks = _webhooks.get(instance, [])
    for hook in hooks:
        if not hook.get("active", True):
            continue
        op_filter = hook.get("filter")
        if op_filter and op_data.get("op") not in op_filter:
            continue
        _webhook_pool.submit(_send_webhook, hook["url"], op_data, instance)

def _send_webhook(url: str, data: dict, instance: str):
    try:
        _requests.post(url, json=data, headers={
            "Content-Type": "application/json",
            "X-Choreo-Instance": instance,
        }, timeout=10)
    except Exception as e:
        print(f"[webhook] POST {url} failed: {e}", file=sys.stderr)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp

@app.route("/<path:p>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def cors_preflight(p=""):
    return "", 204

# ---------------------------------------------------------------------------
# SQLite connection pool (one per instance, thread-local)
# ---------------------------------------------------------------------------
_local = threading.local()

def get_db(instance: str) -> sqlite3.Connection:
    """Get or create a SQLite connection for this instance in this thread."""
    key = f"db_{instance}"
    db = getattr(_local, key, None)
    if db is None:
        db_path = INSTANCE_DIR / f"{instance}.db"
        if not db_path.exists():
            return None
        db = sqlite3.connect(str(db_path), check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        setattr(_local, key, db)
    return db


def init_db(instance: str) -> sqlite3.Connection:
    """Create a new instance database with full schema."""
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    db_path = INSTANCE_DIR / f"{instance}.db"
    db = sqlite3.connect(str(db_path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")

    db.executescript("""
        -- The source of truth: append-only operations log
        CREATE TABLE IF NOT EXISTS operations (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            op      TEXT NOT NULL CHECK(op IN('INS','DES','SEG','CON','SYN','ALT','SUP','REC','NUL')),
            target  TEXT NOT NULL DEFAULT '{}',
            context TEXT NOT NULL DEFAULT '{}',
            frame   TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_ops_ts ON operations(ts);
        CREATE INDEX IF NOT EXISTS idx_ops_op ON operations(op);

        -- Materialized projection: derived, disposable, rebuildable
        CREATE TABLE IF NOT EXISTS _projected (
            entity_id TEXT NOT NULL,
            tbl       TEXT NOT NULL,
            data      TEXT NOT NULL DEFAULT '{}',
            alive     INTEGER NOT NULL DEFAULT 1,
            last_op   INTEGER NOT NULL,
            PRIMARY KEY(entity_id, tbl)
        );
        CREATE INDEX IF NOT EXISTS idx_proj_tbl ON _projected(tbl) WHERE alive=1;

        -- CON adjacency index
        CREATE TABLE IF NOT EXISTS _con_edges (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            stance    TEXT DEFAULT 'accidental' CHECK(stance IN('accidental','essential','generative')),
            coupling  REAL DEFAULT 0,
            data      TEXT DEFAULT '{}',
            op_id     INTEGER NOT NULL,
            alive     INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_con_src ON _con_edges(source_id) WHERE alive=1;
        CREATE INDEX IF NOT EXISTS idx_con_tgt ON _con_edges(target_id) WHERE alive=1;

        -- Time-travel snapshots (periodic checkpoints of projected state)
        CREATE TABLE IF NOT EXISTS _snapshots (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            op_id INTEGER NOT NULL,
            ts    TEXT NOT NULL,
            tbl   TEXT NOT NULL,
            data  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snap_tbl ON _snapshots(tbl, op_id);

        -- Runtime metadata
        CREATE TABLE IF NOT EXISTS _meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)

    # Initialize watermark
    db.execute(
        "INSERT OR IGNORE INTO _meta(key, value) VALUES('projection_watermark', '0')"
    )
    db.execute(
        "INSERT OR IGNORE INTO _meta(key, value) VALUES('snapshot_interval', ?)",
        (str(SNAPSHOT_INTERVAL),)
    )
    db.commit()

    setattr(_local, f"db_{instance}", db)
    return db


# ---------------------------------------------------------------------------
# Projection Engine
# ---------------------------------------------------------------------------
# The developmental cascade (Update 9)
OPERATOR_CASCADE = ['NUL', 'DES', 'INS', 'SEG', 'CON', 'SYN', 'ALT', 'SUP', 'REC']
OPERATOR_TRIADS = {
    'Identity': ['NUL', 'DES', 'INS'],
    'Structure': ['SEG', 'CON', 'SYN'],
    'Time': ['ALT', 'SUP', 'REC'],
}
VALID_OPS = set(OPERATOR_CASCADE)


def _write_provenance(data: dict, target: dict, frame: dict, op_id: int):
    """Write frame provenance metadata on projected fields (Update 1a).
    Only writes provenance when frame contains epistemic/source/authority keys."""
    if not (frame.get("epistemic") or frame.get("source") or frame.get("authority")):
        return
    prov = data.get("_provenance", {})
    for k in target:
        if k == "id":
            continue
        entry = {"op_id": op_id}
        if frame.get("epistemic"):
            if isinstance(frame["epistemic"], dict):
                entry["epistemic"] = frame["epistemic"].get(k, "unknown")
            else:
                entry["epistemic"] = frame["epistemic"]
        if frame.get("source"):
            entry["source"] = frame["source"]
        if frame.get("authority"):
            entry["authority"] = frame["authority"]
        prov[k] = entry
    data["_provenance"] = prov


def project_op(db: sqlite3.Connection, op_id: int, op: str,
               target: dict, context: dict, frame: dict):
    """
    Process a single operation and update projection tables.
    This is called synchronously on every write.
    """
    tbl = context.get("table", "_default")
    eid = target.get("id")

    if op == "INS":
        # Create or update entity in projection
        if not eid:
            return
        existing = db.execute(
            "SELECT data FROM _projected WHERE entity_id=? AND tbl=?",
            (eid, tbl)
        ).fetchone()
        if existing:
            # Merge new fields into existing data
            data = json.loads(existing["data"])
            for k, v in target.items():
                if k != "id":
                    data[k] = v
            # Frame provenance (Update 1a)
            _write_provenance(data, target, frame, op_id)
            db.execute(
                "UPDATE _projected SET data=?, alive=1, last_op=? WHERE entity_id=? AND tbl=?",
                (json.dumps(data), op_id, eid, tbl)
            )
        else:
            data = {k: v for k, v in target.items() if k != "id"}
            # Frame provenance (Update 1a)
            _write_provenance(data, target, frame, op_id)
            db.execute(
                "INSERT INTO _projected(entity_id, tbl, data, alive, last_op) VALUES(?,?,?,1,?)",
                (eid, tbl, json.dumps(data), op_id)
            )

    elif op == "ALT":
        # Update specific fields on existing entity
        if not eid:
            return
        existing = db.execute(
            "SELECT data FROM _projected WHERE entity_id=? AND tbl=?",
            (eid, tbl)
        ).fetchone()
        if existing:
            data = json.loads(existing["data"])
            for k, v in target.items():
                if k not in ("id",):
                    data[k] = v
            # Frame provenance (Update 1a)
            _write_provenance(data, target, frame, op_id)
            db.execute(
                "UPDATE _projected SET data=?, last_op=? WHERE entity_id=? AND tbl=?",
                (json.dumps(data), op_id, eid, tbl)
            )

    elif op == "DES":
        # Designation: apply naming/framing to entity
        if not eid:
            return
        existing = db.execute(
            "SELECT data FROM _projected WHERE entity_id=? AND tbl=?",
            (eid, tbl)
        ).fetchone()
        if existing:
            data = json.loads(existing["data"])
            # DES stores designations in a _des namespace
            des = data.get("_des", {})
            for k, v in target.items():
                if k not in ("id", "query"):
                    des[k] = v
            data["_des"] = des
            db.execute(
                "UPDATE _projected SET data=?, last_op=? WHERE entity_id=? AND tbl=?",
                (json.dumps(data), op_id, eid, tbl)
            )

    elif op == "NUL":
        # True destruction — the only operator that kills an entity
        if eid:
            db.execute(
                "UPDATE _projected SET alive=0, last_op=? WHERE entity_id=? AND tbl=?",
                (op_id, eid, tbl)
            )
            # Kill associated CON edges
            db.execute(
                "UPDATE _con_edges SET alive=0 WHERE source_id=? OR target_id=?",
                (eid, eid)
            )
        # NUL can also target a field on an entity (field-level destruction)
        field = target.get("field")
        entity = target.get("entity_id") or eid
        if field and entity:
            existing = db.execute(
                "SELECT data FROM _projected WHERE entity_id=? AND tbl=?",
                (entity, tbl)
            ).fetchone()
            if existing:
                data = json.loads(existing["data"])
                # Mark field as NUL'd — true absence
                data[field] = {"_nul": True, "op_id": op_id}
                db.execute(
                    "UPDATE _projected SET data=?, last_op=? WHERE entity_id=? AND tbl=?",
                    (json.dumps(data), op_id, entity, tbl)
                )

    elif op == "CON":
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
            elif coupling >= 0.55:
                stance = "generative"
            else:
                stance = "accidental"
        con_data = {k: v for k, v in target.items()
                    if k not in ("source", "target", "from", "to", "coupling", "stance", "id")}
        if src and tgt:
            db.execute(
                "INSERT INTO _con_edges(source_id, target_id, stance, coupling, data, op_id, alive) "
                "VALUES(?,?,?,?,?,?,1)",
                (src, tgt, stance, coupling, json.dumps(con_data), op_id)
            )

    elif op == "SYN":
        # Synthesis: merge entity B into entity A
        merge_into = target.get("merge_into") or target.get("primary")
        merge_from = target.get("merge_from") or target.get("secondary")
        if merge_into and merge_from:
            a_row = db.execute(
                "SELECT data FROM _projected WHERE entity_id=? AND tbl=?",
                (merge_into, tbl)
            ).fetchone()
            b_row = db.execute(
                "SELECT data FROM _projected WHERE entity_id=? AND tbl=?",
                (merge_from, tbl)
            ).fetchone()
            if a_row and b_row:
                a_data = json.loads(a_row["data"])
                b_data = json.loads(b_row["data"])
                # Merge B into A (A wins on conflict)
                syn_data = {**b_data, **a_data}
                syn_data["_syn_from"] = syn_data.get("_syn_from", []) + [merge_from]
                db.execute(
                    "UPDATE _projected SET data=?, last_op=? WHERE entity_id=? AND tbl=?",
                    (json.dumps(syn_data), op_id, merge_into, tbl)
                )
                # Mark B as dead
                db.execute(
                    "UPDATE _projected SET alive=0, last_op=? WHERE entity_id=? AND tbl=?",
                    (op_id, merge_from, tbl)
                )
                # Reassign B's edges to A
                db.execute(
                    "UPDATE _con_edges SET source_id=? WHERE source_id=? AND alive=1",
                    (merge_into, merge_from)
                )
                db.execute(
                    "UPDATE _con_edges SET target_id=? WHERE target_id=? AND alive=1",
                    (merge_into, merge_from)
                )

    elif op == "SUP":
        # Superposition: store multiple simultaneous values for a field
        if not eid:
            return
        field = target.get("field")
        variants = target.get("variants", [])
        existing = db.execute(
            "SELECT data FROM _projected WHERE entity_id=? AND tbl=?",
            (eid, tbl)
        ).fetchone()
        if existing and field:
            data = json.loads(existing["data"])
            data[field] = {"_sup": variants, "op_id": op_id}
            db.execute(
                "UPDATE _projected SET data=?, last_op=? WHERE entity_id=? AND tbl=?",
                (json.dumps(data), op_id, eid, tbl)
            )

    elif op == "REC":
        # Reconfiguration — handle snapshot ingest, feedback rules, or emergence scan
        rec_type = context.get("type")
        if rec_type == "snapshot_ingest":
            _handle_snapshot_ingest(db, op_id, target, context, frame)
        elif rec_type == "feedback_rule":
            # Store the rule as a projected entity in _rules table (Update 2b)
            rule_id = target.get("id", f"rule-{op_id}")
            db.execute(
                "INSERT OR REPLACE INTO _projected(entity_id, tbl, data, alive, last_op) "
                "VALUES(?,?,?,1,?)",
                (rule_id, "_rules", json.dumps(target), op_id)
            )
        elif rec_type == "emergence_scan":
            # Find unnamed clusters in the CON graph (Update 2c)
            _handle_emergence_scan(db, op_id, target, context, frame)

    elif op == "SEG":
        # Segmentation: no projection effect by default
        # SEG is handled at query time (filtering)
        # But we can record boundary metadata on an entity
        if eid:
            existing = db.execute(
                "SELECT data FROM _projected WHERE entity_id=? AND tbl=?",
                (eid, tbl)
            ).fetchone()
            if existing:
                data = json.loads(existing["data"])
                segs = data.get("_seg", [])
                boundary = target.get("boundary") or context.get("boundary")
                if boundary:
                    segs.append({"boundary": boundary, "op_id": op_id})
                    data["_seg"] = segs
                    db.execute(
                        "UPDATE _projected SET data=?, last_op=? WHERE entity_id=? AND tbl=?",
                        (json.dumps(data), op_id, eid, tbl)
                    )

    # Track operator history on the entity (_ops counter)
    # CON tracks on both source and target entities across all tables
    if op == "CON":
        con_src = target.get("source") or target.get("from")
        con_tgt = target.get("target") or target.get("to")
        for _cid in (con_src, con_tgt):
            if not _cid:
                continue
            # CON edges can cross tables, so find the entity wherever it lives
            _ops_row = db.execute(
                "SELECT entity_id, tbl, data FROM _projected WHERE entity_id=? AND alive=1",
                (_cid,)
            ).fetchone()
            if _ops_row:
                _ops_data = json.loads(_ops_row["data"])
                ops_history = _ops_data.get("_ops", {})
                ops_history["CON"] = ops_history.get("CON", 0) + 1
                _ops_data["_ops"] = ops_history
                db.execute(
                    "UPDATE _projected SET data=? WHERE entity_id=? AND tbl=?",
                    (json.dumps(_ops_data), _cid, _ops_row["tbl"])
                )
    else:
        _eid = eid
        if not _eid and op == "SYN":
            _eid = target.get("merge_into") or target.get("primary")
        elif not _eid and op == "NUL":
            _eid = target.get("entity_id")
        if _eid:
            _ops_row = db.execute(
                "SELECT data FROM _projected WHERE entity_id=? AND tbl=?",
                (_eid, tbl)
            ).fetchone()
            if _ops_row:
                _ops_data = json.loads(_ops_row["data"])
                ops_history = _ops_data.get("_ops", {})
                ops_history[op] = ops_history.get(op, 0) + 1
                _ops_data["_ops"] = ops_history
                db.execute(
                    "UPDATE _projected SET data=? WHERE entity_id=? AND tbl=?",
                    (json.dumps(_ops_data), _eid, tbl)
                )

    # Update watermark
    db.execute(
        "UPDATE _meta SET value=? WHERE key='projection_watermark'",
        (str(op_id),)
    )


def maybe_snapshot(db: sqlite3.Connection, op_id: int):
    """Take a snapshot checkpoint if we've hit the interval."""
    interval = int(
        db.execute("SELECT value FROM _meta WHERE key='snapshot_interval'")
        .fetchone()["value"]
    )
    if interval <= 0 or op_id % interval != 0:
        return

    ts = datetime.now(timezone.utc).isoformat()
    # Get distinct tables
    tables = db.execute(
        "SELECT DISTINCT tbl FROM _projected"
    ).fetchall()
    for row in tables:
        tbl = row["tbl"]
        entities = db.execute(
            "SELECT entity_id, data, alive FROM _projected WHERE tbl=?",
            (tbl,)
        ).fetchall()
        snapshot_data = [
            {"entity_id": e["entity_id"], "data": json.loads(e["data"]),
             "alive": bool(e["alive"])}
            for e in entities
        ]
        db.execute(
            "INSERT INTO _snapshots(op_id, ts, tbl, data) VALUES(?,?,?,?)",
            (op_id, ts, tbl, json.dumps(snapshot_data))
        )


def rebuild_projection(db: sqlite3.Connection):
    """Full rebuild: drop all projection data and replay from log."""
    db.execute("DELETE FROM _projected")
    db.execute("DELETE FROM _con_edges")
    db.execute("DELETE FROM _snapshots")
    db.execute("UPDATE _meta SET value='0' WHERE key='projection_watermark'")
    db.commit()

    ops = db.execute(
        "SELECT id, op, target, context, frame FROM operations ORDER BY id"
    ).fetchall()
    for row in ops:
        target = json.loads(row["target"])
        context = json.loads(row["context"])
        frame = json.loads(row["frame"])
        project_op(db, row["id"], row["op"], target, context, frame)
    if ops:
        maybe_snapshot(db, ops[-1]["id"])
    db.commit()


def catchup_projection(db: sqlite3.Connection):
    """Replay any operations appended since last projection watermark."""
    wm = int(
        db.execute("SELECT value FROM _meta WHERE key='projection_watermark'")
        .fetchone()["value"]
    )
    ops = db.execute(
        "SELECT id, op, target, context, frame FROM operations WHERE id > ? ORDER BY id",
        (wm,)
    ).fetchall()
    for row in ops:
        target = json.loads(row["target"])
        context = json.loads(row["context"])
        frame = json.loads(row["frame"])
        project_op(db, row["id"], row["op"], target, context, frame)
    if ops:
        maybe_snapshot(db, ops[-1]["id"])
    db.commit()


# ---------------------------------------------------------------------------
# Snapshot Ingest (REC with context.type = "snapshot_ingest")
# ---------------------------------------------------------------------------
def _handle_snapshot_ingest(db, rec_op_id, target, context, frame):
    """
    Diff incoming snapshot rows against current projection.
    Generate granular INS/ALT/NUL operations.
    """
    tbl = context.get("table", "_default")
    rows = target.get("rows", [])
    match_on = frame.get("match_on", "id")
    absence_means = frame.get("absence_means", "unchanged")  # unchanged | deleted | investigate
    null_fields_mean = frame.get("null_fields_mean", "unchanged")  # unchanged | cleared | unknown
    ignore_fields = set(frame.get("ignore_fields", []))

    # Build lookup of current projected state
    current = {}
    for row in db.execute(
        "SELECT entity_id, data FROM _projected WHERE tbl=? AND alive=1", (tbl,)
    ).fetchall():
        current[row["entity_id"]] = json.loads(row["data"])

    generated_ops = []
    seen_ids = set()

    for incoming_row in rows:
        eid = str(incoming_row.get(match_on, ""))
        if not eid:
            continue
        seen_ids.add(eid)

        if eid not in current:
            # New entity → INS
            op_target = {"id": eid}
            for k, v in incoming_row.items():
                if k != match_on and k not in ignore_fields and v is not None:
                    op_target[k] = v
            generated_ops.append(("INS", op_target, {
                "table": tbl, "generated_by": rec_op_id
            }, {}))
        else:
            # Existing entity → check for changes
            cur_data = current[eid]
            changes = {}
            for k, v in incoming_row.items():
                if k == match_on or k in ignore_fields:
                    continue
                if v is None:
                    if null_fields_mean == "unchanged":
                        continue
                    elif null_fields_mean == "cleared":
                        if k in cur_data and cur_data[k] != {"_nul": True}:
                            # Field-level NUL
                            generated_ops.append(("NUL", {
                                "entity_id": eid, "field": k
                            }, {
                                "table": tbl, "generated_by": rec_op_id
                            }, {}))
                        continue
                    elif null_fields_mean == "unknown":
                        # SUP: source says null, we have a value
                        if k in cur_data:
                            generated_ops.append(("SUP", {
                                "id": eid, "field": k,
                                "variants": [
                                    {"source": "projection", "value": cur_data.get(k)},
                                    {"source": "snapshot", "value": None}
                                ]
                            }, {
                                "table": tbl, "generated_by": rec_op_id
                            }, {}))
                        continue

                cur_val = cur_data.get(k)
                # Handle SUP'd fields: compare against first variant
                if isinstance(cur_val, dict) and "_sup" in cur_val:
                    cur_val = cur_val["_sup"][0].get("value") if cur_val["_sup"] else None
                if isinstance(cur_val, dict) and "_nul" in cur_val:
                    cur_val = None

                if v != cur_val:
                    changes[k] = v

            if changes:
                op_target = {"id": eid, **changes}
                generated_ops.append(("ALT", op_target, {
                    "table": tbl, "generated_by": rec_op_id
                }, {}))

    # Handle absent entities
    if absence_means == "deleted":
        for eid in current:
            if eid not in seen_ids:
                generated_ops.append(("NUL", {"id": eid}, {
                    "table": tbl, "generated_by": rec_op_id
                }, {}))
    elif absence_means == "investigate":
        for eid in current:
            if eid not in seen_ids:
                generated_ops.append(("DES", {"id": eid, "flag": "absent_from_snapshot"}, {
                    "table": tbl, "generated_by": rec_op_id
                }, {}))

    # Append generated ops and project them
    for g_op, g_target, g_context, g_frame in generated_ops:
        cursor = db.execute(
            "INSERT INTO operations(op, target, context, frame) VALUES(?,?,?,?)",
            (g_op, json.dumps(g_target), json.dumps(g_context), json.dumps(g_frame))
        )
        project_op(db, cursor.lastrowid, g_op, g_target, g_context, g_frame)

    return generated_ops


# ---------------------------------------------------------------------------
# Feedback Rule Evaluation (Update 2b)
# ---------------------------------------------------------------------------
def _trigger_matches(match: dict, target: dict, context: dict, frame: dict) -> bool:
    """Check if an operation matches a rule's trigger conditions."""
    for key, expected in match.items():
        # Support dotted paths like "target.flag"
        parts = key.split(".", 1)
        if len(parts) == 2:
            scope, field = parts
            source = {"target": target, "context": context, "frame": frame}.get(scope, {})
            if source.get(field) != expected:
                return False
        else:
            if target.get(key) != expected:
                return False
    return True


def _interpolate_template(template: dict, data: dict) -> dict:
    """Replace {trigger.target.id} style placeholders in a template dict."""
    result = {}
    for k, v in template.items():
        if isinstance(v, str) and "{" in v:
            # Replace placeholders
            def replacer(m):
                path = m.group(1).split(".")
                obj = data
                for p in path:
                    if isinstance(obj, dict):
                        obj = obj.get(p, "")
                    else:
                        return m.group(0)
                return str(obj) if not isinstance(obj, str) else obj
            v = re.sub(r'\{([^}]+)\}', replacer, v)
        elif isinstance(v, dict):
            v = _interpolate_template(v, data)
        result[k] = v
    return result


def _evaluate_rules(db, op_id: int, op: str, target: dict, context: dict, frame: dict):
    """Check if any feedback rules trigger on this operation (Update 2b).
    Rules cannot trigger other rules in the same evaluation pass (prevents infinite loops)."""
    # Don't evaluate rules for rule-generated operations
    if context.get("generated_by_rule"):
        return

    rules = db.execute(
        "SELECT entity_id, data FROM _projected WHERE tbl='_rules' AND alive=1"
    ).fetchall()

    for rule_row in rules:
        rule = json.loads(rule_row["data"])
        trigger = rule.get("trigger")
        if not trigger:
            continue  # Not a feedback rule (could be a replay profile)

        # Check if this operation matches the trigger
        if trigger.get("op") and trigger["op"] != op:
            continue
        match = trigger.get("match", {})
        if not _trigger_matches(match, target, context, frame):
            continue

        # Generate the action operation
        action = rule.get("action", {})
        if not action.get("op"):
            continue

        interp_data = {"trigger": {"target": target, "context": context, "frame": frame}}
        generated_target = _interpolate_template(
            action.get("target_template", {}), interp_data
        )
        generated_context = _interpolate_template(
            action.get("context", {}), interp_data
        )
        generated_context["generated_by_rule"] = rule_row["entity_id"]

        # Append and project the generated operation
        cursor = db.execute(
            "INSERT INTO operations(op, target, context, frame) VALUES(?,?,?,?)",
            (action["op"], json.dumps(generated_target),
             json.dumps(generated_context), json.dumps({}))
        )
        project_op(db, cursor.lastrowid, action["op"],
                   generated_target, generated_context, {})


# ---------------------------------------------------------------------------
# Emergence Scan (Update 2c)
# ---------------------------------------------------------------------------
def _handle_emergence_scan(db, op_id: int, target: dict, context: dict, frame: dict):
    """Scan the CON graph for unnamed clusters and designate them."""
    min_cluster_size = target.get("min_cluster_size", 3)
    min_coupling = target.get("min_internal_coupling", 0.0)

    # Build adjacency list from live CON edges
    min_stance = target.get("min_stance", None)  # accidental, essential, or generative
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
    ).fetchall()

    adjacency = {}
    for edge in edges:
        src, tgt = edge["source_id"], edge["target_id"]
        adjacency.setdefault(src, set()).add(tgt)
        adjacency.setdefault(tgt, set()).add(src)

    # BFS connected components
    visited = set()
    clusters = []
    for node in adjacency:
        if node in visited:
            continue
        component = set()
        queue = [node]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            for neighbor in adjacency.get(current, set()):
                if neighbor not in visited:
                    queue.append(neighbor)
        if len(component) >= min_cluster_size:
            clusters.append(component)

    generated_ops = []
    for cluster in clusters:
        # Check if this cluster is already designated in _emergent
        cluster_id = f"cluster-{hashlib.md5(str(sorted(cluster)).encode()).hexdigest()[:8]}"
        existing = db.execute(
            "SELECT entity_id FROM _projected WHERE entity_id=? AND tbl='_emergent' AND alive=1",
            (cluster_id,)
        ).fetchone()
        if existing:
            continue

        # DES the cluster as an emergent entity
        des_target = {
            "id": cluster_id,
            "flag": "emergent_cluster",
            "members": sorted(list(cluster)),
            "member_count": len(cluster)
        }
        des_context = {
            "table": "_emergent",
            "generated_by": op_id
        }
        cursor = db.execute(
            "INSERT INTO operations(op, target, context, frame) VALUES(?,?,?,?)",
            ("DES", json.dumps(des_target), json.dumps(des_context), json.dumps(frame))
        )
        project_op(db, cursor.lastrowid, "DES", des_target, des_context, frame)
        generated_ops.append({"op": "DES", "target": des_target})

    return generated_ops


# ---------------------------------------------------------------------------
# EOQL Parser
# ---------------------------------------------------------------------------
# Grammar:
#   state(field=value, field=value, ...)
#   state(field=value, ..., at="timestamp")
#   stream(op=OP, field=value, ...)
#   state(...) >> CON(hops=N)
#   state(...) >> CON(hops=N, min_coupling=0.5)

def parse_eoql(query_str: str) -> dict:
    """Parse an EOQL query string into a structured query dict."""
    query_str = query_str.strip()

    # meta() shorthand (Update 7c)
    meta_match = re.match(r'^meta\((\w+)\)$', query_str)
    if meta_match:
        meta_table = f"_{meta_match.group(1)}"
        return {"type": "state", "filters": {"context.table": meta_table}}

    # Check for CON chaining: state(...) >> CON(...)
    chain_match = re.match(r'^(state\([^)]*\))\s*>>\s*CON\(([^)]*)\)$', query_str)
    if chain_match:
        base = parse_eoql(chain_match.group(1))
        con_params = _parse_params(chain_match.group(2))
        base["chain"] = {
            "type": "CON",
            "hops": int(con_params.get("hops", 1)),
            "min_coupling": float(con_params.get("min_coupling", 0)),
        }
        if "stance" in con_params:
            base["chain"]["stance"] = con_params["stance"]
        if "exclude" in con_params:
            base["chain"]["exclude"] = con_params["exclude"]
        return base

    # state(...)
    state_match = re.match(r'^state\(([^)]*)\)$', query_str)
    if state_match:
        params = _parse_params(state_match.group(1))
        result = {"type": "state", "filters": {}}
        for k, v in params.items():
            if k == "at":
                result["at"] = v
            elif k.startswith("context."):
                result["filters"][k] = v
            elif k.startswith("target."):
                result["filters"][k] = v
            else:
                # Shorthand: bare keys are target fields
                result["filters"][f"target.{k}"] = v
        return result

    # stream(...)
    stream_match = re.match(r'^stream\(([^)]*)\)$', query_str)
    if stream_match:
        params = _parse_params(stream_match.group(1))
        return {"type": "stream", "filters": params}

    # Fallback: treat as state query with table name
    return {"type": "state", "filters": {"context.table": query_str}}


def _parse_params(param_str: str) -> dict:
    """Parse key=value pairs from inside parentheses."""
    params = {}
    if not param_str.strip():
        return params
    # Split on commas, respecting quoted strings
    parts = re.findall(r'(\w[\w.]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))', param_str)
    for key, v1, v2, v3 in parts:
        val = v1 or v2 or v3
        # Type coercion
        if val.lower() in ('true', 'false'):
            val = val.lower() == 'true'
        else:
            try:
                val = int(val)
            except (ValueError, TypeError):
                try:
                    val = float(val)
                except (ValueError, TypeError):
                    pass
        params[key] = val
    return params


# ---------------------------------------------------------------------------
# Query Execution
# ---------------------------------------------------------------------------
def execute_eoql(db: sqlite3.Connection, query: dict) -> dict:
    """Execute a parsed EOQL query and return results."""
    if query["type"] == "state":
        return _exec_state(db, query)
    elif query["type"] == "stream":
        return _exec_stream(db, query)
    return {"error": "Unknown query type"}


def _apply_replay_profile(results: list, frame: dict, filters: dict):
    """Apply replay profile stances to query results (Update 5)."""
    if not frame:
        return
    replay = frame.get("replay", {})
    if not replay:
        return

    # Conflict stance (Update 5b)
    conflict_stance = replay.get("conflict", "preserve")
    if conflict_stance == "collapse":
        for entity in results:
            for k, v in list(entity.items()):
                if isinstance(v, dict) and "_sup" in v:
                    variants = v["_sup"]
                    entity[k] = variants[0]["value"] if variants else None
                    entity.setdefault("_collapsed", []).append(k)
    elif conflict_stance == "suspend":
        for entity in results:
            for k, v in entity.items():
                if isinstance(v, dict) and "_sup" in v:
                    entity["_suspended"] = True
                    break


def _exec_state(db: sqlite3.Connection, query: dict) -> dict:
    """Execute a state() query against the projection (or replay for time-travel)."""
    filters = query.get("filters", {})
    at = query.get("at")
    frame = query.get("frame", {})
    tbl = filters.get("context.table", "_default")

    # Boundary stance: unified mode ignores SEG boundary filters
    replay = frame.get("replay", {}) if frame else {}
    if replay.get("boundary") == "unified" and "_seg.boundary" in filters:
        filters = {k: v for k, v in filters.items() if k != "_seg.boundary"}

    if at:
        # Time-travel: find nearest snapshot, replay forward
        return _exec_state_at(db, tbl, filters, at)

    # Cross-table target.id lookup: when id specified but no table, search all tables
    cross_table = ("target.id" in filters and "context.table" not in filters)

    # Current state: read from projection (Update 4c/4d: dead entity filters)
    if cross_table:
        eid = filters["target.id"]
        if filters.get("_only_dead"):
            rows = db.execute(
                "SELECT entity_id, tbl as _tbl, data, alive FROM _projected WHERE entity_id=? AND alive=0",
                (eid,)
            ).fetchall()
        elif filters.get("_include_dead"):
            rows = db.execute(
                "SELECT entity_id, tbl as _tbl, data, alive FROM _projected WHERE entity_id=?",
                (eid,)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT entity_id, tbl as _tbl, data FROM _projected WHERE entity_id=? AND alive=1",
                (eid,)
            ).fetchall()
    elif filters.get("_only_dead"):
        rows = db.execute(
            "SELECT entity_id, data, alive FROM _projected WHERE tbl=? AND alive=0",
            (tbl,)
        ).fetchall()
    elif filters.get("_include_dead"):
        rows = db.execute(
            "SELECT entity_id, data, alive FROM _projected WHERE tbl=?",
            (tbl,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT entity_id, data FROM _projected WHERE tbl=? AND alive=1",
            (tbl,)
        ).fetchall()

    results = []
    for row in rows:
        data = json.loads(row["data"])
        entity = {"id": row["entity_id"], **data}
        # Include table info for cross-table queries
        if cross_table:
            entity["_table"] = row["_tbl"]
        # Mark dead entities when included
        if filters.get("_include_dead") or filters.get("_only_dead"):
            entity["_alive"] = bool(row["alive"])

        # Apply target.* filters
        if _matches_filters(entity, filters):
            results.append(entity)

    result = {"type": "state", "table": tbl if not cross_table else "_cross",
              "count": len(results), "entities": results}

    # Apply replay profile post-processing (Update 5)
    _apply_replay_profile(results, frame, filters)

    result["count"] = len(results)

    # Handle CON chaining
    if "chain" in query:
        result = _exec_con_chain(db, result, query["chain"])

    return result


def _exec_state_at(db: sqlite3.Connection, tbl: str, filters: dict, at: str) -> dict:
    """Time-travel query: project state as of a given timestamp."""
    # Find nearest snapshot before `at`
    snap = db.execute(
        "SELECT op_id, data FROM _snapshots WHERE tbl=? AND ts<=? ORDER BY op_id DESC LIMIT 1",
        (tbl, at)
    ).fetchone()

    if snap:
        # Load snapshot as base state
        entities = {e["entity_id"]: e for e in json.loads(snap["data"])}
        start_op = snap["op_id"]
    else:
        entities = {}
        start_op = 0

    # Replay ops from snapshot to target timestamp
    ops = db.execute(
        "SELECT id, op, target, context, frame FROM operations "
        "WHERE id > ? AND ts <= ? ORDER BY id",
        (start_op, at)
    ).fetchall()

    for row in ops:
        op = row["op"]
        target = json.loads(row["target"])
        context = json.loads(row["context"])
        op_tbl = context.get("table", "_default")
        if op_tbl != tbl:
            continue
        eid = target.get("id")
        if not eid:
            continue

        if op == "INS":
            data = {k: v for k, v in target.items() if k != "id"}
            if eid in entities:
                entities[eid]["data"].update(data)
                entities[eid]["alive"] = True
            else:
                entities[eid] = {"entity_id": eid, "data": data, "alive": True}
        elif op == "ALT":
            if eid in entities:
                for k, v in target.items():
                    if k != "id":
                        entities[eid]["data"][k] = v
        elif op == "NUL":
            if eid in entities:
                entities[eid]["alive"] = False
        elif op == "SUP":
            field = target.get("field")
            variants = target.get("variants", [])
            if eid in entities and field:
                entities[eid]["data"][field] = {"_sup": variants}

    results = []
    for eid, ent in entities.items():
        if not ent.get("alive", True):
            continue
        entity = {"id": eid, **ent["data"]}
        if _matches_filters(entity, filters):
            results.append(entity)

    return {"type": "state", "table": tbl, "at": at, "count": len(results), "entities": results}


def _exec_stream(db: sqlite3.Connection, query: dict) -> dict:
    """Execute a stream() query against the operations log."""
    filters = query.get("filters", {})
    conditions = ["1=1"]
    params = []

    if "op" in filters:
        conditions.append("op=?")
        params.append(filters["op"])
    if "after" in filters:
        conditions.append("ts>?")
        params.append(filters["after"])
    if "before" in filters:
        conditions.append("ts<?")
        params.append(filters["before"])
    # Support context.table filter via json_extract
    if "context.table" in filters:
        conditions.append("json_extract(context, '$.table')=?")
        params.append(filters["context.table"])
    if "limit" not in filters:
        filters["limit"] = 100

    sql = f"SELECT id, ts, op, target, context, frame FROM operations WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT ?"
    params.append(filters["limit"])

    rows = db.execute(sql, params).fetchall()
    ops = []
    for row in rows:
        op_data = {
            "id": row["id"], "ts": row["ts"], "op": row["op"],
            "target": json.loads(row["target"]),
            "context": json.loads(row["context"]),
            "frame": json.loads(row["frame"])
        }
        # Apply remaining dotted filters client-side (target.*, context.*)
        match = True
        for key, val in filters.items():
            if key in ("op", "after", "before", "limit", "context.table"):
                continue
            parts = key.split(".", 1)
            if len(parts) == 2:
                scope, field = parts
                if scope in ("target", "context", "frame"):
                    if op_data.get(scope, {}).get(field) != val:
                        match = False
                        break
        if match:
            ops.append(op_data)

    return {"type": "stream", "count": len(ops), "operations": ops}


def _exec_con_chain(db: sqlite3.Connection, base_result: dict, chain: dict) -> dict:
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
        frontier = next_frontier

    # Fetch entity data for all reached nodes
    reached_entities = []
    for eid in visited - seed_ids:
        row = db.execute(
            "SELECT entity_id, tbl, data FROM _projected WHERE entity_id=? AND alive=1",
            (eid,)
        ).fetchone()
        if row:
            data = json.loads(row["data"])
            reached_entities.append({"id": eid, "table": row["tbl"], **data})

    chain_meta = {
        "hops": hops, "min_coupling": min_coupling,
        "reached": reached_entities,
        "edges": edges_found,
        "reached_count": len(reached_entities)
    }
    if stance_filter:
        chain_meta["stance"] = stance_filter
    if exclude_filter:
        chain_meta["exclude"] = exclude_filter
    base_result["con_chain"] = chain_meta
    return base_result


def _matches_filters(entity: dict, filters: dict) -> bool:
    """Check if an entity matches the given filters."""
    for key, val in filters.items():
        if key.startswith("context."):
            continue  # Already handled by table selection
        # Meta filters — skip these (handled elsewhere)
        if key in ("_include_dead", "_only_dead"):
            continue

        # SEG boundary filter (Update 3)
        if key == "_seg.boundary":
            segs = entity.get("_seg", [])
            if not any(s.get("boundary") == val for s in segs):
                return False
            continue

        # Absence filters (Update 4)
        if key == "_has_sup":
            has_sup = any(
                isinstance(v, dict) and "_sup" in v
                for k, v in entity.items() if not k.startswith("_")
            )
            if has_sup != bool(val):
                return False
            continue

        if key == "_has_nul":
            has_nul = any(
                isinstance(v, dict) and "_nul" in v
                for k, v in entity.items() if not k.startswith("_")
            )
            if has_nul != bool(val):
                return False
            continue

        # Frame provenance filters (Update 1c)
        if key.startswith("frame."):
            frame_field = key.replace("frame.", "")
            prov = entity.get("_provenance", {})
            if not any(p.get(frame_field) == val for p in prov.values()):
                return False
            continue

        # Operator history filters (Update 6)
        if key.startswith("_ops."):
            op_name = key.replace("_ops.", "")
            op_count = entity.get("_ops", {}).get(op_name, 0)
            if isinstance(val, int):
                if op_count != val:
                    return False
            continue

        field = key.replace("target.", "")
        entity_val = entity.get(field)
        # Handle SUP'd fields: match against any variant
        if isinstance(entity_val, dict) and "_sup" in entity_val:
            if not any(v.get("value") == val for v in entity_val["_sup"]):
                return False
            continue
        # Handle NUL'd fields
        if isinstance(entity_val, dict) and "_nul" in entity_val:
            if val is not None:
                return False
            continue
        if entity_val != val:
            return False
    return True


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

# -- Instance Management --

@app.route("/instances", methods=["GET"])
def list_instances():
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    instances = []
    for f in sorted(INSTANCE_DIR.glob("*.db")):
        slug = f.stem
        db = get_db(slug)
        if db is None:
            db = init_db(slug)
        op_count = db.execute("SELECT COUNT(*) as c FROM operations").fetchone()["c"]
        entity_count = db.execute("SELECT COUNT(*) as c FROM _projected WHERE alive=1").fetchone()["c"]
        last_ts_row = db.execute("SELECT MAX(ts) as t FROM operations").fetchone()
        last_op_ts = last_ts_row["t"] if last_ts_row else None
        instances.append({
            "slug": slug,
            "operations": op_count,
            "entities": entity_count,
            "file_size": f.stat().st_size,
            "last_op_ts": last_op_ts
        })
    return jsonify({"instances": instances})


@app.route("/instances", methods=["POST"])
def create_instance():
    body = request.get_json(force=True, silent=True) or {}
    slug = body.get("slug", "").strip()
    if not slug or not re.match(r'^[a-z0-9_-]+$', slug):
        return jsonify({"error": "Invalid slug. Use lowercase alphanumeric, hyphens, underscores."}), 400
    db_path = INSTANCE_DIR / f"{slug}.db"
    if db_path.exists():
        return jsonify({"error": f"Instance '{slug}' already exists."}), 409
    init_db(slug)
    return jsonify({"slug": slug, "created": True}), 201


@app.route("/instances/<slug>", methods=["DELETE"])
def delete_instance(slug):
    db_path = INSTANCE_DIR / f"{slug}.db"
    if not db_path.exists():
        return jsonify({"error": "Not found"}), 404
    # Close any cached connections
    key = f"db_{slug}"
    db = getattr(_local, key, None)
    if db:
        db.close()
        delattr(_local, key)
    # Delete all related files
    for ext in [".db", ".db-wal", ".db-shm"]:
        p = INSTANCE_DIR / f"{slug}{ext}"
        if p.exists():
            p.unlink()
    return jsonify({"slug": slug, "deleted": True})


@app.route("/instances/<slug>/seed", methods=["POST"])
def seed_instance(slug):
    """Batch-append operations to an instance."""
    db = get_db(slug)
    if db is None:
        db = init_db(slug)

    body = request.get_json(force=True, silent=True) or {}
    ops = body.get("operations", [])
    appended = 0
    for op_data in ops:
        op = op_data.get("op")
        if op not in VALID_OPS:
            continue
        target = op_data.get("target", {})
        context = op_data.get("context", {})
        frame = op_data.get("frame", {})
        ts = op_data.get("ts")

        if ts:
            cursor = db.execute(
                "INSERT INTO operations(ts, op, target, context, frame) VALUES(?,?,?,?,?)",
                (ts, op, json.dumps(target), json.dumps(context), json.dumps(frame))
            )
        else:
            cursor = db.execute(
                "INSERT INTO operations(op, target, context, frame) VALUES(?,?,?,?)",
                (op, json.dumps(target), json.dumps(context), json.dumps(frame))
            )
        project_op(db, cursor.lastrowid, op, target, context, frame)
        maybe_snapshot(db, cursor.lastrowid)
        appended += 1

    db.commit()
    return jsonify({"appended": appended})


@app.route("/instances/<slug>/rebuild", methods=["POST"])
def rebuild_instance(slug):
    """Force full projection rebuild from log."""
    db = get_db(slug)
    if db is None:
        return jsonify({"error": "Not found"}), 404
    rebuild_projection(db)
    return jsonify({"rebuilt": True})


# -- The ONE endpoint --

@app.route("/<instance>/operations", methods=["POST"])
def post_operation(instance):
    """
    The single point of entry. Every mutation, query, and ingest
    comes through here as an operation.
    """
    db = get_db(instance)
    if db is None:
        return jsonify({"error": f"Instance '{instance}' not found."}), 404

    body = request.get_json(force=True, silent=True) or {}
    op = body.get("op")
    if op not in VALID_OPS:
        return jsonify({"error": f"Invalid op: {op}. Must be one of {sorted(VALID_OPS)}"}), 400

    target = body.get("target", {})
    context = body.get("context", {})
    frame = body.get("frame", {})

    # --- DES as query ---
    if op == "DES" and "query" in target:
        query_str = target["query"]
        parsed = parse_eoql(query_str)

        # Optionally append to log for audit (Update 8)
        audit = frame.get("audit", False)
        audit_op_id = None
        if audit:
            cursor = db.execute(
                "INSERT INTO operations(op, target, context, frame) VALUES(?,?,?,?)",
                (op, json.dumps(target), json.dumps(context), json.dumps(frame))
            )
            audit_op_id = cursor.lastrowid
            project_op(db, cursor.lastrowid, op, target, context, frame)
            db.commit()

        # Pass frame to _exec_state for replay profile support (Update 5)
        if parsed.get("type") == "state":
            parsed["frame"] = frame

        result = execute_eoql(db, parsed)
        result["audited"] = audit
        if audit and audit_op_id:
            result["audit_op_id"] = audit_op_id
        return jsonify(result)

    # --- Normal operation ---
    cursor = db.execute(
        "INSERT INTO operations(op, target, context, frame) VALUES(?,?,?,?)",
        (op, json.dumps(target), json.dumps(context), json.dumps(frame))
    )
    op_id = cursor.lastrowid

    # Project synchronously
    project_op(db, op_id, op, target, context, frame)

    # Evaluate feedback rules (Update 2b)
    _evaluate_rules(db, op_id, op, target, context, frame)

    maybe_snapshot(db, op_id)
    db.commit()

    # For REC snapshot_ingest, return the generated ops
    if op == "REC" and context.get("type") == "snapshot_ingest":
        # Re-read the generated ops
        generated = db.execute(
            "SELECT id, ts, op, target, context, frame FROM operations "
            "WHERE json_extract(context, '$.generated_by')=? ORDER BY id",
            (op_id,)
        ).fetchall()
        gen_list = [{
            "id": r["id"], "ts": r["ts"], "op": r["op"],
            "target": json.loads(r["target"]),
            "context": json.loads(r["context"]),
            "frame": json.loads(r["frame"])
        } for r in generated]
        return jsonify({
            "op_id": op_id, "op": op,
            "generated": gen_list, "generated_count": len(gen_list)
        })

    # Return the appended operation
    ts = db.execute("SELECT ts FROM operations WHERE id=?", (op_id,)).fetchone()["ts"]
    result = {"op_id": op_id, "ts": ts, "op": op}

    # DES unframed warning (Update 1b)
    if op == "DES" and not frame:
        result["_warning"] = "unframed_designation"

    # Fire outbound webhooks (non-blocking)
    fire_webhooks(instance, {
        "op_id": op_id, "ts": ts, "op": op,
        "target": target, "context": context, "frame": frame
    })

    return jsonify(result)


# -- Outbound Webhook Management --

@app.route("/<instance>/webhooks", methods=["GET"])
def list_webhooks(instance):
    """List registered outbound webhooks for this instance."""
    _load_webhooks()
    hooks = _webhooks.get(instance, [])
    return jsonify({"instance": instance, "webhooks": hooks})

@app.route("/<instance>/webhooks", methods=["POST"])
def add_webhook(instance):
    """Register an outbound webhook URL. Body: {url, filter?: ["ALT","INS",...], active?: true}"""
    db = get_db(instance)
    if db is None:
        return jsonify({"error": f"Instance '{instance}' not found."}), 404
    body = request.get_json(force=True, silent=True) or {}
    url = body.get("url")
    if not url:
        return jsonify({"error": "url is required"}), 400
    _load_webhooks()
    if instance not in _webhooks:
        _webhooks[instance] = []
    hook = {"url": url, "active": body.get("active", True)}
    if body.get("filter"):
        hook["filter"] = body["filter"]
    _webhooks[instance].append(hook)
    _save_webhooks()
    return jsonify({"added": hook, "total": len(_webhooks[instance])})

@app.route("/<instance>/webhooks", methods=["DELETE"])
def remove_webhook(instance):
    """Remove a webhook by URL. Body: {url}"""
    body = request.get_json(force=True, silent=True) or {}
    url = body.get("url")
    _load_webhooks()
    hooks = _webhooks.get(instance, [])
    _webhooks[instance] = [h for h in hooks if h["url"] != url]
    _save_webhooks()
    return jsonify({"removed": url, "remaining": len(_webhooks[instance])})


# -- The ONE stream --

@app.route("/<instance>/stream", methods=["GET"])
def stream(instance):
    """SSE endpoint. Streams all new operations as they're appended."""
    db_path = INSTANCE_DIR / f"{instance}.db"
    if not db_path.exists():
        return jsonify({"error": "Not found"}), 404

    last_id = request.args.get("last_id", "0", type=int)

    def event_stream():
        # Each SSE connection gets its own DB connection
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        seen = last_id
        try:
            while True:
                rows = conn.execute(
                    "SELECT id, ts, op, target, context, frame FROM operations "
                    "WHERE id > ? ORDER BY id LIMIT 50",
                    (seen,)
                ).fetchall()
                for row in rows:
                    data = json.dumps({
                        "id": row["id"], "ts": row["ts"], "op": row["op"],
                        "target": json.loads(row["target"]),
                        "context": json.loads(row["context"]),
                        "frame": json.loads(row["frame"])
                    })
                    yield f"id: {row['id']}\nevent: op\ndata: {data}\n\n"
                    seen = row["id"]
                if not rows:
                    yield ": heartbeat\n\n"
                time.sleep(0.2)
        except GeneratorExit:
            conn.close()

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


# -- Convenience: direct state query (GET shorthand) --

@app.route("/<instance>/state/<tbl>", methods=["GET"])
def get_state(instance, tbl):
    """
    Convenience GET for state queries. Equivalent to:
    POST /{instance}/operations { op: "DES", target: { query: "state(context.table=tbl)" } }
    Supports ?replay=profileName for named replay profiles (Update 5d).
    """
    db = get_db(instance)
    if db is None:
        return jsonify({"error": "Not found"}), 404
    query = {"type": "state", "filters": {"context.table": tbl}}
    at = request.args.get("at")
    if at:
        query["at"] = at

    # Named replay profile support (Update 5d)
    replay_name = request.args.get("replay")
    if replay_name:
        profile_row = db.execute(
            "SELECT data FROM _projected WHERE entity_id=? AND tbl='_rules' AND alive=1",
            (f"profile-{replay_name}",)
        ).fetchone()
        if profile_row:
            profile_data = json.loads(profile_row["data"])
            query["frame"] = {"replay": profile_data}

    return jsonify(execute_eoql(db, query))


@app.route("/<instance>/state/<tbl>/<eid>", methods=["GET"])
def get_entity(instance, tbl, eid):
    """Convenience: get a single entity."""
    db = get_db(instance)
    if db is None:
        return jsonify({"error": "Not found"}), 404
    row = db.execute(
        "SELECT entity_id, data, alive FROM _projected WHERE entity_id=? AND tbl=?",
        (eid, tbl)
    ).fetchone()
    if not row:
        return jsonify({"error": "Entity not found"}), 404
    data = json.loads(row["data"])
    return jsonify({"id": eid, "table": tbl, "alive": bool(row["alive"]), **data})


# ---------------------------------------------------------------------------
# Demo Seed Data
# ---------------------------------------------------------------------------
DEMO_SEED = {
    "operations": [
        # --- Places ---
        {"ts": "2025-01-05T10:00:00Z", "op": "INS", "target": {"id": "pl0", "name": "Listening Room", "type": "venue", "status": "open"}, "context": {"table": "places"}, "frame": {}},
        {"ts": "2025-01-05T10:01:00Z", "op": "INS", "target": {"id": "pl1", "name": "Five Points Pizza", "type": "restaurant", "status": "open"}, "context": {"table": "places"}, "frame": {}},
        {"ts": "2025-01-06T09:00:00Z", "op": "INS", "target": {"id": "pl2", "name": "Shelby Park", "type": "park", "status": "open"}, "context": {"table": "places"}, "frame": {}},
        {"ts": "2025-01-07T11:00:00Z", "op": "INS", "target": {"id": "pl3", "name": "Red Door Saloon", "type": "bar", "status": "open"}, "context": {"table": "places"}, "frame": {}},
        {"ts": "2025-01-10T08:00:00Z", "op": "INS", "target": {"id": "pl4", "name": "East Nashville Library", "type": "library", "status": "open"}, "context": {"table": "places"}, "frame": {}},
        {"ts": "2025-01-12T14:00:00Z", "op": "INS", "target": {"id": "pl5", "name": "Rook Books", "type": "bookstore", "status": "open"}, "context": {"table": "places"}, "frame": {}},
        {"ts": "2025-02-01T09:00:00Z", "op": "INS", "target": {"id": "pl6", "name": "Barista Parlor", "type": "cafe", "status": "open"}, "context": {"table": "places"}, "frame": {}},
        {"ts": "2025-03-15T12:00:00Z", "op": "INS", "target": {"id": "pl7", "name": "Riverside Grill", "type": "restaurant", "status": "open"}, "context": {"table": "places"}, "frame": {}},
        # Rook Books closes
        {"ts": "2025-09-01T00:00:00Z", "op": "NUL", "target": {"id": "pl5"}, "context": {"table": "places"}, "frame": {}},
        # Status change
        {"ts": "2025-06-15T10:00:00Z", "op": "ALT", "target": {"id": "pl3", "status": "renovating"}, "context": {"table": "places"}, "frame": {}},
        {"ts": "2025-08-01T10:00:00Z", "op": "ALT", "target": {"id": "pl3", "status": "open"}, "context": {"table": "places"}, "frame": {}},

        # --- People ---
        {"ts": "2025-01-05T10:05:00Z", "op": "INS", "target": {"id": "pe0", "name": "Tomás", "role": "musician"}, "context": {"table": "people"}, "frame": {}},
        {"ts": "2025-01-05T10:06:00Z", "op": "INS", "target": {"id": "pe1", "name": "Marguerite", "role": "chef"}, "context": {"table": "people"}, "frame": {}},
        {"ts": "2025-01-06T11:00:00Z", "op": "INS", "target": {"id": "pe2", "name": "Dante", "role": "bartender"}, "context": {"table": "people"}, "frame": {}},
        {"ts": "2025-01-07T08:00:00Z", "op": "INS", "target": {"id": "pe3", "name": "Lina", "role": "librarian"}, "context": {"table": "people"}, "frame": {}},
        {"ts": "2025-01-10T09:00:00Z", "op": "INS", "target": {"id": "pe4", "name": "Felix", "role": "organizer"}, "context": {"table": "people"}, "frame": {}},
        {"ts": "2025-01-15T10:00:00Z", "op": "INS", "target": {"id": "pe5", "name": "June", "role": "artist"}, "context": {"table": "people"}, "frame": {}},
        {"ts": "2025-02-10T10:00:00Z", "op": "INS", "target": {"id": "pe6", "name": "Rafael", "role": "cook"}, "context": {"table": "people"}, "frame": {}},
        {"ts": "2025-03-01T10:00:00Z", "op": "INS", "target": {"id": "pe7", "name": "Mika", "role": "barista"}, "context": {"table": "people"}, "frame": {}},

        # --- Events ---
        {"ts": "2025-01-20T18:00:00Z", "op": "INS", "target": {"id": "ev0", "name": "Open Mic Night", "type": "music", "date": "2025-01-25"}, "context": {"table": "events"}, "frame": {}},
        {"ts": "2025-02-05T10:00:00Z", "op": "INS", "target": {"id": "ev1", "name": "Book Club: Beloved", "type": "literary", "date": "2025-02-15"}, "context": {"table": "events"}, "frame": {}},
        {"ts": "2025-03-10T10:00:00Z", "op": "INS", "target": {"id": "ev2", "name": "Park Cleanup", "type": "community", "date": "2025-03-20"}, "context": {"table": "events"}, "frame": {}},
        {"ts": "2025-04-01T10:00:00Z", "op": "INS", "target": {"id": "ev3", "name": "Pizza & Poetry", "type": "literary", "date": "2025-04-10"}, "context": {"table": "events"}, "frame": {}},

        # --- CON: essential (structural dependence) ---
        {"ts": "2025-01-05T12:00:00Z", "op": "CON", "target": {"source": "pe0", "target": "pe1", "coupling": 0.9, "stance": "essential", "type": "partners"}, "context": {"table": "people"}, "frame": {}},
        {"ts": "2025-01-20T12:01:00Z", "op": "CON", "target": {"source": "pe1", "target": "pl1", "coupling": 0.85, "stance": "essential", "type": "works_at"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-01-25T20:01:00Z", "op": "CON", "target": {"source": "pe3", "target": "pl4", "coupling": 0.7, "stance": "essential", "type": "works_at"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-03-01T10:00:00Z", "op": "CON", "target": {"source": "pe7", "target": "pl6", "coupling": 0.75, "stance": "essential", "type": "works_at"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-03-10T10:00:00Z", "op": "CON", "target": {"source": "ev0", "target": "pl0", "coupling": 0.8, "stance": "essential", "type": "hosted_at"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-03-10T10:01:00Z", "op": "CON", "target": {"source": "ev1", "target": "pl5", "coupling": 0.7, "stance": "essential", "type": "hosted_at"}, "context": {"table": "cross"}, "frame": {}},

        # --- CON: generative (productive relation) ---
        {"ts": "2025-01-20T12:00:00Z", "op": "CON", "target": {"source": "pe0", "target": "pl0", "coupling": 0.8, "stance": "generative", "type": "performs_at"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-02-01T10:00:00Z", "op": "CON", "target": {"source": "pe4", "target": "ev2", "coupling": 0.6, "stance": "generative", "type": "organizes"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-02-20T10:00:00Z", "op": "CON", "target": {"source": "pe6", "target": "pe1", "coupling": 0.55, "stance": "generative", "type": "mentored_by"}, "context": {"table": "cross"}, "frame": {}},

        # --- CON: accidental (contingent association) ---
        {"ts": "2025-01-25T20:00:00Z", "op": "CON", "target": {"source": "pe2", "target": "pl3", "coupling": 0.5, "stance": "accidental", "type": "frequents"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-02-15T10:00:00Z", "op": "CON", "target": {"source": "pe5", "target": "pe3", "coupling": 0.4, "stance": "accidental", "type": "knows"}, "context": {"table": "cross"}, "frame": {}},

        # --- SUP: contradictory data (Time triad — layering) ---
        {"ts": "2025-04-15T10:00:00Z", "op": "SUP", "target": {"id": "pl7", "field": "capacity", "variants": [{"source": "permit", "value": 120}, {"source": "website", "value": 85}]}, "context": {"table": "places"}, "frame": {}},

        # --- DES: designation (Identity triad — naming) ---
        {"ts": "2025-05-01T10:00:00Z", "op": "DES", "target": {"id": "pe4", "title": "Community Lead", "appointed_by": "neighborhood_council"}, "context": {"table": "people"}, "frame": {"epistemic": "given", "authority": "neighborhood_council"}},

        # --- SEG: boundary (Structure triad — filtering) ---
        {"ts": "2025-05-05T10:00:00Z", "op": "SEG", "target": {"id": "pl0", "boundary": "east-nashville"}, "context": {"table": "places"}, "frame": {}},
        {"ts": "2025-05-05T10:01:00Z", "op": "SEG", "target": {"id": "pl1", "boundary": "east-nashville"}, "context": {"table": "places"}, "frame": {}},
        {"ts": "2025-05-05T10:02:00Z", "op": "SEG", "target": {"id": "pl3", "boundary": "east-nashville"}, "context": {"table": "places"}, "frame": {}},
        {"ts": "2025-05-05T10:03:00Z", "op": "SEG", "target": {"id": "pl6", "boundary": "east-nashville"}, "context": {"table": "places"}, "frame": {}},

        # --- Frame provenance example: INS with epistemic frame ---
        {"ts": "2025-05-10T10:00:00Z", "op": "ALT", "target": {"id": "pl0", "capacity": 200}, "context": {"table": "places"}, "frame": {"epistemic": "given", "source": "manual-entry", "authority": "michael"}},

        # --- Self-referential: type definitions (Update 7) ---
        {"ts": "2025-05-15T10:00:00Z", "op": "INS", "target": {"id": "type-place", "label": "Place", "fields": ["name", "type", "status", "capacity"]}, "context": {"table": "_types"}, "frame": {"authority": "michael", "epistemic": "meant"}},
        {"ts": "2025-05-15T10:01:00Z", "op": "INS", "target": {"id": "type-person", "label": "Person", "fields": ["name", "role"]}, "context": {"table": "_types"}, "frame": {"authority": "michael", "epistemic": "meant"}},
        {"ts": "2025-05-15T10:02:00Z", "op": "INS", "target": {"id": "type-event", "label": "Event", "fields": ["name", "type", "date"]}, "context": {"table": "_types"}, "frame": {"authority": "michael", "epistemic": "meant"}},
        # CON between types
        {"ts": "2025-05-15T10:03:00Z", "op": "CON", "target": {"source": "type-person", "target": "type-place", "coupling": 0.7, "stance": "accidental", "type": "frequents"}, "context": {"table": "_types"}, "frame": {"authority": "michael"}},
        {"ts": "2025-05-15T10:04:00Z", "op": "CON", "target": {"source": "type-event", "target": "type-place", "coupling": 0.8, "stance": "essential", "type": "hosted_at"}, "context": {"table": "_types"}, "frame": {"authority": "michael"}},

        # --- Named replay profile (Update 5d) ---
        {"ts": "2025-05-20T10:00:00Z", "op": "INS", "target": {"id": "profile-investigative", "conflict": "preserve", "boundary": "unified", "temporal": "asOf"}, "context": {"table": "_rules"}, "frame": {"authority": "michael"}},
        {"ts": "2025-05-20T10:01:00Z", "op": "INS", "target": {"id": "profile-publicDashboard", "conflict": "collapse", "boundary": "respect", "temporal": "asOf"}, "context": {"table": "_rules"}, "frame": {"authority": "michael"}},

        # --- Feedback rule example (Update 2b) ---
        {"ts": "2025-05-25T10:00:00Z", "op": "REC", "target": {"id": "rule-flag-absent", "trigger": {"op": "DES", "match": {"target.flag": "absent_from_snapshot"}}, "action": {"op": "ALT", "target_template": {"id": "{trigger.target.id}", "status": "needs_investigation"}, "context": {"table": "{trigger.context.table}", "generated_by": "rule-flag-absent"}}}, "context": {"type": "feedback_rule"}, "frame": {"authority": "michael"}},
    ]
}


@app.route("/demo/seed", methods=["POST"])
def seed_demo():
    """Create and seed the demo instance."""
    db_path = INSTANCE_DIR / "demo.db"
    if db_path.exists():
        # Clear and reseed
        for ext in [".db", ".db-wal", ".db-shm"]:
            p = INSTANCE_DIR / f"demo{ext}"
            if p.exists():
                p.unlink()
        key = f"db_demo"
        if hasattr(_local, key):
            delattr(_local, key)

    db = init_db("demo")
    for op_data in DEMO_SEED["operations"]:
        op = op_data["op"]
        target = op_data.get("target", {})
        context = op_data.get("context", {})
        frame = op_data.get("frame", {})
        ts = op_data.get("ts")
        cursor = db.execute(
            "INSERT INTO operations(ts, op, target, context, frame) VALUES(?,?,?,?,?)",
            (ts, op, json.dumps(target), json.dumps(context), json.dumps(frame))
        )
        project_op(db, cursor.lastrowid, op, target, context, frame)
    db.commit()
    count = db.execute("SELECT COUNT(*) as c FROM operations").fetchone()["c"]
    return jsonify({"instance": "demo", "operations": count, "seeded": True})



# ---------------------------------------------------------------------------
# Embedded UI
# ---------------------------------------------------------------------------
import base64 as _b64, zlib as _zlib
_UI_B64 = "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"
_UI_HTML = _zlib.decompress(_b64.b64decode(_UI_B64)).decode("utf-8")


@app.route("/ui", methods=["GET"])
def ui():
    return Response(_UI_HTML, mimetype="text/html")

# ---------------------------------------------------------------------------
# Convenience: Entity Biography (Update 10a)
# ---------------------------------------------------------------------------
@app.route("/<instance>/biography/<eid>", methods=["GET"])
def get_biography(instance, eid):
    """Full operation history for a single entity across all tables."""
    db = get_db(instance)
    if db is None:
        return jsonify({"error": "Not found"}), 404

    # All operations that mention this entity
    ops = db.execute(
        "SELECT id, ts, op, target, context, frame FROM operations "
        "WHERE json_extract(target, '$.id')=? "
        "OR json_extract(target, '$.source')=? "
        "OR json_extract(target, '$.target')=? "
        "OR json_extract(target, '$.merge_into')=? "
        "OR json_extract(target, '$.merge_from')=? "
        "OR json_extract(target, '$.entity_id')=? "
        "ORDER BY id",
        (eid, eid, eid, eid, eid, eid)
    ).fetchall()

    history = [{
        "id": r["id"], "ts": r["ts"], "op": r["op"],
        "target": json.loads(r["target"]),
        "context": json.loads(r["context"]),
        "frame": json.loads(r["frame"])
    } for r in ops]

    # Current projected state
    current = db.execute(
        "SELECT entity_id, tbl, data, alive FROM _projected WHERE entity_id=?",
        (eid,)
    ).fetchone()

    state = None
    if current:
        state = {
            "id": eid, "table": current["tbl"],
            "alive": bool(current["alive"]),
            **json.loads(current["data"])
        }

    return jsonify({
        "entity_id": eid,
        "operation_count": len(history),
        "operations": history,
        "current_state": state
    })


# ---------------------------------------------------------------------------
# Convenience: Gap Analysis (Update 10b)
# ---------------------------------------------------------------------------
@app.route("/<instance>/gaps/<tbl>", methods=["GET"])
def get_gaps(instance, tbl):
    """Entities grouped by which operators have NOT been applied."""
    db = get_db(instance)
    if db is None:
        return jsonify({"error": "Not found"}), 404

    rows = db.execute(
        "SELECT entity_id, data FROM _projected WHERE tbl=? AND alive=1",
        (tbl,)
    ).fetchall()

    # Operator-typed absence: every operator implies its own form of missing
    gaps = {
        "never_designated": [],    # ¬DES — no frame applied
        "never_connected": [],     # ¬CON — structurally isolated
        "never_segmented": [],     # ¬SEG — no boundary assigned
        "never_synthesized": [],   # ¬SYN — not merged
        "pending_transition": [],  # ¬ALT — no state transitions yet
        "has_contradictions": [],  # SUP present — ambiguity held
        "has_destroyed_fields": [],# NUL on fields — explicit destruction
        "never_validated": [],     # ¬REC — not part of feedback loop
    }

    for row in rows:
        data = json.loads(row["data"])
        ops = data.get("_ops", {})
        eid = row["entity_id"]

        if "DES" not in ops:
            gaps["never_designated"].append(eid)
        if "CON" not in ops:
            gaps["never_connected"].append(eid)
        if "SEG" not in ops:
            gaps["never_segmented"].append(eid)
        if "SYN" not in ops:
            gaps["never_synthesized"].append(eid)
        if "ALT" not in ops:
            gaps["pending_transition"].append(eid)
        if any(isinstance(v, dict) and "_sup" in v
               for k, v in data.items() if not k.startswith("_")):
            gaps["has_contradictions"].append(eid)
        if any(isinstance(v, dict) and "_nul" in v
               for k, v in data.items() if not k.startswith("_")):
            gaps["has_destroyed_fields"].append(eid)
        if "REC" not in ops:
            gaps["never_validated"].append(eid)

    return jsonify({"table": tbl, "gaps": gaps})


# ---------------------------------------------------------------------------
# Health / Info
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "Choreo Runtime",
        "ui": "/ui",
        "version": "2.1",
        "operators": {
            "cascade": OPERATOR_CASCADE,
            "triads": OPERATOR_TRIADS,
        },
        "endpoints": {
            "POST /{instance}/operations": "The one way in",
            "GET /{instance}/stream": "The one way out (SSE)",
            "GET /instances": "List instances",
            "POST /instances": "Create instance",
            "DELETE /instances/{slug}": "Delete instance",
            "POST /instances/{slug}/seed": "Batch seed operations",
            "POST /demo/seed": "Seed demo data",
            "GET /{instance}/state/{table}": "Convenience state query",
            "GET /{instance}/state/{table}/{id}": "Convenience entity lookup",
            "GET /{instance}/biography/{entity_id}": "Full entity operation history",
            "GET /{instance}/gaps/{table}": "Gap analysis by operator absence",
        }
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Choreo Runtime")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--dir", type=str, default=str(INSTANCE_DIR))
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--nginx", type=str, metavar="DOMAIN",
                        help="Print nginx reverse proxy config for DOMAIN and exit")
    args = parser.parse_args()

    INSTANCE_DIR = Path(args.dir)
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

    if args.nginx:
        domain = args.nginx
        port = args.port
        print(f"""# Nginx reverse proxy for Choreo — {domain}
# Save to /etc/nginx/sites-available/choreo
# Then: ln -s /etc/nginx/sites-available/choreo /etc/nginx/sites-enabled/
#       sudo certbot --nginx -d {domain}
#       sudo systemctl reload nginx

server {{
    server_name {domain};

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

    # SSE stream — disable buffering for real-time events
    location ~ ^/[^/]+/stream$ {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
        chunked_transfer_encoding off;
    }}

    listen 80;
}}

# ---
# PM2 process config (save as ecosystem.config.js):
#
# module.exports = {{
#   apps: [{{
#     name: 'choreo',
#     script: 'choreo.py',
#     interpreter: 'python3',
#     args: '--port {port} --dir /home/admin/choreo-data',
#     cwd: '/home/admin/choreo',
#   }}]
# }};
#
# pm2 start ecosystem.config.js
# pm2 save
""")
        sys.exit(0)

    _load_webhooks()
    wh_count = sum(len(v) for v in _webhooks.values())

    print(f"""
╔═══════════════════════════════════════╗
║          Choreo Runtime 2.0            ║
║  EO-native event store                ║
╠═══════════════════════════════════════╣
║  POST /:instance/operations  — in     ║
║  GET  /:instance/stream      — out    ║
╠═══════════════════════════════════════╣
║  UI:        http://localhost:{args.port}/ui    ║
║  Instances: {str(INSTANCE_DIR):<25s} ║
║  Port:      {args.port:<25d} ║
║  Webhooks:  {wh_count:<25d} ║
╚═══════════════════════════════════════╝

  Tip: python3 choreo.py --nginx choreo.intelechia.com
       generates a production nginx config
""")

    app.run(host="0.0.0.0", port=args.port, debug=args.debug, threaded=True)
