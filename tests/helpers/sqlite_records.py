"""Raw SQLite fault injection using legacy locators for existing test scenarios."""

from yipilot.persistence.database import Database
from yipilot.persistence.event_journal import JsonlEventJournal


def event_text(path):
    return "".join(event.model_dump_json() + "\n" for event in JsonlEventJournal(path).read_after(0))


def _record(path):
    runtime = next(p for p in path.parents if p.name == ".yipilot")
    parts = path.relative_to(runtime).parts
    if path.name == "metadata.json":
        kind, identifier = "repo", f"{parts[1]}:{parts[3]}"
    else:
        kind = {"records": "artifact", "approvals": "approval", "tool-runs": "tool"}[path.parent.name]
        identifier = path.stem
    return Database(runtime.parent), kind, identifier


def read_metadata(path):
    database, kind, identifier = _record(path)
    return database.record(kind, identifier, "TEST_RECORD_MISSING")


def write_metadata(path, body):
    database, kind, identifier = _record(path)
    with database.transaction() as connection:
        connection.execute("UPDATE records SET body=? WHERE kind=? AND id=?", (body, kind, identifier))


def delete_metadata(path):
    database, kind, identifier = _record(path)
    with database.transaction() as connection:
        connection.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, identifier))
