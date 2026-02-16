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
# Bulk Documentation Endpoint
# ---------------------------------------------------------------------------
_DOC_FILES = [
    ("OPERATORS.md",       "The Nine Operators — Complete Reference"),
    ("EOQL.md",            "EOQL — Query Language Reference"),
    ("developer_guide.md", "Developer Guide — API Reference & Integration Patterns"),
    ("EVENT_TYPES.md",     "Nine Operators Replace 200+ Event Types"),
    ("DESIGN.md",          "Design Decisions"),
    ("SYSTEM_PROMPT_GUIDE.md", "System Prompt Guide — LLM Tool Integration"),
    ("THEORY.md",          "Theoretical Foundations — Graph Traversal & EO"),
    ("FRACTAL.md",         "Fractal Structure of the Framework"),
]

@app.route("/docs", methods=["GET"])
def bulk_docs():
    """Return all Choreo documentation concatenated as a single text response.

    Useful for giving a developer the complete reference in one copy-paste,
    or for feeding into an LLM context window.

    Query params:
      ?format=json   — returns JSON with each doc as a separate entry
      (default)      — returns plain text with separators
    """
    base = Path(__file__).resolve().parent
    fmt = request.args.get("format", "text")

    if fmt == "json":
        docs = []
        for filename, title in _DOC_FILES:
            path = base / filename
            if path.exists():
                docs.append({
                    "file": filename,
                    "title": title,
                    "content": path.read_text(encoding="utf-8"),
                })
        return jsonify({"count": len(docs), "docs": docs})

    # Default: plain text with clear separators
    sections = []
    for filename, title in _DOC_FILES:
        path = base / filename
        if path.exists():
            content = path.read_text(encoding="utf-8")
            sections.append(
                f"{'=' * 80}\n"
                f"  {title}\n"
                f"  Source: {filename}\n"
                f"{'=' * 80}\n\n"
                f"{content}"
            )

    header = (
        "CHOREO — Complete Developer Documentation\n"
        "==========================================\n"
        f"Generated from {len(sections)} source files.\n"
        "Copy this entire block to give a developer the full Choreo reference.\n\n"
    )

    return Response(header + "\n\n".join(sections), mimetype="text/plain; charset=utf-8")


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
            "GET /docs": "All documentation as single copyable text (?format=json for structured)",
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
║  UI:        https://choreo.intelechia.com/ui    ║
║  Local:     http://localhost:{args.port}/ui    ║
║  Instances: {str(INSTANCE_DIR):<25s} ║
║  Port:      {args.port:<25d} ║
║  Webhooks:  {wh_count:<25d} ║
╚═══════════════════════════════════════╝

  Tip: python3 choreo.py --nginx choreo.intelechia.com
       generates a production nginx config
""")

    app.run(host="0.0.0.0", port=args.port, debug=args.debug, threaded=True)
