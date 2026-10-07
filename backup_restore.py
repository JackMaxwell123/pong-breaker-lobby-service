"""Bounded, encrypted logical ledger backup and explicitly empty-database restore.

Run from a trusted operator machine. Keys and database URLs are environment-only.
No backup path is inside a repository by default, and an existing file is never overwritten.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import tempfile
import time

from cryptography.fernet import Fernet
from economy import Economy, SCHEMA_VERSION

MAX_CLEAR_BYTES = 128 * 1024 * 1024
MAX_ENCRYPTED_BYTES = 180 * 1024 * 1024
MAX_ROWS = 1000000
TABLES = {
    "pb_accounts": "id token_hash created coins rating ranked_games wins supporter reward_at equipped deleted store_review",
    "pb_owned": "account_id item_id",
    "pb_operations": "account_id operation_id fingerprint response",
    "pb_ledger": "id account_id delta reason created",
    "pb_matches": "id player0 player1 mode seed created state receipt",
    "pb_receipts": "token_hash account_id product_id created amount remaining voided pending_token",
    "pb_ad_challenges": "token account_id expires used",
    "pb_ad_transactions": "id account_id created",
    "pb_meta": "key value",
}


def cipher():
    key = os.environ.get("PB_BACKUP_ENCRYPTION_KEY", "")
    if not key:
        raise ValueError("Set a separately stored PB_BACKUP_ENCRYPTION_KEY before using the backup tool")
    try:
        return Fernet(key.encode("ascii"))
    except Exception:
        raise ValueError("PB_BACKUP_ENCRYPTION_KEY must be a valid Fernet key") from None


def export_data(database):
    snapshot = {"format":"pong-breaker-ledger", "schema_version":SCHEMA_VERSION, "created_at":int(time.time()), "tables":{}}
    total = 0
    row_bytes = 0
    with database.transaction():
        if database.postgres:
            database._sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        for table, fields in TABLES.items():
            columns = fields.split()
            cursor = database._sql("SELECT " + ",".join(columns) + " FROM " + table)
            rows = []
            while batch := cursor.fetchmany(1000):
                total += len(batch)
                if total > MAX_ROWS:
                    raise ValueError("Logical backup exceeds its one-million-row limit; use a tested provider/pg_dump backup workflow")
                values = [[row[column] for column in columns] for row in batch]
                row_bytes += sum(len(json.dumps(row,separators=(",",":"),allow_nan=False).encode("utf-8")) for row in values)
                if row_bytes > MAX_CLEAR_BYTES:
                    raise ValueError("Logical backup exceeds its cleartext byte limit; no file was written")
                rows.extend(values)
            snapshot["tables"][table] = {"columns":columns, "rows":rows}
    raw = json.dumps(snapshot,separators=(",",":"),allow_nan=False).encode("utf-8")
    if len(raw) > MAX_CLEAR_BYTES:
        raise ValueError("Logical backup exceeds the 128-MiB cleartext limit; no file was written")
    return raw, total


def restore_data(database, raw):
    if len(raw) > MAX_CLEAR_BYTES:
        raise ValueError("Backup exceeds the cleartext size limit")
    document = json.loads(raw)
    if not isinstance(document,dict) or document.get("format") != "pong-breaker-ledger" or document.get("schema_version") != SCHEMA_VERSION or set(document.get("tables",{})) != set(TABLES):
        raise ValueError("Backup schema/format does not match this release")
    total = 0
    for table, fields in TABLES.items():
        entry = document["tables"][table]
        if not isinstance(entry,dict) or entry.get("columns") != fields.split() or not isinstance(entry.get("rows"),list):
            raise ValueError("Backup columns do not match the approved schema")
        total += len(entry["rows"])
        if any(not isinstance(row,list) or len(row) != len(fields.split()) or any(type(value) not in (int,str) for value in row) for row in entry["rows"]):
            raise ValueError("Backup contains invalid rows")
    if total > MAX_ROWS:
        raise ValueError("Backup row limit exceeded")
    with database.transaction():
        for table in TABLES:
            if table != "pb_meta" and database._sql("SELECT COUNT(*) AS n FROM " + table).fetchone()["n"]:
                raise ValueError("Restore requires a completely empty target ledger; existing player data is never replaced")
        for table, fields in TABLES.items():
            columns = fields.split()
            for row in document["tables"][table]["rows"]:
                if table == "pb_meta":
                    existing = database._sql("SELECT key FROM pb_meta WHERE key=?", (row[0],)).fetchone()
                    if existing:
                        database._sql("UPDATE pb_meta SET value=? WHERE key=?", (row[1],row[0]))
                        continue
                database._sql("INSERT INTO " + table + "(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", row)
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup","restore","inspect"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--confirm-empty-database", action="store_true")
    args = parser.parse_args()
    protector = cipher()
    database = None
    try:
        if args.action == "backup":
            if args.path.exists():
                parser.error("Backup target already exists; choose a new filename")
            database = Economy.from_environment()
            if database is None:
                parser.error("Configure the source database through PB_DATABASE_URL or PB_SQLITE_PATH")
            raw, rows = export_data(database)
            encrypted = protector.encrypt(gzip.compress(raw,mtime=0))
            args.path.parent.mkdir(parents=True,exist_ok=True)
            # Exclusive creation prevents overwriting an operator's previous backup.
            with args.path.open("xb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            print(json.dumps({"backup_created":True,"rows":rows,"encrypted_bytes":len(encrypted)}))
        else:
            if args.path.stat().st_size > MAX_ENCRYPTED_BYTES:
                raise ValueError("Encrypted backup exceeds the size limit")
            compressed = protector.decrypt(args.path.read_bytes())
            import io
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
                raw = archive.read(MAX_CLEAR_BYTES + 1)
            if len(raw) > MAX_CLEAR_BYTES:
                raise ValueError("Decompressed backup exceeds the size limit")
            if args.action == "inspect":
                document = json.loads(raw)
                print(json.dumps({"format":document.get("format"),"schema_version":document.get("schema_version"),"created_at":document.get("created_at"),
                                  "row_counts":{name:len(entry["rows"]) for name,entry in document["tables"].items()}}))
            else:
                if not args.confirm_empty_database:
                    parser.error("Restore requires --confirm-empty-database and a separate empty destination database")
                database = Economy.from_environment()
                if database is None:
                    parser.error("Configure the EMPTY restore destination through environment variables")
                rows = restore_data(database,raw)
                print(json.dumps({"restored":True,"rows":rows,"verify_sign_in_and_known_balances_before_switching_clients":True}))
    finally:
        if database:
            database.close()


if __name__ == "__main__":
    main()
