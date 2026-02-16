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

import sqlite3, json, os, re, time, sys, threading, hashlib, uuid
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

def _migrate_operations_table(db: sqlite3.Connection):
    """Add pretty_id and guid columns to existing databases that lack them."""
    cols = {row[1] for row in db.execute("PRAGMA table_info(operations)").fetchall()}
    if "guid" not in cols:
        db.execute("ALTER TABLE operations ADD COLUMN guid TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_guid ON operations(guid)")
        # Backfill existing rows with generated GUIDs
        rows = db.execute("SELECT id FROM operations WHERE guid IS NULL ORDER BY id").fetchall()
        for row in rows:
            db.execute("UPDATE operations SET guid=? WHERE id=?",
                       (str(uuid.uuid4()), row["id"]))
    if "pretty_id" not in cols:
        db.execute("ALTER TABLE operations ADD COLUMN pretty_id TEXT")
        # Backfill existing rows with pretty IDs derived from target name/id
        rows = db.execute("SELECT id, target FROM operations WHERE pretty_id IS NULL ORDER BY id").fetchall()
        for row in rows:
            target = json.loads(row["target"]) if row["target"] else {}
            prefix = _make_pretty_prefix(target)
            db.execute("UPDATE operations SET pretty_id=? WHERE id=?",
                       (f"{prefix}-{row['id']}", row["id"]))
    db.commit()


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
        _migrate_operations_table(db)
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
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            pretty_id TEXT,
            guid      TEXT NOT NULL,
            ts        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            op        TEXT NOT NULL CHECK(op IN('INS','DES','SEG','CON','SYN','ALT','SUP','REC','NUL')),
            target    TEXT NOT NULL DEFAULT '{}',
            context   TEXT NOT NULL DEFAULT '{}',
            frame     TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_ops_ts ON operations(ts);
        CREATE INDEX IF NOT EXISTS idx_ops_op ON operations(op);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_guid ON operations(guid);

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


def _make_pretty_prefix(target: dict) -> str:
    """Derive a 3-character uppercase prefix from the target's name or id."""
    name = target.get("name") or target.get("id") or ""
    name = str(name).strip()
    if not name:
        return "OP"
    # Take first 3 alphanumeric characters, uppercase
    chars = [c for c in name.upper() if c.isalnum()]
    return "".join(chars[:3]) or "OP"


def _insert_op(db: sqlite3.Connection, op: str, target: dict, context: dict,
               frame: dict, ts: str = None) -> tuple:
    """Insert an operation with auto-generated pretty_id and guid.
    Returns (op_id, pretty_id, guid)."""
    op_guid = str(uuid.uuid4())
    if ts:
        cursor = db.execute(
            "INSERT INTO operations(ts, op, target, context, frame, guid) "
            "VALUES(?,?,?,?,?,?)",
            (ts, op, json.dumps(target), json.dumps(context),
             json.dumps(frame), op_guid)
        )
    else:
        cursor = db.execute(
            "INSERT INTO operations(op, target, context, frame, guid) "
            "VALUES(?,?,?,?,?)",
            (op, json.dumps(target), json.dumps(context),
             json.dumps(frame), op_guid)
        )
    op_id = cursor.lastrowid
    prefix = _make_pretty_prefix(target)
    pretty_id = f"{prefix}-{op_id}"
    db.execute("UPDATE operations SET pretty_id=? WHERE id=?", (pretty_id, op_id))
    return op_id, pretty_id, op_guid


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
        g_op_id, _, _ = _insert_op(db, g_op, g_target, g_context, g_frame)
        project_op(db, g_op_id, g_op, g_target, g_context, g_frame)

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
        g_op_id, _, _ = _insert_op(db, action["op"], generated_target,
                                   generated_context, {})
        project_op(db, g_op_id, action["op"],
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
        g_op_id, _, _ = _insert_op(db, "DES", des_target, des_context, frame)
        project_op(db, g_op_id, "DES", des_target, des_context, frame)
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

    sql = f"SELECT id, pretty_id, guid, ts, op, target, context, frame FROM operations WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT ?"
    params.append(filters["limit"])

    rows = db.execute(sql, params).fetchall()
    ops = []
    for row in rows:
        op_data = {
            "id": row["id"], "pretty_id": row["pretty_id"], "guid": row["guid"],
            "ts": row["ts"], "op": row["op"],
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

        s_op_id, _, _ = _insert_op(db, op, target, context, frame, ts=ts)
        project_op(db, s_op_id, op, target, context, frame)
        maybe_snapshot(db, s_op_id)
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
            audit_op_id, _, _ = _insert_op(db, op, target, context, frame)
            project_op(db, audit_op_id, op, target, context, frame)
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
    op_id, op_pretty_id, op_guid = _insert_op(db, op, target, context, frame)

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
            "SELECT id, pretty_id, guid, ts, op, target, context, frame FROM operations "
            "WHERE json_extract(context, '$.generated_by')=? ORDER BY id",
            (op_id,)
        ).fetchall()
        gen_list = [{
            "id": r["id"], "pretty_id": r["pretty_id"], "guid": r["guid"],
            "ts": r["ts"], "op": r["op"],
            "target": json.loads(r["target"]),
            "context": json.loads(r["context"]),
            "frame": json.loads(r["frame"])
        } for r in generated]
        return jsonify({
            "op_id": op_id, "pretty_id": op_pretty_id, "guid": op_guid, "op": op,
            "generated": gen_list, "generated_count": len(gen_list)
        })

    # Return the appended operation
    ts = db.execute("SELECT ts FROM operations WHERE id=?", (op_id,)).fetchone()["ts"]
    result = {"op_id": op_id, "pretty_id": op_pretty_id, "guid": op_guid, "ts": ts, "op": op}

    # DES unframed warning (Update 1b)
    if op == "DES" and not frame:
        result["_warning"] = "unframed_designation"

    # Fire outbound webhooks (non-blocking)
    fire_webhooks(instance, {
        "op_id": op_id, "pretty_id": op_pretty_id, "guid": op_guid,
        "ts": ts, "op": op,
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
                    "SELECT id, pretty_id, guid, ts, op, target, context, frame FROM operations "
                    "WHERE id > ? ORDER BY id LIMIT 50",
                    (seen,)
                ).fetchall()
                for row in rows:
                    data = json.dumps({
                        "id": row["id"], "pretty_id": row["pretty_id"],
                        "guid": row["guid"],
                        "ts": row["ts"], "op": row["op"],
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
        d_op_id, _, _ = _insert_op(db, op, target, context, frame, ts=ts)
        project_op(db, d_op_id, op, target, context, frame)
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
        "SELECT id, pretty_id, guid, ts, op, target, context, frame FROM operations "
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
        "id": r["id"], "pretty_id": r["pretty_id"], "guid": r["guid"],
        "ts": r["ts"], "op": r["op"],
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
