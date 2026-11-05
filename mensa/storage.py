import csv
import hashlib
import io
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def member_number(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("Member numbers must contain 1–20 digits.")
    return value


def parse_paid_csv(text: str) -> set[str]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames != ["member_number"]:
        raise ValueError("CSV must have exactly one column named member_number.")
    numbers = set()
    for row in reader:
        if None in row or row["member_number"] is None:
            raise ValueError("Malformed CSV row.")
        number = member_number(row["member_number"])
        if number in numbers:
            raise ValueError("Duplicate member number in CSV.")
        numbers.add(number)
    return numbers


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS links (
                    user_id TEXT PRIMARY KEY,
                    member_number TEXT UNIQUE,
                    pending INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paid_members (member_number TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS interactions (
                    id TEXT PRIMARY KEY, response TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, action TEXT NOT NULL, count INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
            """)
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialized(self):
        with self.connect() as db:
            return (
                db.execute("SELECT 1 FROM metadata WHERE key='paid_imported_at'").fetchone()
                is not None
            )

    def link(self, user_id: str, number: str):
        number = member_number(number)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT member_number FROM links WHERE user_id=?", (user_id,)
            ).fetchone()
            if current and current[0] and current[0] != number:
                raise ValueError(
                    "Your account is already linked. Ask an administrator to correct it."
                )
            try:
                db.execute(
                    """INSERT INTO links(user_id, member_number, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET member_number=excluded.member_number,
                    pending=1, revision=links.revision+1, last_error=NULL, updated_at=excluded.updated_at""",
                    (user_id, number, int(time.time())),
                )
            except sqlite3.IntegrityError:
                raise ValueError(
                    "That member number is already linked. Ask an administrator for help."
                ) from None

    def unlink(self, user_id: str):
        with self.connect() as db:
            db.execute(
                "UPDATE links SET member_number=NULL, pending=1, revision=revision+1, updated_at=? WHERE user_id=?",
                (int(time.time()), user_id),
            )

    def get_link(self, user_id: str):
        with self.connect() as db:
            row = db.execute(
                """SELECT links.*, EXISTS(SELECT 1 FROM paid_members p
                WHERE p.member_number=links.member_number) AS paid FROM links WHERE user_id=?""",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def targets(self, all_links=False):
        with self.connect() as db:
            rows = db.execute(
                """SELECT links.*, EXISTS(SELECT 1 FROM paid_members p
                WHERE p.member_number=links.member_number) AS paid FROM links
                WHERE pending=1 OR ? ORDER BY user_id""",
                (all_links,),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_synced(self, user_id: str, revision: int, error: str | None = None):
        with self.connect() as db:
            db.execute(
                "UPDATE links SET pending=?, last_error=? WHERE user_id=? AND revision=?",
                (int(error is not None), error, user_id, revision),
            )

    def import_preview(self, numbers: set[str]):
        with self.connect() as db:
            old = {r[0] for r in db.execute("SELECT member_number FROM paid_members")}
            links = list(
                db.execute("SELECT member_number FROM links WHERE member_number IS NOT NULL")
            )
        document = json.dumps([sorted(old), sorted(numbers)], separators=(",", ":"))
        return {
            "paid_count": len(numbers),
            "previous_paid_count": len(old),
            "grant_count": sum(r[0] in numbers and r[0] not in old for r in links),
            "revoke_count": sum(r[0] in old and r[0] not in numbers for r in links),
            "confirmation": hashlib.sha256(document.encode()).hexdigest(),
        }

    def replace_paid(self, numbers: set[str]):
        with self.connect() as db:
            db.execute("DELETE FROM paid_members")
            db.executemany("INSERT INTO paid_members VALUES (?)", [(n,) for n in numbers])
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('paid_imported_at', ?)",
                (str(int(time.time())),),
            )
            db.execute("UPDATE links SET pending=1, revision=revision+1, last_error=NULL")
            db.execute(
                "INSERT INTO audit(action,count,created_at) VALUES ('import_paid',?,?)",
                (len(numbers), int(time.time())),
            )

    def cached_response(self, interaction_id: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT response FROM interactions WHERE id=?", (interaction_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def save_response(self, interaction_id: str, response: dict):
        with self.connect() as db:
            db.execute("DELETE FROM interactions WHERE created_at < ?", (int(time.time()) - 900,))
            db.execute(
                "INSERT OR IGNORE INTO interactions VALUES (?,?,?)",
                (interaction_id, json.dumps(response), int(time.time())),
            )

    def summary(self):
        with self.connect() as db:
            last = db.execute("SELECT value FROM metadata WHERE key='paid_imported_at'").fetchone()
            return {
                "linked_accounts": db.execute(
                    "SELECT count(*) FROM links WHERE member_number IS NOT NULL"
                ).fetchone()[0],
                "paid_members": db.execute("SELECT count(*) FROM paid_members").fetchone()[0],
                "pending": db.execute("SELECT count(*) FROM links WHERE pending=1").fetchone()[0],
                "errors": db.execute(
                    "SELECT count(*) FROM links WHERE last_error IS NOT NULL"
                ).fetchone()[0],
                "paid_imported_at": int(last[0]) if last else None,
            }
