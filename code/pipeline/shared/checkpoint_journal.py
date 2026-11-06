"""Transactional per-source checkpoints; a source and its rows commit together."""

import json
import os
import sqlite3


def path(config):
    return (
        config.claim_generation_dir / "01_raw_claim_candidates.checkpoints_v2.sqlite3"
    )


def _identity():
    value = os.environ.get("ENTITY_EXECUTION_ID", "")
    if not value:
        raise ValueError("Checkpoint execution contract is not bound")
    return value


def _open(config):
    dest = path(config)
    dest.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(dest, timeout=60)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS chunks (sequence INTEGER PRIMARY KEY,chunk_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL)"
        )
        stored = connection.execute(
            "SELECT value FROM metadata WHERE name='execution_identity'"
        ).fetchone()
        if stored is None:
            connection.execute(
                "INSERT INTO metadata VALUES ('execution_identity',?)", (_identity(),)
            )
            connection.commit()
        elif stored[0] != _identity():
            raise ValueError("Journal execution identity mismatch")
        return connection
    except BaseException:
        connection.close()
        raise


def _payload(chunk_id, rows):
    if not isinstance(chunk_id, str) or not chunk_id:
        raise ValueError("Missing source ID")
    if not isinstance(rows, list) or any(
        (str(r.get("chunk_id", "")) != chunk_id for r in rows)
    ):
        raise ValueError("Checkpoint row belongs to another source")
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def commit(config, chunk_id, rows):
    payload = _payload(chunk_id, rows)
    db = _open(config)
    try:
        with db:
            prior = db.execute(
                "SELECT payload FROM chunks WHERE chunk_id=?", (chunk_id,)
            ).fetchone()
            if prior is not None:
                if prior[0] != payload:
                    raise ValueError("Conflicting duplicate source checkpoint")
                return
            db.execute(
                "INSERT INTO chunks(chunk_id,payload) VALUES (?,?)", (chunk_id, payload)
            )
    finally:
        db.close()


def load(config):
    db = _open(config)
    rows, completed = ([], set())
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Checkpoint database is malformed")
        for chunk_id, payload in db.execute(
            "SELECT chunk_id,payload FROM chunks ORDER BY sequence"
        ):
            batch = json.loads(payload)
            _payload(chunk_id, batch)
            rows.extend(batch)
            completed.add(chunk_id)
        return (rows, completed)
    finally:
        db.close()
