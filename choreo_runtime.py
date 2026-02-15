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
