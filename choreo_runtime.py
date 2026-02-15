#!/usr/bin/env python3
"""
Choreo Runtime 1.0
EO-native event store with projection engine, EOQL queries, and SSE streaming.

Two endpoints per instance:
  POST /{instance}/operations  — the one way in
  GET  /{instance}/stream      — the one way out (SSE)

Plus instance management:
  GET    /instances
  POST   /instances
  DELETE /instances/{slug}
  POST   /instances/{slug}/seed

Run: python choreo.py [--port 8420] [--dir ./instances]
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
VALID_OPS = {'INS', 'DES', 'SEG', 'CON', 'SYN', 'ALT', 'SUP', 'REC', 'NUL'}


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
            db.execute(
                "UPDATE _projected SET data=?, alive=1, last_op=? WHERE entity_id=? AND tbl=?",
                (json.dumps(data), op_id, eid, tbl)
            )
        else:
            data = {k: v for k, v in target.items() if k != "id"}
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
        con_data = {k: v for k, v in target.items()
                    if k not in ("source", "target", "from", "to", "coupling", "id")}
        if src and tgt:
            db.execute(
                "INSERT INTO _con_edges(source_id, target_id, coupling, data, op_id, alive) "
                "VALUES(?,?,?,?,?,1)",
                (src, tgt, coupling, json.dumps(con_data), op_id)
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
        # Reconfiguration — handle snapshot ingest or schema transforms
        rec_type = context.get("type")
        if rec_type == "snapshot_ingest":
            _handle_snapshot_ingest(db, op_id, target, context, frame)

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

    # Check for CON chaining: state(...) >> CON(...)
    chain_match = re.match(r'^(state\([^)]*\))\s*>>\s*CON\(([^)]*)\)$', query_str)
    if chain_match:
        base = parse_eoql(chain_match.group(1))
        con_params = _parse_params(chain_match.group(2))
        base["chain"] = {
            "type": "CON",
            "hops": int(con_params.get("hops", 1)),
            "min_coupling": float(con_params.get("min_coupling", 0))
        }
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
    parts = re.findall(r'(\w[\w.]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))', param_str)
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


def _exec_state(db: sqlite3.Connection, query: dict) -> dict:
    """Execute a state() query against the projection (or replay for time-travel)."""
    filters = query.get("filters", {})
    at = query.get("at")
    tbl = filters.get("context.table", "_default")

    if at:
        # Time-travel: find nearest snapshot, replay forward
        return _exec_state_at(db, tbl, filters, at)

    # Current state: read from projection
    rows = db.execute(
        "SELECT entity_id, data FROM _projected WHERE tbl=? AND alive=1",
        (tbl,)
    ).fetchall()

    results = []
    for row in rows:
        data = json.loads(row["data"])
        entity = {"id": row["entity_id"], **data}

        # Apply target.* filters
        if _matches_filters(entity, filters):
            results.append(entity)

    result = {"type": "state", "table": tbl, "count": len(results), "entities": results}

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
    if "limit" not in filters:
        filters["limit"] = 100

    sql = f"SELECT id, ts, op, target, context, frame FROM operations WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT ?"
    params.append(filters["limit"])

    rows = db.execute(sql, params).fetchall()
    ops = []
    for row in rows:
        ops.append({
            "id": row["id"], "ts": row["ts"], "op": row["op"],
            "target": json.loads(row["target"]),
            "context": json.loads(row["context"]),
            "frame": json.loads(row["frame"])
        })

    return {"type": "stream", "count": len(ops), "operations": ops}


def _exec_con_chain(db: sqlite3.Connection, base_result: dict, chain: dict) -> dict:
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

    base_result["con_chain"] = {
        "hops": hops, "min_coupling": min_coupling,
        "reached": reached_entities,
        "edges": edges_found,
        "reached_count": len(reached_entities)
    }
    return base_result


def _matches_filters(entity: dict, filters: dict) -> bool:
    """Check if an entity matches the given filters."""
    for key, val in filters.items():
        if key.startswith("context."):
            continue  # Already handled by table selection
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
        instances.append({
            "slug": slug,
            "operations": op_count,
            "entities": entity_count,
            "file_size": f.stat().st_size
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

        # Optionally append to log for audit
        audit = frame.get("audit", False)
        if audit:
            cursor = db.execute(
                "INSERT INTO operations(op, target, context, frame) VALUES(?,?,?,?)",
                (op, json.dumps(target), json.dumps(context), json.dumps(frame))
            )
            project_op(db, cursor.lastrowid, op, target, context, frame)
            db.commit()

        result = execute_eoql(db, parsed)
        return jsonify(result)

    # --- Normal operation ---
    cursor = db.execute(
        "INSERT INTO operations(op, target, context, frame) VALUES(?,?,?,?)",
        (op, json.dumps(target), json.dumps(context), json.dumps(frame))
    )
    op_id = cursor.lastrowid

    # Project synchronously
    project_op(db, op_id, op, target, context, frame)
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
    """
    db = get_db(instance)
    if db is None:
        return jsonify({"error": "Not found"}), 404
    query = {"type": "state", "filters": {"context.table": tbl}}
    at = request.args.get("at")
    if at:
        query["at"] = at
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

        # --- CON: constitutive (high coupling) ---
        {"ts": "2025-01-05T12:00:00Z", "op": "CON", "target": {"source": "pe0", "target": "pe1", "coupling": 0.9, "type": "partners"}, "context": {"table": "people"}, "frame": {}},
        {"ts": "2025-01-20T12:00:00Z", "op": "CON", "target": {"source": "pe0", "target": "pl0", "coupling": 0.8, "type": "performs_at"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-01-20T12:01:00Z", "op": "CON", "target": {"source": "pe1", "target": "pl1", "coupling": 0.85, "type": "works_at"}, "context": {"table": "cross"}, "frame": {}},

        # --- CON: associative (low coupling) ---
        {"ts": "2025-01-25T20:00:00Z", "op": "CON", "target": {"source": "pe2", "target": "pl3", "coupling": 0.5, "type": "frequents"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-01-25T20:01:00Z", "op": "CON", "target": {"source": "pe3", "target": "pl4", "coupling": 0.7, "type": "works_at"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-02-01T10:00:00Z", "op": "CON", "target": {"source": "pe4", "target": "ev2", "coupling": 0.6, "type": "organizes"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-02-15T10:00:00Z", "op": "CON", "target": {"source": "pe5", "target": "pe3", "coupling": 0.4, "type": "knows"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-02-20T10:00:00Z", "op": "CON", "target": {"source": "pe6", "target": "pe1", "coupling": 0.55, "type": "mentored_by"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-03-01T10:00:00Z", "op": "CON", "target": {"source": "pe7", "target": "pl6", "coupling": 0.75, "type": "works_at"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-03-10T10:00:00Z", "op": "CON", "target": {"source": "ev0", "target": "pl0", "coupling": 0.8, "type": "hosted_at"}, "context": {"table": "cross"}, "frame": {}},
        {"ts": "2025-03-10T10:01:00Z", "op": "CON", "target": {"source": "ev1", "target": "pl5", "coupling": 0.7, "type": "hosted_at"}, "context": {"table": "cross"}, "frame": {}},

        # --- SUP: contradictory data ---
        {"ts": "2025-04-15T10:00:00Z", "op": "SUP", "target": {"id": "pl7", "field": "capacity", "variants": [{"source": "permit", "value": 120}, {"source": "website", "value": 85}]}, "context": {"table": "places"}, "frame": {}},

        # --- DES: designation ---
        {"ts": "2025-05-01T10:00:00Z", "op": "DES", "target": {"id": "pe4", "title": "Community Lead", "appointed_by": "neighborhood_council"}, "context": {"table": "people"}, "frame": {}},
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
_UI_B64 = "eJztvduW20iSIPieX4FiVgtEBskgGReFSDHUmaHIVPQoJZUi1Fk5kloHJJwkKkAACYARwaJiTr1sz+6cOXPOzvTuvMzZPfvS+wW7+zD7tB8wH5FfsJ+wZuYXOG68hEJZ2bVVlxAB+MXc3Nzcbm7++DdPX55c/Pjq1JgmM+/4i8f4j+HZ/mRQY34NXzDbgX9mLLGN0dSOYpYMam8uvm0e1eRr356xQe3KZddhECU1YxT4CfOh2LXrJNOBw67cEWvSQ8NwfTdxba8Zj2yPDTqtNjaTuInHjk+mQcQC4/XcT9wZe7zL337x2HP9S2MasfGgNk2SMO7t7o6hi7g1CYKJx+zQjVujYLY7iuPuk7E9c73F4On3O+e2H/fcxPYa15Np8rftxl673W839unvAf09pL8P4W8H3z8Qlf+OJd9EtuvHO98HftCj6lgZq2JFqPbAcePQsxeD+NoOa0bEvEEtThYei6eMJTgmejr+ohcFQbJsNoeT3pftEfx33MeHbu/Lzj7894ie9uDJ7tjdNj3t977s4n/wWzyPxvaIwXfWYd399A000D2E/zKsEkQOi+CF3bX39tULKLJ3sHew34U3gMLel+wI/uvwJ/hot+32sM0fAYJD+9B+aPNHAGEf/4PA2qMRzCZ83zs8HHfUC2jgqHM0Gh+pNzDEaDK0648eNTrtbqO732m0OhZ8nkSM+Tio0cEBk8+y9N5+o/PoYePRvigcREB9MODxwSPWHqoXsnh3/6DROThqdGTjEXNgZGOElj+pknuPGodH+D9ecLSwAYj24fDQ2RePsuhho3MEAHe6omjI0TXaP3r0SDymrR42HnYBhD1Zdh6FHsBrHx0cjB+qF7J8B/o/OgBkPBTlF8zzgmto3R7utY/Ui7R9wMfDRw0JNVJ6z3z6vYHkbDZi+NuMWeTizMyQOk1FrQZSq9nAt3EIJILo6B2FN/hvt9fphje3X3y1nNnRxPV77X5oO47rT+DXMLhpxu4f8YFTDhAQlB0GzmKJ/Tf5uuhd2VGdQ2T1h/bochIFc98Rr4cTqz8KvCASz4Azq0+1oWnW6+wBILCSWXPKXFhQvU7roB9csWiM2Ji6jsP8vvzUbl9Nof95kgR+BgLXn8LYk/5oHsXQUxi4wGqivlgAfuAzHTB65jCJirdfuH44Txox89goaSTsJrEjZpf2kYO9MLYSDOxZEpROeGPEgec6hvhGr+XnZmQ77jwW9eC1nItDqNZpQ2/BPEFk0RAE0L1xMJrHCmb+uBQN6tDx1WjxaZ3aDuC3beB/gQIMvQhM2e0XvV7zmg0vXRjrKAo8b2hHS+LVvQOAQ8wI/LwtKdhMIkDCUsMEvPCB+CJovbzCdD4bLouoK0XPHlLsl3YYLgW37Y09dpOjklbsOiwFuruH6MNyMPYI13G7bKa6aV+8sYr5ynRMrTpuBKTjBn4PcD6f+f05LMcmJyhOcZKqm4uePU+CFMSmF0yCpZzrDk02/dkHkDM92Z478ZtuwmZxD2cKaHxihz0iDbVEYXXMqgCHTrGzJqz2S4mZo3Q+6XcW2fQmRRRSH0A8wa8AQL2zd+CwSUOw5YbguNZasP8wjxN3vGgKoUC+LrIVZFuCYVxzIHFf1hbhvlqEX47HYzlAXA7LFbWQiD2WJDhHwBQR8c0WERavH8+HS614u7jS97JQoRCQa7CFnSAgTSL/cRDNevMwZNHIjlmfM9xmEoS9JvFgRQ4xJ6SUInCBwjwY+2XFmiQP5YHVQUNxphKMHMyd4kD3Uz7UJjAOM2DgtC43oVKkJNnQQ8nQssR2KCGv5K+wQHNMngbl0sqzPc9odQ7iHHS9KS69Eu6CjDnPv3N1WzYg+YoVKytW2S/y2G6+FaPlussczYrV15Y0QlhT6yDDqCpWRbGTkdjGmx4bJ8RldHR2JHZLFlhxzsvRpWiS00Fu/vYLlCHwR7BpLRYFwoO1iHTcK/gVLSWfzzKmLH8W0swhXzhaI5w9LrXFR1iSw9L4H36r5qLj0Vqar2Bxsqv9Kgzmp2w9vW/ENoELANwzkAiXCHGvs8kmlpfEZoA0TrjYGKAId1h8KQWC7gZbVspNuhtvXHyf6262hyvQdN6oMcPcPpBpMa0LW0CBtXbL94FcA1dJljhWiYT6PisRs6ewUomOQpdDtV0cSNFhJW/t5jYJVGCLI8sTWzfeSCBBrF5P4TXtKyiuXkd2SGAKZlxg6/SxkttK7bZEl8iIs4guwJ5B/KXdwP+2dHogqa7IJDfdvvJYH4ZVG4OlySRZMb44M0rcXzM9ZSu/FMvDsGrL09jqcJKfhL2cwtHdGNI1xLo54BMJeFF9EcaLAgXw6Y2byIkEF9rfL90bNOl+DbPZkoOV8yYFVoszT8UTuPKGHz17yLxlnt+vlDMPthDmWpXrEPrmOprYCnT5PwzEXEXMs3E5rkMHby+yXU+oFKCC/Y1ssmIq9vP8r1vac3bv4x2NXc9bpsre36xSTx61UTvJLEzxxM0xVhkYGrnSeIBgY9711PYd2BDEKPdTxamTGyYt+2zLBwCpGqA9BLKbJ6yPUgZ+SWeTfsHwWb0JHxr4p4TJ7eeY3L6Si2HsQ30IyOXSETjQcInxRhMXyjc6yTiKc5kKwtr2VSXcF4lREyk6+6XCsFgquC+UCbPavq4DTMbE9SsZly6XWkUfRssJEjHDh+kEH5aRseijOM+2785swn4492JmdGPD9cdo44aF/7eXbDGO7BmLDfq6hFlGOl4GuGyTRa9ze6A9tfZuATohRMr1WrQlAB/RhFgyIODbqFpM5cwqsaMkZWBKvG/nNoA1DFOjtxRLSHWSKh02tudeQiCt0sewQLIJgZYrqoKQukVjjxgZifVc3WbBpv3k6UxiSejlJSI8h6Ocngu7RBA2z16cL0uot5zi0D6Hlb5+fpGpxC3iJbWkqdzinZ28fJGphybvklrcEs67evHmeaZKxJySGmRlF52cn36XqSH4bbGStIvzjs5/zMIWlttUuelddPX0NIs8bjkvqSVN6qKrN6+2QQN19fr0ZLklW0RKG63f41eIvOvJLU9RzJGMYq01RdfooJ4RJ1HgF0TDggyCKnVynTFa0itdAkiFOM8OY9aTP6igkUyX2tLAXaqfYWDKirUZvxf6yjaSUZa1dStYW6n8hPA7y1XwlmiKG4ufZbukfSOmqXtAjoCcYk4DVy+Bzt0wdmMBqeC4CHI50+WllgVpKy4YQbdnhVXGK2gcaDzcihNBlZEXxEDeWzCjyUguhqJ8mUMjlP5ppKa1i+CniD/KID7deX8aGdOuhqduHk9oehYqp5h4vlqw4t4yb4zIIVhYslAyN9pEZbxmuFy/tDN9EkVmPW0PsanhpuJgtu7RPTq6NB6HgrTROSLEZ6A/1HF/w9V2fZ2EESvVDH8aGq3L61KmfUsfg7Bs581bzqhonERlGy7/OJolea65L3GIPn+KOXBHvPDYc4rbHEK7EQMtwQ3VXctTu78OniqAzTHQ7r0xUGzfrTQifTqNHpaSJ/VqKN9xURU+bFezw4JF5TN4kblQv1/Eq9WPGHUN6ytxR7aXG92RHFzrp+iOsnmZhIM6432NSLNBk9SNbFs877VL+HaedzTFxu6ES7VLjN0b5pCG3u5zdzDGRBBG2lLC5xbsVaYmsjJWje+PgBsH9yZp3suaAH5fR/rJqFeqlNHqHsQGw4W43pBPIwNWx/xlaTdti0o0p1k39Bb2+W29IYT55pAl14xx8JrrHLa3WOhG3/eOSglN97CUyiS8u5tt/YIIoj2Mc7b9jbCTNRiqtpZFQt7aRF/FcbWQi9xybha9SgoiaYEvV3L0rspiS3gzPECo1FDRL5AXVhkvc9x0nyZ73CzYRtegY3/L3Srbrez1ao1oRcO8g9ObRLgNCSZLBrfYX8+z46Q5mrogO2QbEQZlJ+eTJE6AfK1HKgSCw7cpZzQqUr0QsvtFeVxfT+ud9qXTlCU2BKC4+PIhdyS+uT6MOXRHl9mlcrQpX9KbMHg0jr4z51ab7v44qtyFS1xQd9nEylQCgnWY+Dkmk1opVQFDhsEV9Py2NpCDMva3Dt4Cjynpd1vema/fCiMXELCo9p+tiGDLhPtUtbyBC2wWOLYHIOe3fGiSJVnnsm5rP0z37S4Qt+7W2TwOQOu+FU+D68yEy49l+O1+svzaTS2UB92csHTUvpqW6LgEzcbaak4q5pW5r0uOcugFo8v1MmOBQW/F31XfIq5TPPFYT/FQJq8XFWfVkiqu8LW5TJ8TshUFjDEUvLDekf/kSYc4OvMdPXiMC+dQwm9Wey4KHgrZneuTWs+nQ7QaScedbDe4LBIi15NveQEWlSw0NMuI79e2mxQLSPX5i9Y180bBjJVsX4WIlM0DbXQXISKUyF7tZVz0EB3nDDh5wesh2Ttl2TCv5u9lZjprNDpol/sAssaUw7T5pk3DLdkAiIYKE5+rJreFzG7Z3SzmYL+4c2sdTCLXUUDhQx//NGEiQtQhmnx+4l5nHBnwfx1kiYx9Wi3FhVYczciOSmyV96rAbxRGqMNTHZqgtqxVTgC9KWO6n5cxV3PS/QJ2sgbAcu6ZtwXJGH+y7DZJG9zO6QS12FXgXbk5D8EGnqfWaIo/neaIeV5B7NOPb7SPAF2Pd8VBmce74qgRahXwj+NeGa4zqNlhWDv+wjDoxQhE43hQE0F+9L70C0Va1471LyocunZ88ngXPtDXYhHEZU2cRUrLZYrE82HtOJLHlKiI9rcAk5JLBbzwle9TND78es68mhH4HHMwiGs3GU3P4IPtj1g9mbpx68r25syCIQUh0q5Bz4Na7fjnP/2TIZsTNQx493iXFwSg+FfVdx4yFKkUZPCdMxZZRAhZBJ4HYwDoGHOesllQB2h+/p//T+Mcng1QWwBbvGqxLVV3BDtqwtTIoIUd4wW73qCmA4PI1vz5v/xP+Xr6BJTPRS6mulaGldK469rxSxBAbM57nwcTrf3y2hSpy5VsDXm+fXUR1M04AUTMzIYJLMhs4ARbNaIG+P6158E0g3ij5sB1YbT/w/8GMwkvjw0oYAQpMNmiI95OEJ7AokuIPES9z4kcgxYxzG+W136SrUjDWhJMJh47513WzVFyYwIBfLPgRw1vklZiDz0mMIHDhxInU3al4Eo56KPwBsn2/y4iRYxTVD9DOGt/QUgLwlggjdNOEGn4go+fgi+oLvGFr0QjussQxROdyWjFMn2VGHqyGyQdIytqALXjs6enLy7OLn7MQJjtaXOhRgk2ewXj0SF2lllz45HBIycKyH8Z1k14L1f4MfyWeCw28PT0vLQBeK8agN/VDZy9KG8A3qsG4HfZTH6WGTl/9fXJ6Z9rOs5PvytFBrxXyIDf1dg8efmitAF4rxqA39UNnP9Y3gC8TyH48cUvNh0XZ9/f/2yU4v7r5xelI4f3auTwewXq3rwqR92bVynq3ryqbuD16UlpA/BeNQC/S5laQZRbzffFaZValfRX2B82EDXOFzEgvyh96rJFUaiYBsFlDDKFGmFRiPi3/14KET+wIZbftg8W/OSt7OK//VfZw+nL3z3ftnk7dFe2vrxVQtCrM+M8ZKNte3BY6AWL1Vj6x/9R9vKUSq+U7rMHjsqnl4NSWM7lsY1cgBNVzxM7mcc5IKUNyFA2F6Q+DjHFecKi//lP/5wDO/2hQYjHhErUKH6KolSW0Q/ccFBDe8IuOOHqalN1XXngJq1+jhpVZbWrhJfEFBMXtJZrea6FNtlqJeZqqCRxx07sJjaE+RpQCM9oNsnfwxclnlskdhvn9FSto6hOcq0D6yxvHD5Q2//ufzFwftm2TU8iO5yWNU0fqOn//B+M7/BhhYZUOjVkEtQRSTZUie3PduS9m5qLDkuO4OYtrDUDJn3EpoEHLQ9q37oebHetVouTyZgezxBwRBKNYFCLmA+FQXE0divxPAx5C2zmJt8kujCNlvtTePs9GnKxkVL6wxKw/WyvlvLzIwYPTuMwJPE3+grU1788UFI7fhUFf0DN307E+i+bWXEEJMuaxEEO2ddr+h34s2AeMye49omAo+R8FM2HdXaFZq9CA3hAQzbwLf0WWEktf3JV52ry8xWy7jNVkcdQ6PXyrLcwNoeWGW/oKf4+9oPrVRU84gO8wnP8nWWuwFdTbvr87O9P18+e0MR4o/JhDeN1BKk5YQn7xQCNLLbRaS8rCF5Lmj1hJ0vEGPGgUS7FUT5lCUxw3mpSMRwR/lAEwB4WDRrAe55e1M2xyzwnVnuqoEvmSAaXpwHeXLEdqA6a6UI1lBpdNm8Dd0UuT8W6pC4Z4oq9XMQ0SER/g78L8yj/0XdR4eBLuQfxCQ00d8wXUQsW1YQlg8GAACPTHjXyHAbeitgsuGKwRQC3QTae36ypUQH2dI+zG4Whx7vwin8j7iBwF0SPd/kz/6aZHhHQl2HBpoh6JCqKaEYUNsXEpVmUJsVseRT3UZ6n8jbyXn9FadSTURGm0g7DMPRFVVnUwWjqsKyY1qqyqD6jfizbdSeroEANA1UIKh3PAVMhLJPK0qDKoa7GSy/8ZEoevsrioHuicsmLs8kMJr6qLGoqqIpQ2YjBIMfuZB6xCvutmNoLoiKj/nfnL19Ycn4fS5+lmlxeDMTmmuvUekat1jBqmK2LfoMoLStk2j7hNrW1jYtyinfLk+Fk6VnWyCS3sqNv8WzU2m6oVL6Tfd5JseXCokSfay3PInGlppblYDTHGWoBqk49hj+/WZw5oOvIdWxaK9boCdqjvZSnFiWKlD/Nh9AkrlrkxPhvkRXnmEw8itwQ5h7oIk5Q6RmYZv8L0O8NWKAXA3/ueY3RPHoFMvRAWZXhxbcgHiUDbl6GRxQP0+8xbuqDTiMIueREzfBWeZETezRlg7fvGySr8qflbYOmNFYf0f3xmjwWmBmgR7DM7Bv6cduIY8bBc2jvOQUekiwEwHwlM2cwtr2Y9b/4woZFNTLGc5/YtgFqYD20k2kDubG1BLQk0WJJM8wREQzwy5PljCXTwOmZr16eX5gNdCCxKO4tzRO+CTcvFiEze6BWhjADxCV3/xAHvnlLLfeQ9DDYG+RQd7yoU2+3veVtX+sqGtioYRljloymdZiBHYIssHghYOy/iVrBJbDyKLg2fHZtnEZRENWjVky6mygXsWQewcioraiFUNTp0y3ABQ0za4n9BR5rMapvQlc9s0GdMQzcpfqE2y9uCxiDKRldnqiNr05I4wNg3qCSxDNapknw5DAth49TYu6a6aijBw+iFqX+GwzMbOo+01oK0Yt5LRdgip5dfP98YK5QYoPLVOhS5GH2RTMpwSTRnPUlMumBitxmEIlPG/cM2C50jRgdA9EKEPL0KvsX1FucDS+wHemqivW5yGJT+usE5nmReNV8CWehXh5W9wDqcAchvo5zQ9/WYUhDlhOsQLS0361xEJ0CF6i7g+PsqlSAc0efgB2dD9i0pJ6AAztwW7E3n/SDFjJxsWTFyx3TqJs7biv1c8GbIIwbBr5lyExchu/kT0sQC44eljsoeycYQFnn6/TWEoMCbFkKWwN4wvdiOrMDfqIP2GP+BE/OlU12zluL0NOMI2fgD4pV9zP8tJ9ltv0se+3jBvMDDwSoy/WPlE2tEeLgYeXmBSosbF20c7ZkvkrTXFktNbEUa6JDimoLJsbGEYunoHPZdUtbJufnp/y51NdZ2TP3fJqWVYpj5XO+M1/bmCHk7GnQM2wP5OD++U//bPYr1jJ+38XCZmOpqO03kbW8GyPClhQLSuf+HvgpBzrHoVbShGI6YtWYOFatrdwC4J9LpzHv/k8nE+l5EEbBLARuIQtQZlmj7gXXPPSvYfiBQe7O2Oq989/5pzf2LIQ10zPwM+aJhSKgxEFDsLRpCA1jtsBnVEdNNS+0LjlaNU7qMdunldVKgufY6Qm0WrdA5CNzU3337T/YzT+2m48+NN/vThpmU+fDVay9scQWe9S4RhgfP0Z8o7eWtseipB49ES965rc070YSCIQpPm3qfODTJ5JgqpxH+lo6jflYDMntkDNpWMVXpM9Es7r5lOqkO07N3MHiO2btCc7kBSjCBhetYwPUKzJ3DgH9xhhwYcAuMLN9GIi3aAGLSPvIy2Ya5nd5Dw0lJD49fX56cWrySUh58ipUZnlwCSoyTLACD4LCc5KUGP5uusMBqQRhT/h7SXPrLX+as2jREzy07rnA1QfddtsCCVbEPvSWCQi5PZNKmreKxGLYzuJI20Ctpb7fZD71sXz6UWx3XCIFyAdvW62W9v19Kw6AYut2Y2gNju2W6zSH8MfqK42gBQrBIH7bft9KYv2tfQNv5Xba7ODn21uFIr4BDlCGPmegI+kbpJI5ghCEDlF+EIQtgYYHD9LfPBwEBwWvf5O0yJIZ/+Am07r5AciH99OyHaeeWICwzMYLQ+XPYpScDjL7NoBSFxAMPSMYG1oDEmkVs00NwSNUtBBAkEZU02/h5ftBhPiYh2jWPOe7GIBQuaRFnA8saV2GKs4l0a6i2lzzm+2pMigGmBBqilMhSmhjV3MEA1GTNBrkRvgk9wyzBiMA2QrFM1Am2nLpTHdgi9vUsffOxIjR5J3ZeGcSenfMdytcfP/538rdURQuhFEdmzujHTPjIRasI7MJ5zCry0CCHYAibMH/W2SIrdOsl3MIPPZz5pRM3pPvQQnExVPPrkN4FdaDwXGAi89C6RQoFdVuVELR2ngezCNg0JwxajSILTzB7j64zsDc4R1bojquC6qN5g4G40QqMxssJ+uHA9KeQ0wJXwcpEThgdsHO/XjqjhNYsJauTyNtrFy2UsGEgrh6h15h/UrtEjGpk58LJDF3gHfj6rKW+qdwHhNZZha7WOC3orkVS7UFW5JfjwAF5Wu2n1+w0tt0K8Z+yzA9EFdAyBgzUOYYS5aViopOT5y40UXaIPpuMI8vV2HzwS99Ze+hIh8/kqTdz1l3NLmAtopzUgGDCITuutkaj4CFyOU7wsUrpH+RiBoXe+7Vy/EYLwMwxapY1bq+crV+QH10Syxr3MdAmgDNMQwZllxaDvm2KpQun6ukmnfpak2DDasLKrWpEc9XFBM+ayVQ4rM+pcuVNcmBk2PZZjaq0+xD/9kCJnIOYhzLq6RcN2PDgrZ3m7f+9VdN1NVQm57h4HiooZ1H0SjMN4a05oEEWlc5gra4qsIJXqKGaPMumKGKRXRkQz8HKFBSSZAoPx1T6KH/rIiiEACFpxIw0aFcBiZ/X04cEtU8EMjangZFUBD0m5s8jMu5Q3sySKfYoAjDuUObPCRHtLh9dYxKMoWIxZ3bKbNG1quZHHhQxZW1lIRx9flI4gq4XQlEGjwqiC0IaRPAzU+yeCvl9Up2hu/ybWtqx7gNW0v1hqtwtDczr8jq8+9SXk/kqppBVlzeRno+HY9vVrfYCW/MWx1WdKyRFJjdv/JOjn7WydHfis3ddT/6BVhntahfRdIST09ATAM+aO6gCiNfvm/9IXD9umnsGKbVK2wy6ze48q4ys0WWWe5qrseW2TNXtLvevrh6FaRfqzcSipzJ+Owk/nl8jfKI/Uax4gcPtFciikoXwyQky5zwnjMQcPnuVZYNS+syNfEM3xVKEnvNlAPOWSgleWamIGeHhbIUp5ktiZxPlSvKoFpJHvBWUpZ2rVxRTGCaLykwmClJ0Rf1DFYzyFve1SzNd8Q72cLXV63cR7jHa33l4iJaY4EX8UNQSbf0amqwOBwIWu20eyzQh7ZCGXsJbx+Hx6cvmz6lPjMo7sTAsBrWMl4A61WrNW6AFs0M7i5pBr63MLxg0jAAmmhClUDDmdktVCajhTGbJ8Q3DDc2QFVWfIRCFxgVIb5I3+eOS5JZ6/FueFwCvjzHWvTNh7X8ARK6F2MvlzYKY9qrD6U9D2x+KI0MiZXe+Ul5X91cgpb9TF9lh9hOuKGWzcJkoYycOe9+GRYw0L5W+gXPflYM8PF0//j//V//w/9hvMBoiGEQAaeRR/DgE8z+EQ+QhAmGXywIPdYw9jklwLtOx8CgGuZMsARGwfDwrYZhGzIyB22wl67voHlrxuJ4AWQhToSCvjWbzX0XRnr+u+fGyPbfmYmBwZh8sisHmxtSEY98YP/pn43THB75qM7RDmCMo2Bm+EEyBVCANHnIpToNN7P9OQgEAC7GBBhXro1RE0DokXHtRgw2EBhkPAJmhHkSAxpn7Nsh8CL0hwJGku1GIc1PyHHJ+gR/UGIRw/lv/5UC4dFQzIARpoP5HS0VvGpvDmwC+UHc4nbGutUQcRj4C2cqiWxYXrHtNdTabaLV1zHsYYyNbgfyNcxrcE1ZogDw3XemgHV5S2H1wAzoLFosQSVM7vbkdGgmaxEi1no8jI6/O80U4iOgAiDyUYmLqZ28M2PDzaBY+2tmjR/jpJ7E1lKGPoBoiwGe+I4cNHiD4IU7Y+cUwAFs3m++OTcby2kwj3qmD+w1ckdmY+b684T1zG7TcSduYt7q8sTYWdsFvsh3MQPeMO1hDFCUmA3HXmj9LZitdZ/tbaz3xvsG6Qk0VxppUeKQO/ESLa7sKima4vvc+MFjnqsNFlpMtHT/ZJ1cYgcnkRqFIBSqLeoR/7R4/TobHLPUdMeyljvY+WULGfUDbXyauHjcLm03o6yAmgDqCjbCC5TWyEXvMCvntlPmQNGG1d/AyC13XzLTUofSGZLzvZYcdKxIA1084XH8ItDF8J0S5wuqQE9MYwHzTsGPuHKNmuB2NeRc0Clu3yhvW2io5gtI+AeFhV5nBhgvbpLp3nNIxyNsCoWlzhqupWz2TjwA4gQKiQkPTgwU4TnWUlnkyzZN+lNyrc2axJP7mybL1XJIAMbcJ51urw0Dx0BAc8eJUxTA8Jz4NrU216tJ1vr4EQ2bnCxcB4tyzxuW5L/Qtq6VooinsmL4AQpCK4RjHvWGPnrKswwgYgFyKIgXiFmkcxSY8WSc9ByNpoOXQ3RXty7ZIlZdWZLyLwfHl7hCXQdWMnoA8AVwkZ//8T8CJ8kvClH77eV7yxK6YANUwX4RPoBcAw+aGk2V1VrCiYcfrWJV1U0rJo8D4svSG/v5H//JKCvO/ykUf8xmx3UolxYcBfPQgzFZoqz1eBfKpHYlCSCeti0BMDc2LfQYoyLyjeAxw7WNtDLwkQAlgaP2M5+Bkl0bdtSPH9++tyRHgYLyvaQuvkFDRw8eAOWCRi+3bNyrRRA/+rcIlKyvy6qR5p13mrGohlOJreyYWUcYRgbT1kMLPfV1Zcogo2pCh4CaHRPL81+lZclpRuCUfnZoodIgciX0k3Gr0yuVHA7az12SVDv+EqEUk5X13hF2BJvIuvHK9l6SxZbbbBqfcTN+8EDzOsmP7yV4qOUMygoQM0Kxf4BFVOQcEqK+vWIB/PNJ2yttmf7n2zIl8Lm9TiBdDxzgQxF7G4wlw1BTM90ld+hJdvrgwW8uC87GMQ8UuLRurXR+UVsiUWwcv++X7LVxcg0iNXdNqFf4hnLcPE4i/HnsOo934R+zzxtUYI0BLKJU/LgzRjrGX4p+sfqubIrS5ZiFEYsGIqOShbQ0HiLJhTMSaNXZYk2Wn2I9Thdh4pSPERF3NWBvx0SKVwOy9FpXAxP4Z8qVUdEBJfQKOXNA8wgTddX6EM9DLJvhMfHQ4Nnma8c///f/O6m2sI/wwoIqae8gtrC+C4CnoguenR57+e+MNKvDyhahpdySurL63PszEPrFFRd5pbENQxeXHDPQCkyfWQENfqsd498CIFSXQ1tVW46F/ytbuJWQ4FAIHWrBm4DDtC2ZxSSfThQp4ErxYPOWE6Qj3+IvnaK1ByJp+BeXT2lGERI120Z7LRVWZeyrHXNNu8SJmC6FmiVkKo2naRHHWcU1FxRSLYYTazD768Jm8gwd4wyIwVKghQgdWc3bNbD1sJr75vp/ZvbbioG9sXq7cWD18/pJNjMGXq2QzVBTmWpnyMNJmUrQWMiTnksrt2E8EjG4vnQtvuMmdc5yC4JQea4+0KDKpawNswtUr4hqOt+qF5l2FfMo/MeMAPbL74cpadx9a1Ti9a9+VyzbYBQK9tsCW6s5cJZtHB9YAlc0eBgOTucAuGRdAC6opQPqgzKlbyDOFa/L6Ou8vUZKYgpH8wCVpVkQMUprAYBzgMs3DCXt5+PDhFhaZcXYShZFjovWGcn/t9EshCtsK82iRJQWC2kCatfjke1f2TEdmpyc0O+Sw/h6plQ8HsArqS1MxmtWQiNaFsaxkdxQeL3QjgajFs9DL2o1fhjAcwsWGTz8gGA0nmlvnhE0DSeMBsIi7bAroNZX7g3zXqNt7OPHTn/UogEMfvgKCsITH8PgmXjkHj1RBK1BaZyaLCne8tElNwAlDEycXq2bXQf1tuSmBVz5QqYbrkPjlANa/MtLYER89BqWGiWI/qHxTFRMouCSnRO2TZ7l8uCgIf/fbrU7XcukkrilEB4GHQoixqV+Mzhs928e/9C/2YFfQBRQbshgs3llwx7Ie8A4BNhTbiQg2A49ZyCoW7ey0QU2unj8rL9Y22i7scg0+oN8Vo1yzJ28fP7y9flgyR1LPfPL8cEj1h6aDe5fghfd7ujggJkN7maCF4d7h4fjDpTgxy/wFRvtHz16ZDbiS9fz6IU93GsfmY1RFMT43D4cHjr75q2cr3kSjMcDOrH6uN169PAJ/byI61aPx4XwEPUgSpjzMtw8VF3EDLoxVuK9PFGtSHmIYmuT+LEoYPVUCVHfxmQSZ44u+wjxDTajsve0qS6+h70GA8l592Wh7WiMDEJlZVQ/Afb+hnG0ZIEXFi1MAffggbQr4ZETDjfJVgzxkQL2Fp7fD5au04MfDbRa9tLu8ZGMnA1opwf/v73V+0HzG/WjdSFCjagXgRbVbaYyGkEFkDloSAvKAmHlivCjqNkyeBhCWG0DEFnxzDL+OHNuEPkKQCUaAM4l/meDXPvE8mYfP85aiHjUqZBgZYxDX7TLcUe9ycB7/kDxxxKnXm9G8BEOqUE8zHHCw+BvLQX16KY7+GG32xgtuoNn8O/Mvnk94LHgrl9H/vNVu7V31JhgHhyff3l19lV9r0k/45+A8g9AbOYgKFO/r5v6owG2+lVaoe7Cbr6rD0GCAxIWkK37Fe+u77eAld50dyJeeRTEdSoB/bUWA4BZfokBWP5FjYxcz3jIsWwFSJr4jTQ4Z/aZOBpp0yztzekbdA83kkmiFZJWZu1N0BiFWglpXv74sd06oNMw0chwfUNMK6yaSaI9W0sagJjV2O3J6YdqeC5ePUO19w3ZeE/IbCOidn8+G7LIfDIKe/wFDc9N5hQi9qTdOjrowewe3Ir5e6v3gYcmOMHs7OQ+Y5f653QRgOIWA1d7QWtB0ATnc/7g2FdVjgd7uRo6I9MbIe8D1cXzB9k6pzTDHE2pdpk2SG4+DjkDCnmPTTx4UPE94d+5XVQHQIipgz1VlfpV77vKqTICYtVrRsyZg2Bcjxs+LIZ4B6gZdtfdktbluEaLdS0sVrfAN48ubVF6qeweRUsGhEu/W7dbiyZ027BbN/DvjdXUvg3Ft6H4xvdsfY8X3RXWvUuuRU0CwKED8FZPkwLku1sp+QQx02QHzPykCzydw6PGEcg7+w9R3tkX4g6WqlfJSJkqJQISrEMhlT2Hl09tWGlvDxv777PCSbHMe+s2T3VMrW8QIQ5zyqDYyAcaLTaGA43yxOy58Yk3yNGnXUq1QsCoFrlgPmFOs2IXzCPMpfaOowF7fdJpHfVaBzvpML5qHRWwSiVLULt3gCGZBbEUFCyYqT29UZq5HVNMhRL/QDaoxOfx4O4Ind0MEBM7MHILN7cFPi52EAnwCHs0kjaiylkMkNrho6g5tb3xIN2xnJuvnJsdZ/GVQzWn0UCdl+rCIxTe6Xathu1PAE3aEnIW0IugIPtKElN6LfvsBoDiL6OAzIXURPnU8ptYGWkG06iRQoCPX7X2DyzSJ+Qe3d1gWdAUdbIz1DlIZ2jNYtnfaLFssJy7xeUcMYonJOrIShe+Jlkc7ihhpdNupDvMVwCvtWJZ0X6CvB4/lisvdjSSPKoR7RzlMEstTwbEuSjI7LXtuLb3HeYgBLkurdnQ2rD6E5RLT1DdP0+CEKaybDlZZqFgp6SgZeZRO9GQeLt2TEVaSVviqthbHyXH9x8/KkWLik28YGh7X3vh1B6ADJGfOf1zZyP1tVMgtwz3kTtCgKGtB21+da1Re/q9cQ4LqcbrolryNRpVQMwhq4pZSnfZjg80urtAXd1veXLCdqId0KpvU41iKyFeif4Ua4SSRUbEdLMSvCDnbrmkXNfLwotUWk5FZUGQQl7u6gKz1ZgIYbmrS8vlTAYpZHIDNRr7m/CS7t6jBpAk/I8YR9U06gyh29jbmGmoGX9UnPDCzGqgSH6ygihouoWSRAPe6eyn2gMo/sRHBnLy0eJKzhDXHweZydPs2Ma7ebs9fIj+HE1GhAKoYyBlyGYtbGVnYKYV5JedGjr7TKcmCQUKnnqVWW8c9wptZVRI2KRGcYxjG5jqJjJ7GAfePGH9zPXNlAczF/G86gaqgpHSlP1mcuvAqz4a4PQMObycxC2m/7wYeqnA72clfTpai9iSBWVMn1hzHptAy2tQwgutRwnGetEdPvymKvqZv1Fmr93A/x02Wo+2TEWbuQmQh7DJDpwoCJtc4OkNvXlUPwpvLBE8N81ajz/vNcsFv1PtOGPlkbZbNRvKl4iehLyle9uLLPfoIst1dzVnfWrS3JxeTFZylSTeTKZNJEg6Yk9L9P0MnQrSOr2TqAC/2xV6oPXpgz4k37J20wBSoXa5GhHlRrd5ZlAVzqPQY/eArExzUBQjn2wnuO61AWy8ejTfH3e+sdnxqTzXIXBHMXQSqXfC26F+05rwyWtY298aa6XxXaU4y88ILhfDgQ2KOUZeGgNRQuJBt7KogNFPHXo3v07uPgyFooJABIKYHIQdx8HItTNj4Cw19RF50wKn52XI6Fq1ejgTH/prGPjQ56frXqDR1ZTnhZpDG7s38TMmlpDvvxGv0wjaOGtKkU7KPSvdaDxLP7pITVZkvmKzpjvCmxXezbsHo6PS2MOZuNdLRnM+g8VGRytAPIvxJIvDEsqR1dMjYAnUHRQEuu3OPsVRFtCmSRiNtMRpQcSQx238wMAsteQ65IBmzyQBqPxoVCakYMZOeMN1fguMgZlNjNDGHcMvHG8qCckpXF+Yp1/trnuSPvaLV+dpTIjuQBW7LeU+127bwJzGGT9g7tjdOzp3986kCXvYOVDgmwWKHfrWrX4mW/p+6BxmJtuQlT/CgYErZ+cvRWSVFG9m/iA95JGpj05BPE1Stxqzm9JC9o1WqJ/vb+bv1Gc3zZlvfUVg5vrXR6ElVlcrzl4RL8rTs6vw0nnoDNiVpmdDZazzDc4MdHZC3lXyU1p9nutVmSOE1kAqeaPOroQr9vfNqIWzbu1G3Je66iAyT/auTlNy32udOvqq08aY/L9ZcQI4iZ+pqthloSZa3/HNcevRw5WnfJ/ScdTs2Uo/uF7Z93M6yV1+8likxe1ncnrxzC91a3AszwBb/Az8doCNxaCQeFeithxAflC19JDtUp2SlqEhPPOF0ELjgeo3dRzGg3x08ZM0YrinhUz0X0XBzMWcQJ6H4WlcEQDZsipXzo75xIZp2KEDVJU5czDLlWWV4vb2Fk+A15lasFec1oFUw4g8y0/Z2J57SNtU8Mq6VetigG2lM8MP8OdyGdENB/jebMyutKmoLjwPzcY8hG5U2WKGpKpWK0rKJgv55LRIJOFkhSWgJ1CWPkSaJHLAVlOTuCogR4p4QiWbtX8FQTph5iA9pd7hkagaQ8tcMVC9MHJtyfQKsrlCyk8EEYZJ2S5WZV7g9wBo2ReAPpOVeRzWJhXKJhfZpM96Irwr4zo6WN6qGxHSKw0yFxO8b9HF4i/HOETLSqpAUUaPFdmI+E0FIixHp5aPH3myguVQF55U4LxLnVNyHkEImyWv07vgaiilF8u8FhZczIA6rDoEUBpZlRhj3MdWnGfbNNKxeLv3quDHzQaVnjLTY1qjYkzrgMe0fvxIv6g9+UDWR1OP4b0aRG8vyTpZHon+GxEESMHgm0a+pwnuRPw7su54cLwFEtOL2UkkPOKRebHwuu+YveKBM3lTrUKUFOPlquJDuHtkPVQG4dtaCvzDIohA9MZQR7UU3152G1fd92I5SpR29VDJfO1upvpe42qPqudPUznj3K0o46ZH8jhi4nJPDjpX5IpC4dOv0oxxq9K50uAQH7DHrTIslZNuPuqTwowbXZoDaFDG3q8dBwxh5QgKA+gPM4GOKjaVMxXJ+CRXCcI4k9wwDbKqB2mkUxroBG3kWcrHj1pR9VPQY7H8x49BNgakpEnL6n9u5iKOwgOBGSsYCk/bE79voZgTUbB9SWAY0P4oH+ar4ljScN+jtlU4CuiwWa38dB+F22gRvFlLBs69KFEaCw7Cc69TZhbEwcaj8lpVIcES5XQ00aGh6WcTM7G9gK2qmN51Z66zm8xKMtZ37TUb5Ia5fAsnTzCiumz/qTWMdDXkyyDt1Czj+BitC/Uprq7OyrzAn5/MSWGWAPPjNGtAhkIdKxeAPLVdf0DJ+OHFB3ok1Qd/PHhA/8AaQRbi5B5ziYuFrW/Ay6yNYpAxnWhcW1uHR5IQXLwX1fdmvPakYIfMlOH5Z0XLuHGnkRUB0AKrZHlP5JnjniwjLRXZzdYZUReBdkwWlpXaq/OsHsZJeNlylF/nDZXFQfJmfwVD3GxErzmlVU5ZhhzFqErBqjMZTIu7UAphJYBqDbRYJsZtr5iXQTPB63a+9MV6o3zJ2MtPVZFQKD0LGlqqrozkfonhpMRZVzDbd/MuO91PQ3EGeR9ppdeF24aNgt03Ne0WUIuWXXo0sjtX1ihK7jyHjQLO+XtztGSgk30b+61+SkqQI7cyKUb2slJ4CrTcDPcmFAnG9jLdYTfna/yyCSMJVvA1bFhkr9bW/FrYn+RA7+Vq3AcjKAz9r0tsqyWm5rfo/fhzrqTNJERN2MuIiLe3WRnxi3weQ+1y2GoLWPmNcmTv4dfJFa1g2pVxSo1anb76ZSiTSpDEx9eHlAobY7xOr4+XfInFr2VxX9ksv0tQNm2lt23x60vMMx8+AIXxZg1sNr2uBPsTEGzcoTihtb5H0XBJlzTYjTukmwbXd0eN5jrbSisIQqkSZOeml5mjHv29FbdEbEtT2VsK+2U3NmX9BmKQ/OqZsuSnInNpelcE3c4yoItUjCeGOU2SsLe762HOtGkQJ72j/W5bIMHoVX1X+eK0i5XEnRbQdtk9Fuq+HYbO+JekQqw89IXh2E2K1E4Pw1LedlwdvJVnyczD1Lkmvw1MNJyeyhWYqAfi6gGt1g5U2zZ0gMIDMuEmG6bAyt2CfWyKmwt2chse+UmaQSi0+7XKva7bF9rcQM0vs99w4bbUsIOvg5BnvoOq6RG27Am2J2YdYcu8w+BnmfBsJbib2BeU7xYP+ZXmFCxYHtLL5LZJ4GrkiET53dvkdL9p8mCQo3Y7ndWdbJXCJh/PI8wwcLfr17vp/esChoyaf5gFY7q3WWKCXDOYNfX4zB/ybXS6p7UYljRYElu2V2aAqB2/9JlKUimT07q8HxAEFniPUz5Hbc94PAqcrG11TRilTGRQWHVBKIwcDbn1NPimYD3exU6OW8bFlBkRvwxTpNfFrJcy3ygl2eU3hoDkwg/H4vbQAJAdAxk6lQ7mCY2JkmWWEsUGaQjyWFWjKhDUcLJn5SQ/LX5Lkw7XENw6DvgHkN7c8aIp1kmPLnRrDllyzZhfOt+0sHkWUnMHt53Mprp9Fgw835YVPfVAmKPCoLPpf337yp1gClaMnQiHgR05resIhkhBy5gyogAipY8YBeEiy0zKpnTTaMV1CyeXUYQzmMM0zvCwMMrSMMNJBJOSzqnrozzeHMIurl0wyJ3bmBlYRUyLPVylhieBGrdN90oaT+Tw6+mGy/d4lCZWqFrZMLqtk01+avjta4JW8hsxFBiGLhMY0qiPgo9pqXkW4/5L4u8vBZO6dwbP5TnJ3tNk45yd8usRBTNFZo9cE60OkmlS5ufz89OWwcOhYqnaEY/l1hODhFFPGYESzE+MPx/RHYRuZJyffocRe8iLo7iaD2cWbXnKIF1gy3NxLln95TFzyblhGlLGzef1V8u0OXjrGPZfFv/G+ZHylMEDTyoGfI+cq2B4Kmdd98CnuPmMXxXbM4RAmnKUHOfSTHI/DcuUJqRNwqKM2G89zLlVR7Okdry7a6yRWN75y3e+kb14EMivdlwLwpoo08t+hg/Hta+fX8jPjYoGuHyaNkIdlZZ0nYqu+OdZJ9NVaROgwE/YylY4TdXUuEGHqoJcSNSbgE4K4WroySoYbzAEsh6vbmvRhK1nozGQMrDJCIDVxIG/stuL1y+NCUgrsLdl+n7n3+bX6NZ7LkpUtDi+1vZU3E9hE+w86mqqidBWHPlF7raYhtzziIHwQOKY6z78wXDYyHVAx3HHqM+M7IjlN9G/YInIELepxfcqGr1mE5S2I+PN6+cx4l8I1YbN2Q0GmbAKkall8BT7xnCh7pEwMEqgZXxv+3gLhbguo/cpGmfJ3v05JZRiZsYscz7S57XKQsS3yRrpmGncnGDc12IejQf+MA77xmo7U+14OY+8hhAxn/SMt8SwG7WzF+e19w2xv8L7JJqzW3UGJNoAyiDCgwjQw3enCkoO051h9VwypXKaggUtCHYLoKAWOWKe3xNEiL1bTthows6Y+SpELwyOoT5QES3bskuWF3aFlwThJTKt1hr5LrX9qfB6/cpxZemXYzXT4Peo9Qdg8XUZ7J4eTl+V9k8NRsb0fvz4G5BXReuZh4pc2y8CQyE+nV4zjTfUCmuNoUt2mgsRvdP5yTsatD9R5jV36tOWWGCmvrLNnqmfjRYHSytj2TY8Zc2N39MWUGxqj0YQxNrfwiItLPGyqnZTQ9py5qKP1H19a7W4q6pu3YW4eCB89m6zYO45pPaSKVVRNd5lc/vFFzAE40McshHqTsJtol2AHS7wyk9sd7la61JtiLWh3CxEzBmI6KDYnnEShC5zfoPxfzE/iAXqfq6etggefHnT3Xt01Md6C8OOjXgB62CGcs0sTMzbRrfdbusHGbS799DRgwPdxMtlSv9WDJ/8AK9MowLEMTKY4ne0Ga+FaRh6eue/87/BPmA/76k9B98KRaVnrLh+yVjCRi6DBPPm6CcoJUpppEdCWuF+JuyI7qfCMEFMkoASAO9Q6+baTabod8ZAZ5Q+K5JnAyOtWUZTyo0kGsqzkWk1KAW7IKKjhkiAl7OQqhFK6Korj5emu4/oUkuqA3+wWGRfa0bFTLv50MIXWJ7OSaVXaFGF+TAeRe6QFVtH7CAbE1fZ4UR5GIf+znQQV9n78wgZZw7PMtKju9Pq4kYuy/hI5zzrIP8CyyRg8R0QklHnk5C4/C02co52kx7am4x6zCa4WlUVDJ+op4EJ+Or8R3gVL3xYN7EbUwu4GnoGiBoAArKQtEvKgh/PAWiZYQHfvj49IdNr4I/dyTySoLzzsTsZ5Wgg8YKgMusZ2pljo+4F15bxuHksQyzn/PUU2LQFVP8Z3WN9kAmiMfTfXPTseRJU2NRymb9JMH887ZbIBkc5qf5huVQvrzIW9ytudMR2DysS76EYFddHdck3vj4DtBGNxSyOAe1cZ0IqiognoD0zuQ7Si9iqLY9ZTW6VGK5shTkrWs40tGqXz12DKDk9Dg52wtXcdguD0lo7YNX2XK0xlmT5qFASc4TTXquA1I5T9l2536dJ0SWHzxkW1+Chu1b1rErNIc1d9UBzmlorDJifTe1OcV9U2TaNmijLk79eG80v76LmV2E4xRC69VZmXWvcFryKWSvd7dfYzFfaEOj8FueXdIec7esGAuH/WCNM3BfJ0qoy2kbV4JUBhWiWiwF/pdi8FaBIsvzDhjQL0uBnI9mNXDwbk2vOmCVNjy6K67bXRKnxl6LMjKRcLxOVC3T6ada+34m+7IhpMm+Ld6tJ5eTUFPFVXHDF1Wu75JRHqFvGaxaHUJcZJIngXk8xjEK0+EXWVXEVFVfan29dLZiHYm1xXfEPG64rmIz7XVerdK17WmKvSvQ1XF+2IXKMrQta+Sul/HoopVy9vidKuUhV9L9Sxb8Uqigxo9wTObzOmGKMGZohccfhRsz4ryTyL4VEKu1n90Qo32VtcGRlVKeC/kom/0LIpMJqek80cp61vAqdFGhzEVNaJM0WSzF6zi8l9Z/7dhhPg8Q4A/UqTn4RwZ+aR0s3rU20ptlksSWZXwmDi5BkwVgA+MElAGvZ+HPHHY+h+sR26ZouLusxR7YMisMEM1NRMDpGmsw9O0Iz9e7Xzy920aStKR6Vdsj/n63Q6sBL/mXDJQozes9LNEsJ97jNc2oR4T9IUmkwkE4rkpR+scV5JowOIoJlpk7I/pVM78e4mcaa3JehSNqJ7sugib5s0l0LDf+VEEQFupC5SAf4+s9n475vOjihTMWKCoylEXvzyUZ2a82Nhr7CdO5yGefvh1buFkQHY75DZJsIddrdJZnAz7ivjXoyBU4AcrlrO7HV2yruK21YOcBlBNiaWLE0qxvuIN32gSF85vh40O5W1xeCsS7bUhMHjqG52dc2I6P9YOPSW3l0YGQc89vigfvw1yJB5Us4P/1O797uGrrnf+0oxCXroEbprYyOtBwGa9sIAXqE5MdMG48eGiq2YFskUBjCWhyoSEaQG7SeO6ORoQUvbIoCLdkh1HjEDo1MtMPaZlLvbCqQETjDoZGLkNjIgV2RG+QvipuURokYdUxtSbZjfiB2a5aCk5DJbp9OxqM2VwnVX5Q917WVTfd/J/ZWA1KKgYr40GwQNRwnYnFcA9ULuJHCwl2bZzcYnQ0wRgzzfmAY/nDBY/6xC4ypyfdRTYUyUrWYvwG9QDys7U6hOfJIzEjLUaMCcw7WBOZMu+Q6o9iZ1Rry/gYaMj8WRH7BhMyNubPeSH7iZGDLQA/aQhTCw2pQMoCPUVoctOXrwIiCAPMszWY2ntVGrRidz5QUCmvLPXMbd1mqCpan4Xn0qNFpdxvd/U5JGp5V3uhNTgSWoUxcflBBj0COdHQjADoMI9cfuXheKr0O4Ycp841FMOf2mQb9BKzgKVrD5rFu+EpuxAwLRAaewPEptixrlRA10a5BQW9uIq3GNHfC6y/qaK5LmGgKocNCqS0DSsy9hLs25Q4Or+VxevgNTf2I8HDjEhZMYuaNsT3KlqETTWFVTfcINZqpWzjGsgc7sFBEUCLtUBF5fpX3gQdMjakd0wkNIxiN5hEG3XPOgnVLDCzpWbSSQ2bNJiEo8L2FEY+mbGarM0JAZfSfi9PfX7zD86/86ez70/OLr79/9c4vHCwLQsluVUVD/Ke0Z5wHHuT4keaQRyry4MSPFInIYw95rOFHLZtvrvOx59SOufdaQYC+6W9W9X6NuARUD1m6PvH8r7+iD2E4k51s0AfMye4UmLxLfU3pJE3Kh8v6oI0vReTaPigKL4wYRVB6DGirHoRIZLZnVWkugh6z0nyOGrmnWFIPXn0C5G078E+EP48vUOh/vAu/8OllqH5+z2w+RPF8phYGf7WLDezKxoaBs9B7xcZhPQbXCPmgtler4DZ4wlUqDtAaNuWUJacFotE1BlX0NchlE9/9I06+UB/Ut6d44zhgsMHPkNhew7gKXIcPyuEjyINc1jeQdsaMnrYvdA3oexfvs1Et08cx3zJsg7KqN0QBeBZJynhEFb2SzHE7uPAkkaa8qK7PUu1FA4jUYurNF9fPNzhT5EBJ5loNwoazSerPqqkEHqErParoOdd6ODb5JqDD/618o20MDeB2QcgHQBx9OwQCn9IVpxRVQnMiSPB8hQ7H13HMIoU0j28EUzc0xGl3FTqxHSzALXUFLMWK0MD+yKHB7H+ZWQ1mQ05nWsQGxhfTjTTX08D79Bm9oNCq6gnFc22aBpdiSqpwBDnl2iBFLAP/FDRAbZkYlHWtIezMaZ0tkfnmla4OpsiU+iBB5NmLLIk9CzzyPM1AiECphwMTG7GLb2yfBfPY25LGYM/LGPh1xsUVSgEMs6MMZs5pHwcRAe9AJC6GggoodY47dkd2FVJ2OSuGf5HxV8ioUtsr00OzByp7XbxKrl0r33zyAnNu75nul4j2e2tt/Eer3NOGJV2C8ngJSU8gVXGSAZ0pYmheTcUBHjQ73c/IaK+pUEwG40w+8IjHL4AMoVbUPJRZRlDDBfTjkWeUXusOv4SmZ/h4EgLlTH6yLtaE0gx820p2qGJnxheArGogAXvMkNtmmRByeS0wJj7Xq0StliudNKDkrTz6T8dCSkE8V6AVQsdy11TeEdSyVirAzZ71Xwcz6vEGz3iBkn8aRPr50dkwim3YycrK3Xb3sNnuNLvtTcfH91FuS0hJUmO18S8/J2UDl+l7MYfEyrayOSSsIlu6Z55DUVMp0xGJhbOHz0qYC2cCSU5bBI4OTPyK3/9XcMKjlosaKa/HVUVxecYd2MbXsBRR+dL6D3xDTMWqSdeCtktmPdUOc9ODqmMqBJTN8WdYxDhI1Da1QfKAVeTOZFz+TCNNpcZSapYINo5XLeP2Sl6QNvK4upGu1sgKXsCPYKmQdfnDHgbzRL62PhOqUs1pJVEswtU0QfJ99bq/f+nmRBr9KNjm+NioI6Wp4DWrzOxz9/SStePjY5E3Mk3KMQ7Q0RWnMXKY1+zajjD7cDAjNsFNXinryB1zFYzGRSXpDkzkB7SuwGIMIp8t+PmDkZbZnR9o5Kv1yS8nh+BkrFiRn7RXqcGuX1Wa6RlJBBhPl2YKoyZ/zcjQ4Cttvbt25LQ0UjVJUdvnHvb4l6GB/PbzOfsCAQ0IKP4lGdsP3PyMNswYbZuaOb/A075OLeXISGIW2hHJlZL/8NO9LeMsMfBucTem28S1TyVJhNZxnR8BnJj5Tu/O5PSrkE0rl09ZWuA0jWU66mJeuOxuW5IVTt9sq/5TClPGZ+PknDYKojJwMhb7nrHkHo+eoVBL1pwJvhEIKkuZVmKYp7bw8ipDbP5oyIVfKBHDO55crNmpURa0jZBNRnafcn1Kx1MFjSEutcCMcpzpnipNAs1IS38OIkhtwlsSQZLzsuletO1ogNZnz3i7bLVatw2D/nl/x3n/IEECAnB99fQhCaAE0GzTdWqZTHj/0tyyIp3xOresfgrUnjsu9yymntlv2Miex0zkfUOHHE1hmmsc1/gIAxEYQ9OwYceXaAwAQZO8aGi+wu2gZQjb4wiNs6JD9N3pR0/P+SmAUIQWYGopSf9YHHvPaL605YBKy4P2E2OIpvI0iL9lZB35tH/gBRsgz84AW0Vn7P3vi/qQQEnKpIDh+lFhc4x1LKBb2uBX0diJOLHAOR5sj7CSmO1kzHdqpcF6bfC1J/gBz/2uzlFpiMSV4vpzMkSLkxCEqhwcGmK3337P5QEPmdfxLmYE2ca/FEuC1J9gg3GvbI/c9MF9bcUbMk3hGOxRBgbgbZQwUFpQy3ZW4xb/uy1T1YnlbnvqCQbWeugN94DNR5+EptSpuymadPg/uI7aBJo3iz/eQbaY+4pYV2LjF+BA8nIvzYeYYzkvAsUiHQbDQ9OuPRWcBXlHy/h6IyNEYRFx0wPZhEkesYVgM3ajOOEGNRF0k0ZYRWzMIvS5YxxNNrAHtAUX3+OmAqwwStIrLvwJBeHQVsS1C9yCBPFrewW0gU5zZixYcgctgtScEb8kLEb0TPOX/rEb2LVW2i3uxUJY1B/HPqCfQ1bd+gxkHf1gZCmDO1jL2M7GtO+PXRHLxoUAFCHQ+NfgGHaTX5jVSQLHvKig1TdAN6C/M/53j/4+rN1BZMTxOB8ENdX4+OD1ecKugQqbJ6jvngRR5DpBtIbNvQiulcwk1GDgeO4lrgQhquClWZ+upvparFDpNlYO/S9oM1CHH+WJshLb53UQeQ66SO1LkjvlKbRYHeIcRXbI8HKeCbpATs7/HpZgGABvaFC+MykQgXgpMiSiwBxCA9oRSB+D/EfBLAxiYjs83kETTzDqFDZIl+50uIP4U3smovdIKD4jadkPrjnPIo89QzMs/8xFZKdV+5Tlk4YHbLp8ikUwlEMVeLskqq99C0vAePfupntonEW02U/xBote7ZHdPAxrt3Rwd5s1VgJaarcvTVyeP5S60s6SSnOlbYWeDVuKbKISbD30rjztOuevKF5/UMaN0h59LYN7Wdp47rTlcWcfZsz241WtzX1BLmub9Oee94G8tDE1u1Wr+cTwf6HqcOFwc0NjDPwOLWFX0aKXUX5BaQOjo+Qe1CAFB98KRHL/OLyXEZiga+GpVgoOcWMKH4KtFNgU3guYHnaF8mgLR7EH8auCRiTvhHX+S+ixL4X7qIlr00nDInMsW8+ohWO3I7IuoOYZXPuAoYmIi8ehE21TrnKKoZQcnaJnKI5ent3CPvEaHhRkuZX4euqOpmk/GPeMpmQyAMAEgYydV1LXxax+rQI9eWjqCwSVpVGsmdDVDeJUj9/4l8DhfRV7hfdX2SMyo4k3L2CLgV6jKyn4jnA+nBWxYG/81Jyaa5jEpbRhfpOoQMWKFs98KjPibDLTIpkqxZuX8yR2Qf6n5Hn2ypjIN746GFMAkuTXFEgZ6riytfE8LrRDMY1ptCJs8HQNE5IAEcmqEb9ivqPHxPEWyTYg3nwjgi5JOFgVcvmDm0ynsKrz0GGQoHjzvfRnAQeJ3aHr8TCmtFbFqOk+2JJppm1dYTDh8XsxSP22FlFaGpdZcWYyH8NSwxBkYGyLtOs1xy3XFfp6lMyJ3U1gmv7s4YUkcK+4l8dFzgACCmgzNmX//+nMr6EA6nmjKcPktGMb5PPK5GlCngD5WbZy53u9KOsufG5eg4jbwz/6wafhHAqpbXsYaulzYQf7HV5h/XoOS4uXq6440SpWHd96ZwIW3sn7iwfvTJX5aQBLuuG5MzcZHLStd2Zf9o06sdR1PzMIQIV6z/AYf7Y+cc6VX3mAYnA39Nr0bybD0aCrg5TLUVQCXUk6AUmRoFESHQIProqo3udX5WIm+ScmzLpKySIEnmN5jYQSWsyeec48fmddeqyd7DLaTQCbnAXM3SrOh7zEqyX4bRaVRwJxQPp5wMG6i0CqABYiqpm9Mfun6msKYFblpLaSyJ3VrQYoXyvKE6R9KKNDK4///fynfzb7W93SbcJmbcqLupdkBej9dKvu6l6S1cGk9+atvKY713slruQJ9J//y3+SSg2QRkTBUB8/mjy2B+fY+H/+L/5lhNdpfvzYxnf8c4zmC3OHknjGdF2xO17UowaqEA2Qrela700B4pN3GkWBNEyZxSOkT1noBYv0bgTU4PGAKN2BcOc7keUSuuvJUg6VMBzc1xFTnvqEJdIeASuGnwPFIMNwPgSehCm/eYjY+fnzBl2vwNkdlONaiLqLUl3FAoqBf+QbIA1jAuZnFxev0ouMtZOlu7vGz//0J/if8SpiaJh2YxfTM/GX+lbZaWWLFIR9tF34DLUB47nrz0G1A1kWxPb6myEQ1Nzodne7+xYfhw0TOMMAH54b3sEgaVeGmE3mKFvCVMEMa266SzIvjo3vTl4Zf/89tJ7MQ7KdkSGXNK4jf1tjzJfGS59HInBolX4bz50AFm4i7iMzHjww1CviN6CWNBdGuEimgb8n/22GbmjgaZMbY8QimJNEfRHPTfr6zoeCe6qhMUB0Kf0CsQEiDMzuZZNHtzRDEHDQnFVuRPwSza9zUhhffd/lFy3MZvyStZ//9E8cd6gQHflWdnR+OEuHMjHCWTfD2FPSeBPS7SwP8DaREsrottISCaYtE5edrrzusHQuKJ//iNZBC+8yCMqnZhRqhWwHlsHfYrkmL9f7N7v8664qVIW58/NnFMyLKY3TJFbv/JFjyEbe+bNLx42MZpgm/YHZE9OegtFsEo8yd/Affq1K6Vz56lh4wiKAnI4MsggYamrjHc0jzyjc/yKb3p27okjz9zw5XHVRh82C3RgW5dpGeUkUaHa5zFpBCy+IuItEsNcSnyKMDYjpyPRNMdhLDz8fRS6ZUI3QjoRrJ2Vt6TWhyFNifiwGy2KzuPjRyUNsDkRnkraH8/GYDls1uL3g2gUo+Fq8doHEp4Hn8JB37jJCEN6cGdeBChgQS13k++a+rq35yXfCVMOHSAewsvbidJ7LqEgwD/4GqZozStiVZ8axsZvMQknWgpGU09nLCNBLSYpmtj/PdssXP5ChscuS0S61s0s8vWlf2a6H2BSdFCS98PiVHdPg3FiMbrXfTGwCwkTKnz6QQ6hoZywdtQC7jyMtjyr60ngRRDOYMMVAke+9OSOLf0NtiZrp0iAWRfLGrgQN/0PE9SGEpuVK6XQfttrw345aKf18caDFD2j6gVE+gyVl/BYX1qpSv2++xnTyZ6+M3+Kp4YR9wDwfq2t8G0QYlM0c/GX8lpeAah9uPozlJ/y1aSuvogCWzm/pJD8TlW5XoVhbmYjdi2dn5wb87+LZqfGvTn80Xn39+iKD4LI20ECCbg08bcrUGm6IRcrXr34yJRflVtVsuph3xdWgwDOQ6NCtojL3i9VdQQb/xviH3bf/sPt+R1wp8NtfLVkgBB+QxSLYxTXUaXXSFVPZ10mauKnE3F8rtlBwKPEmFc8FwWzcz6B2Ra2RPcJwxrIavADMgPMh4ZeuGUeH++12rMEyms79SyB3OroL3X9gvrjlCJvM0TJeO8l846jdr3JXrOHnpz7tLiSGugnLclHPN5rxejZaLMGoVWdXcmNaAc1EPHLBb5QgSyPBir5X7MigE5Tsx/st+kDS9nOmLps/9UfRIlxl8UKejTBI+VVuSU2nfFeq2oFORP3M3gfjoVv5+CoXex3fIzEQzKmKxxZtqunc398z4tjrrykNRT7gOPhxZsanwWMJ2vYRDbuYD2W3dFi74zma+PApZLMtO/pwyRZbdRZG7hXU2aQr0J/dCA0fR22jeYyYEBVK9uqvx3gHL+7VDS5Jv6Ebvzy6xTf5lJNE8g7C8uHMXRHsU65pgppSJNiDlvGvGAvR3S5VYCJeKF2QIs9RfhDxF0K3aVGzSGzNiIloILTvRXY85RFC8+iKst9EbBgEpTedrdNOuEAF+pW4b4xTbiq2A0qPleKwq8q1eLnWH2Lj8WPjnXn68tt3oCDMAmfusZYITzAGfMexwxAjVsTuwyNL3plCFzGFN5fHpqUfUMWR31SWF9CEoICQMdVnO5rE+D6nr8AyRy1ndxrM2C6pVHIYqdYjmxhdO9hCsawqwfyrXrqBnjx7+fr05YdXL19fYD2lIsnSqsTTs9flDetAKP4Of94DT+/jtZLfVmp4SAl8/u2rlHmDrstDxoySWRKfobxWcB5WSB9pSuv0QIpDgq472uS6oF+zm3zDZF4n6AmBhSs3GbF+NZ/4ZuGCAtnJPJaxgvGULoiNMTHShiGHQm/h9YmD8CY3rB/QhUWifst4HuBy2Rx8D8obGRgqdu3C3e15lng/zi7FYLult8XfhwWTx1LkL8+SN8FrB04oyG3IyGbBP7AbfliGrphPD5hOZVAcl1KlZZSbNBvGv7ZDl0WNtbZNsbGoe+xtqQpub5g6Z75TuOyew5O12ugmmer9cbZoqivMlMX23Tvkac1nRk1cRty8oHBD4ZHHLnfxhu+aLOkArxRMtgZ913pGrSjMq8uCYYvErcFjo6lrExSiZwFjM5PIvSZ4szxPxS+R52E/lcatl5QcDlGF0S7IdLNJ/Y06P86EmSIwjgWGZ/3FoE+cZMnjj4+4hkGoPNa+hnnG4B9MrfVeILTyCOUExEexXiqCa4onOHAFiSUGohE6bZQhVfkRuAbOlcH4DoHPNEmyVYEIbH2F7Aq1nqn+VpUyjNUTuK7275ucXzRlwv6eoRHMujNz3wTOIheoDJrrB9eBKdx/SLNaS2IkFpkdoXNw0Tnsdbu9dqfVbrf/NZ96VHfzFJU9dSLKcb9fTclLNeqqFnptRUN8A6vRcYEgZk6NyIZXF15CrT7FHvAmKPZAtQJzNaIPPEg20puhACGtERB1ca1A4R+ZF5LaRel3p5grAOrObIyxgeWuQ1SmYQPRoh8K184kEuE9OUI+8zlfFwZLW7mpf5DsWooRSeROJsA/fNhYQb9lZEoyZgxEXNpPiCyR74z4VchMp02yMMuDZNAIKBq0Fji1bLsIvjQ6eOQKIe9JyAW8GTDXaHPyZgsF8KsI1AJumcHVWmetScvYgAvZw1Gnu2etJO8vjS7ezijGnx38n5ULS/678ThrK3Yh9ESAAFHYrfmkxJxnIU1eB9ElOnvXTdGFRkcYmkTxhS28wLYlb7BtqStsW3pocGWTr4M50AvNwNm3RCcgZbpj3BUHA34qS92AEhi/b2jfcN/Qvv1YpWLAIuMXlWgG8G3FHnXfhVw0zCma05VjaTtSqZq+15hIk+nCWo40n54+P704/ZUT51+4xqeUFjzlM3Yj1gSu2xwHFAmV8mtYOQuuFEZo7+cZ0GwjRYCB10LYjhEGgWfU92lJwhpVUQKddmwIO3AL/dzkG5COQTygFlz7jezheAAavX3xHAaIjkMRRix1EHnwlzZDOtEWzZMpz2GjbRgtxcyFedLFQ/dBxK8D3kwfVEYLRYEtLr9w3bLSnHvKg5qVciSdGp9bQcSez7mLp67cJta9aonovCLrY+qWSe+QiBsGni1PKvzA3OzpLVI9kY7HCDfuPOZ5SUXyhRI98O/sK/ucDGdGfYhnWTAxdgR7hcPy5643ypkolFOp3+Kocg4rHgUWxxgJiqcBTtGrdU6UVyepq1rfWM/VMn2B/GWRhxQ6awEboJ6ek62cRfWSftRZIVBF6uRts4zBMRf/ONh08x6FfIV2FDNeqOXYiW31ZanAYy1YVPUgpO0Q/pEbIvwUW6IlPLelSOQZdwWFNymmvJ6I2aV9+vz0u1SmMXAb5J3BVij3yQcPtN5k5O1Ai72VQi13AD614+kwsCMHGuIjQbmVQ1kK42uGgUiGTbZ0x42lVaJODkEoi2HXIFiePbWKM9/9fFP/BLsGxWSw/1CngjJB4BUZgbcgcjxY6M7QTiuDBqVvHdGEG/HgfqmX+L3sA4P66tBJQyyowUU0ZxZqs1GPT+WY+IjPYzVaLszMB3yM61Yv9RMCsVAZVAjwR4u7BbCr+rCGlAy7ul4B/0Nkj2y6hY63uI4V3x723lvZYnhtQVIf15ZB+PadGYTvzPe3Bn/gSwBf1EpyR+4dkxxTd9hwPsG0xFvzni+NH1Dn13JUE/PJyUkvthCQeCvVAhnRP+1JtkxPO+KRe5/cqSLiTru9lczE7/Wplpm6+weNzsFRo3OPMpO4S+huIhNuaVexMr5qxvE3Md/wZJ4syQ8p24d3bS/iZsCD1EQo1HzYjDG3s0O5EfzRAshJcjXYRGkrenMWWy1qWom/Kg8XRWnmpDbDYVgvoqg7W+lGsP1OcA3VNcurZVA6F+bLECekD9CnYxVIxaFO8/hVyjr88E0mO3JG9OFi2i8g+ghAXqaAnPmfwT4+m4srt+gaPhRFAd7JlE7NKxO2gaeDpS8FbeEwI2RVzaNJ2HHwJGIYivOIcuLiEgnoW6xEK3VXuBDvoBMKL6jt53ND39F0oA3qk0y4Ffa2NFGVMoSlFjdpa6OMAD63gNVewHb9CkUHfJma3vBi4NqtbEMzuxUNbrqtjYff1W7XmM7f8NBDkaL+V47TvA2zGqcFy+VGCFSFlFVSs0eeoD4WzTDrOTVJN3xjUA38gH8UpjdIMQB6wNy7NLigU8y9RE6fbFoAL43Tx/PAaEOUZ2v4LTHI+1CjXRh43DNuGa9YNEZpESUWuV6pKUxNwHu+QygCJRB4Z941gcCvk6zSHAUlZCXFrxpqbuhOSeWxpVq6lIzggUxEUKNMBPie5yKoXs7ZVpABnIdBkmmi07abR6VtyCbei8iEUgoH5JDlPZeqoFE01xeJXw1dphOoiRwkNQV+LZMgoEYph+SR/bRQ/sh/tpweWFHNqF7LjGLcFUAEJlaEOqS+OohrWVMFP9BZIwBjDxCh3uLsrjGgLgUNcZcaTwBIgkXmeD3INAnJHxu2hqbVTGvqCH/FDQiqBahjlPxnZa33ldfFyq0ajdR1dbRnLF+BOMAxfgfVQfpa0NACH8l3/pqrXWSGXhd/9z35WnqGdklvVdE3r5/37sRc1rSLHjqjjpYJq+ioU9O4yre2XBq/JRUPlIXbW8Vs5PpXnylMXSuQrnxVRMSWQKHNfHIiP00Ttc8SzxzMTVNKcE00JW+SaOOezkL/bu6OLtN8WyW09blutM7cWnovl4x2M1eMrtTMcgofSOpCDo+VhrbZRZvypKW2NIzdXtnm+cAfxmFfM0yj+A8aE4glm3WlLrVNr0wXjWodCtsAf1/1twQIlBvqoI9a9zJspX2W9Kpcsfnji5v1LO7kXYWDVb2jTzv1Zm3UIz+7+vT0+Z06jLhn637GqJwLayZ4k786QtKr0u8++/cDm4BqlL9rHa9av/0E6NSZuPuAjtrBFg3k6J84p3P3HiZzC+C5pR0tEci+s8DDw7aM81vXYxVMM3WFfdVyhvdHsue/e46aH+J+aKMHqI5cLERrlOjRqoAj45IrAUh0IP3+mfTDWUxtcjFyEkwmHjvnfq6661hL7iEA4GdxZaYB19kxz7CEaTVGU3a1suAJFDApOQA12qJZa4m8IoPBwPQBNaa1LPtqmn1sX7zl52qCaAbvb/Fsf3kdaq+iXhRQaozmo7bDJpZ5C7i4dn0HNNSCcwpTEAANmY26NTjGRBGgnP69y64RZLppxLR4ToC6dWv1v/hCWQ/RgHjmg8atHr+o57JPuPCZJxEQPq1LkZGBcrekh6DqFuYUgL6DSyoty5N6IqqgO0BGlMW8giwWM686WwSS3DnzTFEDB0itDgYdawk1RTYR/MVzgcVvO+/5yz7vGT0Wo6nsu67qWLfUImVf0JptQ7Ogm/3APBCxWV0rVXx/y2vzMWe+IrS3X9xa+AvWNFkLj+EX5eaBf6fJzDv+4v8DFukmAg=="
_UI_HTML = _zlib.decompress(_b64.b64decode(_UI_B64)).decode("utf-8")


@app.route("/ui", methods=["GET"])
def ui():
    return Response(_UI_HTML, mimetype="text/html")

# ---------------------------------------------------------------------------
# Health / Info
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "Choreo Runtime",
        "ui": "/ui",
        "version": "1.0",
        "operators": sorted(VALID_OPS),
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
║          Choreo Runtime 1.0            ║
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
