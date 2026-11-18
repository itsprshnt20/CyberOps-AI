
"""Local alert intake, case persistence, and immutable handoffs."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

STATES = ("Open", "Investigating", "Closed")
SEVERITIES = ("Low", "Medium", "High", "Critical")
MAX_BYTES = 1024 * 1024
FIELDS = ("source", "external_id", "timestamp", "severity", "title", "entity")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,79}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
  timestamp TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL, entity TEXT NOT NULL,
  UNIQUE(source,external_id)
);
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, owner TEXT, severity TEXT NOT NULL,
  status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
  case_id TEXT REFERENCES cases(id), alert_id TEXT REFERENCES alerts(id), PRIMARY KEY(case_id,alert_id)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, case_id TEXT REFERENCES cases(id), at TEXT NOT NULL,
  kind TEXT NOT NULL, author TEXT, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
  case_id TEXT REFERENCES cases(id), revision INTEGER, case_version INTEGER NOT NULL,
  created TEXT NOT NULL, markdown TEXT NOT NULL, PRIMARY KEY(case_id,revision)
);
"""


class CaseError(ValueError):
    pass


def utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now():
    return utc(datetime.now(timezone.utc))


def field(value, label, limit, optional=False):
    if not isinstance(value, str):
        raise CaseError(f"{label} must be text.")
    value = value.strip()
    if (not value and not optional) or len(value) > limit or any(
        not char.isprintable() and char not in "\n\t" for char in value
    ):
        raise CaseError(f"{label} needs valid text up to {limit} characters.")
    return value


def parse_alert(record):
    if not isinstance(record, dict) or set(record) != set(FIELDS):
        raise CaseError("Expected exactly source, external_id, timestamp, severity, title, and entity.")
    values = {name: field(record[name], name, 180) for name in FIELDS}
    for name in ("source", "external_id", "entity"):
        if not TOKEN.fullmatch(values[name]):
            raise CaseError(f"{name} must be a short identifier.")
    values["source"] = values["source"].lower()
    values["entity"] = values["entity"].lower()
    if values["severity"] not in SEVERITIES:
        raise CaseError("severity must be Low, Medium, High, or Critical.")
    try:
        date = datetime.fromisoformat(values["timestamp"])
        if date.tzinfo is None or "T" not in values["timestamp"]:
            raise ValueError
        values["timestamp"] = utc(date)
    except (ValueError, OverflowError) as exc:
        raise CaseError("timestamp must be ISO 8601 with a UTC offset or Z.") from exc
    return tuple(values[name] for name in FIELDS)


def parse(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise CaseError("Choose a nonempty UTF-8 JSON file up to 1 MiB.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CaseError("JSON has a duplicate key.")
            result[key] = value
        return result

    try:
        records = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CaseError("Malformed JSON. Export a UTF-8 JSON alert array.") from exc
    if not isinstance(records, list) or not 1 <= len(records) <= 3000:
        raise CaseError("JSON must contain an array of 1–3,000 alerts.")
    return records


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def import_alerts(self, raw):
        records = parse(raw)
        counts = dict(new=0, duplicate=0, rejected=0, conflicting=0)
        issues = []
        with self.db(True) as db:
            for row, record in enumerate(records, 1):
                try:
                    values = parse_alert(record)
                except CaseError as exc:
                    counts["rejected"] += 1
                    issues.append(f"Record {row}: {exc}")
                    continue
                source, external_id, *_ = values
                found = db.execute("SELECT * FROM alerts WHERE source=? AND external_id=?",
                                   (source, external_id)).fetchone()
                if found is None:
                    db.execute("INSERT INTO alerts VALUES (?,?,?,?,?,?,?)", (uuid4().hex, *values))
                    counts["new"] += 1
                elif tuple(found[name] for name in FIELDS) == values:
                    counts["duplicate"] += 1
                else:
                    counts["conflicting"] += 1
                    issues.append(f"Record {row}: conflicts with saved alert {found['id']}. Original retained.")
        return counts, issues

    def alerts(self):
        with self.db() as db:
            return db.execute("SELECT * FROM alerts ORDER BY timestamp,id").fetchall()

    def list_cases(self):
        with self.db() as db:
            return db.execute("SELECT * FROM cases ORDER BY created DESC,id").fetchall()

    @staticmethod
    def get(db, case_id):
        case = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if case is None:
            raise CaseError("Case was not found.")
        return case

    def create(self, title, severity, owner):
        title = field(title, "Title", 160)
        owner = field(owner, "Owner", 100, optional=True)
        if severity not in SEVERITIES:
            raise CaseError("Choose a severity.")
        case_id, timestamp = uuid4().hex, now()
        with self.db(True) as db:
            db.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?)",
                       (case_id, title, owner, severity, "Open", timestamp, timestamp, 1))
            db.execute("INSERT INTO events(case_id,at,kind,detail) VALUES (?,?,?,?)",
                       (case_id, timestamp, "Case created", "Status: Open"))
        return case_id

    @staticmethod
    def event(db, case_id, kind, detail, author=""):
        timestamp = now()
        db.execute("INSERT INTO events(case_id,at,kind,author,detail) VALUES (?,?,?,?,?)",
                   (case_id, timestamp, kind, author, detail))
        db.execute("UPDATE cases SET version=version+1,updated=? WHERE id=?", (timestamp, case_id))

    def link(self, case_id, alert_ids):
        if not alert_ids:
            raise CaseError("Select at least one alert.")
        with self.db(True) as db:
            self.get(db, case_id)
            for alert_id in dict.fromkeys(alert_ids):
                if not db.execute("SELECT 1 FROM alerts WHERE id=?", (alert_id,)).fetchone():
                    raise CaseError("An alert was not found.")
                changes = db.execute("INSERT OR IGNORE INTO links VALUES (?,?)", (case_id, alert_id))
                if changes.rowcount:
                    self.event(db, case_id, "Alert linked", f"Alert ID {alert_id}")

    def note(self, case_id, kind, author, text):
        if kind not in {"Analyst note", "Open question"}:
            raise CaseError("Choose a note type.")
        author = field(author, "Author", 100, optional=True)
        text = field(text, "Note or question", 4000)
        with self.db(True) as db:
            self.get(db, case_id)
            self.event(db, case_id, kind, text, author)

    def change(self, case_id, status, owner, resolution, author=""):
        owner = field(owner, "Owner", 100, optional=True)
        author = field(author, "Author", 100, optional=True)
        resolution = field(resolution, "Resolution", 4000, optional=True)
        if status not in STATES:
            raise CaseError("Choose a case status.")
        with self.db(True) as db:
            case = self.get(db, case_id)
            if status == "Closed" and case["status"] != "Closed" and not resolution:
                raise CaseError("A resolution note is required to close the case.")
            if (status, owner) != (case["status"], case["owner"]):
                db.execute("UPDATE cases SET status=?,owner=? WHERE id=?", (status, owner, case_id))
                detail = (f"Status: {case['status']} to {status}. "
                          f"Owner: {case['owner'] or 'Unknown'} to {owner or 'Unknown'}.")
                if status == "Closed" and resolution:
                    detail += f"\nResolution note: {resolution}"
                self.event(db, case_id, "Case updated", detail, author)

    @staticmethod
    def records(db, case_id):
        case = Store.get(db, case_id)
        alerts = db.execute("SELECT a.* FROM alerts a JOIN links l ON l.alert_id=a.id "
                            "WHERE l.case_id=? ORDER BY a.timestamp,a.id", (case_id,)).fetchall()
        events = db.execute("SELECT * FROM events WHERE case_id=? ORDER BY at,id", (case_id,)).fetchall()
        return case, alerts, events

    def detail(self, case_id):
        with self.db() as db:
            return self.records(db, case_id)

    def preview(self, case_id, next_owner, next_action):
        next_owner = field(next_owner, "Next owner", 100)
        next_action = field(next_action, "Next action", 1000)
        with self.db() as db:
            case, alerts, events = self.records(db, case_id)
            return case["version"], briefing(case, alerts, events, next_owner, next_action)

    def finalize(self, case_id, version, next_owner, next_action, reviewed_text):
        next_owner = field(next_owner, "Next owner", 100)
        next_action = field(next_action, "Next action", 1000)
        with self.db(True) as db:
            case, alerts, events = self.records(db, case_id)
            content = briefing(case, alerts, events, next_owner, next_action)
            if version != case["version"] or content != reviewed_text:
                raise CaseError("The case or handoff changed. Review a fresh preview before saving.")
            revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM handoffs WHERE case_id=?",
                                  (case_id,)).fetchone()[0]
            timestamp = now()
            markdown = (f"# Analyst handoff · Revision {revision}\n\n"
                        f"Saved at (UTC): {timestamp}\n\n{content}")
            db.execute("INSERT INTO handoffs VALUES (?,?,?,?,?)",
                       (case_id, revision, case["version"], timestamp, markdown))
        return revision

    def revisions(self, case_id):
        with self.db() as db:
            return db.execute("SELECT * FROM handoffs WHERE case_id=? ORDER BY revision DESC",
                              (case_id,)).fetchall()


def suggestions(linked, candidates, minutes):
    if not 1 <= minutes <= 1440:
        raise CaseError("Enter a proximity of 1–1,440 minutes.")
    known = {row["id"] for row in linked}
    by_entity = {}
    for row in linked:
        by_entity.setdefault(row["entity"], []).append(datetime.fromisoformat(row["timestamp"]))
    limit = timedelta(minutes=minutes)
    return [row for row in candidates if row["id"] not in known and any(
        abs(datetime.fromisoformat(row["timestamp"]) - date) <= limit
        for date in by_entity.get(row["entity"], ())
    )]


def briefing(case, alerts, events, next_owner, next_action):
    def block(text):
        return "\n".join("    " + line for line in text.splitlines())

    def section(name, items):
        return f"## {name}\n\n" + "\n\n".join(block(item) for item in items)

    overview = "\n".join([f"Case ID: {case['id']}", f"Title: {case['title']}",
                          f"Severity: {case['severity']}", f"Status: {case['status']}",
                          f"Owner: {case['owner'] or 'Unknown'}",
                          f"Created (UTC): {case['created']}", f"Updated (UTC): {case['updated']}"])
    facts = [f"Alert ID {row['id']} · {row['source']}/{row['external_id']} · {row['timestamp']} · "
             f"{row['severity']} · {row['title']} · Entity {row['entity']}" for row in alerts]
    notes = [f"{row['author'] or 'Unknown author'} · {row['at']}\n{row['detail']}"
             for row in events if row["kind"] == "Analyst note" or
             (row["kind"] == "Case updated" and "\nResolution note:" in row["detail"])]
    questions = [f"{row['author'] or 'Unknown author'} · {row['at']}\n{row['detail']}"
                 for row in events if row["kind"] == "Open question"]
    return "\n\n".join([
        section("Case overview", [overview]),
        section("Observed facts", facts or ["No alerts linked."]),
        section("Analyst notes", notes or ["No notes recorded."]),
        section("Open questions and suggested manual checks", (questions or ["No open questions recorded."]) +
                ["Suggested manual check: Verify cited alerts in the source system and confirm the case scope."]),
        section("Next owner and action", [f"Next owner: {next_owner}\nNext action: {next_action}"]),
    ]) + "\n"
