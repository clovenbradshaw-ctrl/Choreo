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
_UI_B64 = b"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"
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
