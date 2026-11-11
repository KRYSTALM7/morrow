"""SQLite-backed commitments and shared-expense records, separate from the wallet ledger."""
from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import date, datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FinanceError(Exception):
    """Base class for safe, expected finance-store errors."""


class FinanceNotFound(FinanceError):
    pass


class FinanceConflict(FinanceError):
    pass


class FinanceValidation(FinanceError):
    pass


class IdempotencyConflict(FinanceConflict):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _shift_month(anchor: date, months: int) -> date:
    ordinal = anchor.year * 12 + anchor.month - 1 + months
    year, month0 = divmod(ordinal, 12)
    month = month0 + 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def _due_date(anchor_text: str, frequency: str, interval_days: int | None,
              offset: int) -> str:
    anchor = date.fromisoformat(anchor_text)
    if frequency == "weekly":
        result = anchor.fromordinal(anchor.toordinal() + 7 * offset)
    elif frequency == "monthly":
        result = _shift_month(anchor, offset)
    elif frequency == "annual":
        result = _shift_month(anchor, 12 * offset)
    else:
        result = anchor.fromordinal(anchor.toordinal() + int(interval_days) * offset)
    return result.isoformat()


class FinanceStore:
    """Versioned SQLite storage with owner-scoped records and atomic idempotent writes."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._migrate()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _migrate(self):
        with self._lock, closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS finance_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            version = db.execute("SELECT COALESCE(MAX(version), 0) FROM finance_schema_migrations").fetchone()[0]
            if version < 1:
                db.executescript("""
                    CREATE TABLE commitments (
                      commitment_id TEXT PRIMARY KEY,
                      owner_user_id TEXT NOT NULL,
                      title TEXT NOT NULL,
                      category TEXT NOT NULL,
                      amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                      currency TEXT NOT NULL CHECK(length(currency) = 3),
                      frequency TEXT NOT NULL CHECK(frequency IN ('weekly','monthly','annual','custom')),
                      interval_days INTEGER,
                      schedule_anchor_date TEXT NOT NULL,
                      series_start_number INTEGER NOT NULL DEFAULT 1,
                      status TEXT NOT NULL CHECK(status IN ('active','paused','cancelled')),
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      version INTEGER NOT NULL DEFAULT 1
                    );
                    CREATE INDEX commitments_owner_status ON commitments(owner_user_id, status, updated_at DESC);
                    CREATE TABLE commitment_occurrences (
                      occurrence_id TEXT PRIMARY KEY,
                      commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
                      owner_user_id TEXT NOT NULL,
                      occurrence_number INTEGER NOT NULL CHECK(occurrence_number > 0),
                      due_date TEXT NOT NULL,
                      amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                      status TEXT NOT NULL CHECK(status IN ('unpaid','paid','paused','cancelled')),
                      paid_at TEXT,
                      payment_reference TEXT,
                      created_at TEXT NOT NULL,
                      UNIQUE(commitment_id, occurrence_number)
                    );
                    CREATE INDEX commitment_occurrences_owner_due ON commitment_occurrences(owner_user_id, due_date, status);
                    CREATE TABLE shared_expenses (
                      expense_id TEXT PRIMARY KEY,
                      owner_user_id TEXT NOT NULL,
                      description TEXT NOT NULL,
                      category TEXT NOT NULL,
                      gross_amount_minor INTEGER NOT NULL CHECK(gross_amount_minor > 0),
                      personal_share_minor INTEGER NOT NULL CHECK(personal_share_minor >= 0),
                      currency TEXT NOT NULL CHECK(length(currency) = 3),
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      version INTEGER NOT NULL DEFAULT 1
                    );
                    CREATE INDEX shared_expenses_owner_created ON shared_expenses(owner_user_id, created_at DESC);
                    CREATE TABLE shared_expense_shares (
                      expense_id TEXT NOT NULL REFERENCES shared_expenses(expense_id),
                      owner_user_id TEXT NOT NULL,
                      participant_user_id TEXT NOT NULL,
                      participant_handle TEXT NOT NULL,
                      share_minor INTEGER NOT NULL CHECK(share_minor > 0),
                      PRIMARY KEY(expense_id, participant_user_id)
                    );
                    CREATE INDEX shared_shares_owner_participant ON shared_expense_shares(owner_user_id, participant_user_id);
                    CREATE TABLE shared_expense_settlements (
                      settlement_id TEXT PRIMARY KEY,
                      expense_id TEXT NOT NULL,
                      owner_user_id TEXT NOT NULL,
                      participant_user_id TEXT NOT NULL,
                      amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                      source TEXT NOT NULL CHECK(source IN ('manual','explicitly_reconciled')),
                      payment_reference TEXT,
                      actor_user_id TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      FOREIGN KEY(expense_id, participant_user_id)
                        REFERENCES shared_expense_shares(expense_id, participant_user_id)
                    );
                    CREATE INDEX shared_settlements_expense_created ON shared_expense_settlements(owner_user_id, expense_id, created_at);
                    CREATE TRIGGER shared_settlements_no_update BEFORE UPDATE ON shared_expense_settlements
                      BEGIN SELECT RAISE(ABORT, 'settlement rows are immutable'); END;
                    CREATE TABLE finance_idempotency (
                      owner_user_id TEXT NOT NULL,
                      operation TEXT NOT NULL,
                      idempotency_key TEXT NOT NULL,
                      request_hash TEXT NOT NULL,
                      response_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      PRIMARY KEY(owner_user_id, operation, idempotency_key)
                    );
                    CREATE TABLE finance_audit_events (
                      event_id TEXT PRIMARY KEY,
                      owner_user_id TEXT NOT NULL,
                      actor_user_id TEXT NOT NULL,
                      resource_type TEXT NOT NULL,
                      resource_id TEXT NOT NULL,
                      event_type TEXT NOT NULL,
                      at TEXT NOT NULL,
                      details_json TEXT NOT NULL
                    );
                    CREATE TRIGGER finance_audit_events_no_update BEFORE UPDATE ON finance_audit_events
                      BEGIN SELECT RAISE(ABORT, 'finance audit events are immutable'); END;
                """)
                db.execute("INSERT INTO finance_schema_migrations(version, applied_at) VALUES(1, ?)", (utc_now(),))
                version = 1
            if version < 2:
                db.executescript("""
                    CREATE TABLE financial_preferences (
                      owner_user_id TEXT PRIMARY KEY,
                      savings_target_minor INTEGER NOT NULL DEFAULT 0 CHECK(savings_target_minor >= 0),
                      safety_buffer_minor INTEGER NOT NULL DEFAULT 0 CHECK(safety_buffer_minor >= 0),
                      history_coverage_started_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      version INTEGER NOT NULL DEFAULT 1
                    );
                    CREATE TABLE forecast_snapshots (
                      forecast_id TEXT PRIMARY KEY,
                      owner_user_id TEXT NOT NULL,
                      period_start TEXT NOT NULL,
                      period_end TEXT NOT NULL,
                      calculation_date TEXT NOT NULL,
                      model_version TEXT NOT NULL,
                      inputs_json TEXT NOT NULL,
                      result_json TEXT NOT NULL,
                      created_at TEXT NOT NULL
                    );
                    CREATE INDEX forecast_snapshots_owner_period
                      ON forecast_snapshots(owner_user_id, period_start, created_at DESC);
                """)
                db.execute("INSERT INTO finance_schema_migrations(version, applied_at) VALUES(2, ?)", (utc_now(),))

    @staticmethod
    def _request_hash(payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _mutate(self, owner_user_id: str, operation: str, key: str, payload: dict, callback):
        request_hash = self._request_hash(payload)
        with self._lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute("SELECT request_hash,response_json FROM finance_idempotency WHERE owner_user_id=? AND operation=? AND idempotency_key=?",
                                 (owner_user_id, operation, key)).fetchone()
                if old:
                    if old["request_hash"] != request_hash:
                        raise IdempotencyConflict("Idempotency-Key was already used with different input")
                    result = json.loads(old["response_json"])
                    db.execute("COMMIT")
                    return 200, result
                result = callback(db)
                db.execute("INSERT INTO finance_idempotency(owner_user_id,operation,idempotency_key,request_hash,response_json,created_at) VALUES(?,?,?,?,?,?)",
                           (owner_user_id, operation, key, request_hash,
                            json.dumps(result, sort_keys=True, separators=(",", ":")), utc_now()))
                db.execute("COMMIT")
                return 201, result
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @staticmethod
    def _audit(db, owner, actor, resource_type, resource_id, event_type, details):
        db.execute("INSERT INTO finance_audit_events(event_id,owner_user_id,actor_user_id,resource_type,resource_id,event_type,at,details_json) VALUES(?,?,?,?,?,?,?,?)",
                   (_id("fev"), owner, actor, resource_type, resource_id, event_type, utc_now(),
                    json.dumps(details, sort_keys=True, separators=(",", ":"))))

    @staticmethod
    def _commitment_row(db, row):
        result = dict(row)
        next_due = db.execute("SELECT MIN(due_date) FROM commitment_occurrences WHERE commitment_id=? AND status='unpaid'",
                              (result["commitment_id"],)).fetchone()[0]
        result["next_due_date"] = next_due
        return result

    @classmethod
    def _generate_occurrences(cls, db, commitment):
        if commitment["status"] != "active":
            return
        unpaid = db.execute("SELECT COUNT(*) FROM commitment_occurrences WHERE commitment_id=? AND status='unpaid'",
                            (commitment["commitment_id"],)).fetchone()[0]
        to_add = max(0, 12 - unpaid)
        if not to_add:
            return
        max_number = db.execute("SELECT COALESCE(MAX(occurrence_number),?) FROM commitment_occurrences WHERE commitment_id=?",
                                (commitment["series_start_number"] - 1, commitment["commitment_id"])).fetchone()[0]
        for number in range(max_number + 1, max_number + to_add + 1):
            due = _due_date(commitment["schedule_anchor_date"], commitment["frequency"],
                            commitment["interval_days"], number - commitment["series_start_number"])
            db.execute("INSERT INTO commitment_occurrences(occurrence_id,commitment_id,owner_user_id,occurrence_number,due_date,amount_minor,status,created_at) VALUES(?,?,?,?,?,?,'unpaid',?)",
                       (_id("occ"), commitment["commitment_id"], commitment["owner_user_id"], number,
                        due, commitment["amount_minor"], utc_now()))

    @classmethod
    def _commitment_detail(cls, db, commitment_id, owner_user_id):
        row = db.execute("SELECT * FROM commitments WHERE commitment_id=? AND owner_user_id=?",
                         (commitment_id, owner_user_id)).fetchone()
        if row is None:
            raise FinanceNotFound("Commitment not found")
        result = cls._commitment_row(db, row)
        occurrences = db.execute("SELECT * FROM commitment_occurrences WHERE commitment_id=? ORDER BY occurrence_number LIMIT 200",
                                 (commitment_id,)).fetchall()
        result["occurrences"] = [dict(item) for item in occurrences]
        return result

    def create_commitment(self, owner_user_id, values, key):
        def create(db):
            commitment_id = _id("cm")
            now = utc_now()
            db.execute("INSERT INTO commitments(commitment_id,owner_user_id,title,category,amount_minor,currency,frequency,interval_days,schedule_anchor_date,series_start_number,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,'active',?,?)",
                       (commitment_id, owner_user_id, values["title"], values["category"],
                        values["amount_minor"], values["currency"], values["frequency"],
                        values["interval_days"], values["next_due_date"], now, now))
            row = db.execute("SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)).fetchone()
            self._generate_occurrences(db, row)
            self._audit(db, owner_user_id, owner_user_id, "commitment", commitment_id,
                        "commitment_created", values)
            return self._commitment_detail(db, commitment_id, owner_user_id)
        return self._mutate(owner_user_id, "commitment:create", key, values, create)

    def list_commitments(self, owner_user_id, status=None, limit=50, offset=0):
        with self._lock, closing(self._connect()) as db:
            params = [owner_user_id]
            where = "owner_user_id=?"
            if status is not None:
                where += " AND status=?"; params.append(status)
            total = db.execute(f"SELECT COUNT(*) FROM commitments WHERE {where}", params).fetchone()[0]
            rows = db.execute(f"SELECT * FROM commitments WHERE {where} ORDER BY updated_at DESC, commitment_id LIMIT ? OFFSET ?",
                              [*params, limit, offset]).fetchall()
            return {"commitments": [self._commitment_row(db, row) for row in rows],
                    "has_more": offset + len(rows) < total}

    def get_commitment(self, owner_user_id, commitment_id):
        with self._lock, closing(self._connect()) as db:
            return self._commitment_detail(db, commitment_id, owner_user_id)

    def list_occurrences(self, owner_user_id, commitment_id, status=None, limit=50, offset=0):
        with self._lock, closing(self._connect()) as db:
            self._commitment_detail(db, commitment_id, owner_user_id)
            where = "commitment_id=? AND owner_user_id=?"
            params = [commitment_id, owner_user_id]
            if status is not None:
                where += " AND status=?"; params.append(status)
            total = db.execute(f"SELECT COUNT(*) FROM commitment_occurrences WHERE {where}", params).fetchone()[0]
            rows = db.execute(f"SELECT * FROM commitment_occurrences WHERE {where} ORDER BY due_date, occurrence_number LIMIT ? OFFSET ?",
                              [*params, limit, offset]).fetchall()
            today = datetime.now(timezone.utc).date().isoformat()
            result = []
            for row in rows:
                item = dict(row)
                item["display_status"] = "overdue" if item["status"] == "unpaid" and item["due_date"] < today else item["status"]
                result.append(item)
            return {"occurrences": result, "has_more": offset + len(rows) < total}

    def update_commitment(self, owner_user_id, commitment_id, values, key):
        operation = f"commitment:update:{commitment_id}"
        def update(db):
            row = db.execute("SELECT * FROM commitments WHERE commitment_id=? AND owner_user_id=?",
                             (commitment_id, owner_user_id)).fetchone()
            if row is None:
                raise FinanceNotFound("Commitment not found")
            if row["status"] == "cancelled":
                raise FinanceConflict("Cancelled commitments cannot be updated")
            allowed = {"title", "category", "amount_minor", "frequency", "interval_days", "next_due_date"}
            changes = {name: value for name, value in values.items() if name in allowed}
            if not changes:
                raise FinanceValidation("Provide at least one commitment field to update")
            old_schedule = (row["frequency"], row["interval_days"], row["schedule_anchor_date"])
            next_title = changes.get("title", row["title"])
            next_category = changes.get("category", row["category"])
            next_amount = changes.get("amount_minor", row["amount_minor"])
            next_frequency = changes.get("frequency", row["frequency"])
            next_interval = changes.get("interval_days", row["interval_days"])
            next_anchor = changes.get("next_due_date", row["schedule_anchor_date"])
            schedule_changed = old_schedule != (next_frequency, next_interval, next_anchor)
            if schedule_changed:
                max_number = db.execute("SELECT COALESCE(MAX(occurrence_number),0) FROM commitment_occurrences WHERE commitment_id=?",
                                        (commitment_id,)).fetchone()[0]
                last_paid = db.execute("SELECT MAX(due_date) FROM commitment_occurrences WHERE commitment_id=? AND status='paid'",
                                       (commitment_id,)).fetchone()[0]
                if last_paid and next_anchor <= last_paid:
                    raise FinanceValidation("Next due date must be after the last paid occurrence")
                changed_occurrences = db.execute("SELECT occurrence_id FROM commitment_occurrences WHERE commitment_id=? AND status IN ('unpaid','paused')",
                                                 (commitment_id,)).fetchall()
                db.execute("UPDATE commitment_occurrences SET status='cancelled' WHERE commitment_id=? AND status IN ('unpaid','paused')",
                           (commitment_id,))
                series_start = max_number + 1
            else:
                changed_occurrences = []
                series_start = row["series_start_number"]
                if "amount_minor" in changes:
                    db.execute("UPDATE commitment_occurrences SET amount_minor=? WHERE commitment_id=? AND status IN ('unpaid','paused')",
                               (next_amount, commitment_id))
            now = utc_now()
            db.execute("UPDATE commitments SET title=?,category=?,amount_minor=?,frequency=?,interval_days=?,schedule_anchor_date=?,series_start_number=?,updated_at=?,version=version+1 WHERE commitment_id=? AND owner_user_id=?",
                       (next_title, next_category, next_amount, next_frequency, next_interval,
                        next_anchor, series_start, now, commitment_id, owner_user_id))
            row = db.execute("SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)).fetchone()
            self._generate_occurrences(db, row)
            self._audit(db, owner_user_id, owner_user_id, "commitment", commitment_id,
                        "commitment_updated", {**changes,
                          "rescheduled_occurrence_ids": [item["occurrence_id"] for item in changed_occurrences]})
            return self._commitment_detail(db, commitment_id, owner_user_id)
        return self._mutate(owner_user_id, operation, key, values, update)

    def set_commitment_status(self, owner_user_id, commitment_id, action, key):
        operation = f"commitment:{action}:{commitment_id}"
        values = {"action": action, "commitment_id": commitment_id}
        def change(db):
            row = db.execute("SELECT * FROM commitments WHERE commitment_id=? AND owner_user_id=?",
                             (commitment_id, owner_user_id)).fetchone()
            if row is None:
                raise FinanceNotFound("Commitment not found")
            statuses = {"pause": ("active", "paused"), "resume": ("paused", "active"),
                        "cancel": (("active", "paused"), "cancelled")}
            allowed, new_status = statuses[action]
            if row["status"] == new_status:
                return self._commitment_detail(db, commitment_id, owner_user_id)
            if row["status"] not in allowed:
                raise FinanceConflict(f"Cannot {action} a {row['status']} commitment")
            now = utc_now()
            db.execute("UPDATE commitments SET status=?,updated_at=?,version=version+1 WHERE commitment_id=?",
                       (new_status, now, commitment_id))
            occurrence_status = "paused" if action == "pause" else "unpaid" if action == "resume" else "cancelled"
            old_statuses = ("unpaid", "paused") if action == "cancel" else (("unpaid",) if action == "pause" else ("paused",))
            placeholders = ",".join("?" for _ in old_statuses)
            db.execute(f"UPDATE commitment_occurrences SET status=? WHERE commitment_id=? AND status IN ({placeholders})",
                       (occurrence_status, commitment_id, *old_statuses))
            updated = db.execute("SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)).fetchone()
            if action == "resume": self._generate_occurrences(db, updated)
            event_type = {"pause": "commitment_paused", "resume": "commitment_resumed",
                          "cancel": "commitment_cancelled"}[action]
            self._audit(db, owner_user_id, owner_user_id, "commitment", commitment_id,
                        event_type, {})
            return self._commitment_detail(db, commitment_id, owner_user_id)
        return self._mutate(owner_user_id, operation, key, values, change)

    def pay_occurrence(self, owner_user_id, commitment_id, occurrence_id, payment_reference, key):
        operation = f"commitment:pay:{commitment_id}:{occurrence_id}"
        values = {"payment_reference": payment_reference}
        def pay(db):
            commitment = db.execute("SELECT * FROM commitments WHERE commitment_id=? AND owner_user_id=?",
                                    (commitment_id, owner_user_id)).fetchone()
            if commitment is None: raise FinanceNotFound("Commitment not found")
            occurrence = db.execute("SELECT * FROM commitment_occurrences WHERE occurrence_id=? AND commitment_id=? AND owner_user_id=?",
                                    (occurrence_id, commitment_id, owner_user_id)).fetchone()
            if occurrence is None: raise FinanceNotFound("Commitment occurrence not found")
            if commitment["status"] != "active": raise FinanceConflict("Only active commitment occurrences can be marked paid")
            if occurrence["status"] != "unpaid": raise FinanceConflict("Commitment occurrence is not unpaid")
            paid_at = utc_now()
            db.execute("UPDATE commitment_occurrences SET status='paid',paid_at=?,payment_reference=? WHERE occurrence_id=? AND status='unpaid'",
                       (paid_at, payment_reference, occurrence_id))
            updated = db.execute("SELECT * FROM commitment_occurrences WHERE occurrence_id=?", (occurrence_id,)).fetchone()
            self._audit(db, owner_user_id, owner_user_id, "commitment_occurrence", occurrence_id,
                        "occurrence_marked_paid", {"payment_reference": payment_reference})
            self._generate_occurrences(db, commitment)
            return dict(updated)
        return self._mutate(owner_user_id, operation, key, values, pay)

    @classmethod
    def _shared_detail(cls, db, owner_user_id, expense_id):
        row = db.execute("SELECT * FROM shared_expenses WHERE expense_id=? AND owner_user_id=?",
                         (expense_id, owner_user_id)).fetchone()
        if row is None: raise FinanceNotFound("Shared expense not found")
        result = dict(row)
        shares = db.execute("""SELECT s.*, COALESCE(SUM(r.amount_minor),0) AS reimbursed_minor
            FROM shared_expense_shares s LEFT JOIN shared_expense_settlements r
              ON r.expense_id=s.expense_id AND r.participant_user_id=s.participant_user_id
            WHERE s.expense_id=? AND s.owner_user_id=?
            GROUP BY s.expense_id,s.participant_user_id ORDER BY s.participant_handle""",
            (expense_id, owner_user_id)).fetchall()
        participants = []
        for share in shares:
            item = dict(share)
            item["outstanding_minor"] = item["share_minor"] - item["reimbursed_minor"]
            item["status"] = "settled" if item["outstanding_minor"] == 0 else "partial" if item["reimbursed_minor"] else "open"
            participants.append(item)
        total_receivable = sum(item["share_minor"] for item in participants)
        total_reimbursed = sum(item["reimbursed_minor"] for item in participants)
        result["participants"] = participants
        result["total_receivable_minor"] = total_receivable
        result["total_reimbursed_minor"] = total_reimbursed
        result["outstanding_receivable_minor"] = total_receivable - total_reimbursed
        result["status"] = "settled" if result["outstanding_receivable_minor"] == 0 else "open"
        return result

    def create_shared_expense(self, owner_user_id, values, key):
        def create(db):
            expense_id = _id("se")
            now = utc_now()
            db.execute("INSERT INTO shared_expenses(expense_id,owner_user_id,description,category,gross_amount_minor,personal_share_minor,currency,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (expense_id, owner_user_id, values["description"], values["category"],
                        values["gross_amount_minor"], values["personal_share_minor"], values["currency"], now, now))
            for participant in values["participants"]:
                db.execute("INSERT INTO shared_expense_shares(expense_id,owner_user_id,participant_user_id,participant_handle,share_minor) VALUES(?,?,?,?,?)",
                           (expense_id, owner_user_id, participant["user_id"], participant["handle"], participant["share_minor"]))
            self._audit(db, owner_user_id, owner_user_id, "shared_expense", expense_id,
                        "shared_expense_created", {"gross_amount_minor": values["gross_amount_minor"],
                                                   "personal_share_minor": values["personal_share_minor"],
                                                   "participant_count": len(values["participants"])})
            return self._shared_detail(db, owner_user_id, expense_id)
        return self._mutate(owner_user_id, "shared_expense:create", key, values, create)

    def list_shared_expenses(self, owner_user_id, status=None, limit=50, offset=0):
        with self._lock, closing(self._connect()) as db:
            rows = db.execute("SELECT expense_id FROM shared_expenses WHERE owner_user_id=? ORDER BY created_at DESC, expense_id LIMIT ? OFFSET ?",
                              (owner_user_id, limit, offset)).fetchall()
            all_rows = [self._shared_detail(db, owner_user_id, row["expense_id"]) for row in rows]
            if status is not None: all_rows = [row for row in all_rows if row["status"] == status]
            # Filter before paging for a correct has_more signal.
            if status is not None:
                ids = db.execute("SELECT expense_id FROM shared_expenses WHERE owner_user_id=? ORDER BY created_at DESC,expense_id",
                                 (owner_user_id,)).fetchall()
                filtered = [self._shared_detail(db, owner_user_id, row["expense_id"]) for row in ids]
                filtered = [row for row in filtered if row["status"] == status]
                return {"expenses": filtered[offset:offset + limit], "has_more": offset + limit < len(filtered)}
            total = db.execute("SELECT COUNT(*) FROM shared_expenses WHERE owner_user_id=?", (owner_user_id,)).fetchone()[0]
            return {"expenses": all_rows, "has_more": offset + len(all_rows) < total}

    def shared_expense_summary(self, owner_user_id):
        """Aggregate this owner's receivables without loading participant identities."""
        with self._lock, closing(self._connect()) as db:
            row = db.execute("""SELECT COUNT(DISTINCT CASE
                      WHEN s.share_minor>COALESCE(r.reimbursed_minor,0) THEN e.expense_id END) AS expense_count,
                    COALESCE(SUM(CASE WHEN s.share_minor>COALESCE(r.reimbursed_minor,0)
                      THEN s.share_minor-COALESCE(r.reimbursed_minor,0) ELSE 0 END),0) AS outstanding_minor
                FROM shared_expenses e
                LEFT JOIN shared_expense_shares s
                  ON s.expense_id=e.expense_id AND s.owner_user_id=e.owner_user_id
                LEFT JOIN (SELECT expense_id,participant_user_id,SUM(amount_minor) AS reimbursed_minor
                    FROM shared_expense_settlements WHERE owner_user_id=?
                    GROUP BY expense_id,participant_user_id) r
                  ON r.expense_id=s.expense_id AND r.participant_user_id=s.participant_user_id
                WHERE e.owner_user_id=?""",
                (owner_user_id, owner_user_id)).fetchone()
            return {"open_expense_count": row["expense_count"],
                    "outstanding_receivable_minor": row["outstanding_minor"]}

    def get_shared_expense(self, owner_user_id, expense_id):
        with self._lock, closing(self._connect()) as db:
            return self._shared_detail(db, owner_user_id, expense_id)

    def list_settlements(self, owner_user_id, expense_id):
        with self._lock, closing(self._connect()) as db:
            self._shared_detail(db, owner_user_id, expense_id)
            rows = db.execute("SELECT * FROM shared_expense_settlements WHERE owner_user_id=? AND expense_id=? ORDER BY created_at,settlement_id",
                              (owner_user_id, expense_id)).fetchall()
            return {"settlements": [dict(row) for row in rows]}

    def settle_shared_expense(self, owner_user_id, expense_id, participant_user_id,
                              amount_minor, payment_reference, key):
        operation = f"shared_expense:settle:{expense_id}:{participant_user_id}"
        payload = {"participant_user_id": participant_user_id, "amount_minor": amount_minor,
                   "payment_reference": payment_reference, "source": "manual"}
        def settle(db):
            self._shared_detail(db, owner_user_id, expense_id)
            share = db.execute("SELECT * FROM shared_expense_shares WHERE expense_id=? AND owner_user_id=? AND participant_user_id=?",
                               (expense_id, owner_user_id, participant_user_id)).fetchone()
            if share is None: raise FinanceNotFound("Participant not found in this shared expense")
            reimbursed = db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM shared_expense_settlements WHERE expense_id=? AND participant_user_id=?",
                                    (expense_id, participant_user_id)).fetchone()[0]
            outstanding = share["share_minor"] - reimbursed
            if amount_minor > outstanding: raise FinanceConflict("Reimbursement exceeds this participant’s outstanding share")
            settlement_id = _id("set")
            created_at = utc_now()
            db.execute("INSERT INTO shared_expense_settlements(settlement_id,expense_id,owner_user_id,participant_user_id,amount_minor,source,payment_reference,actor_user_id,created_at) VALUES(?,?,?,?,?,'manual',?,?,?)",
                       (settlement_id, expense_id, owner_user_id, participant_user_id, amount_minor,
                        payment_reference, owner_user_id, created_at))
            self._audit(db, owner_user_id, owner_user_id, "shared_expense", expense_id,
                        "reimbursement_recorded", {"settlement_id": settlement_id,
                                                    "participant_user_id": participant_user_id,
                                                    "amount_minor": amount_minor,
                                                    "source": "manual"})
            detail = self._shared_detail(db, owner_user_id, expense_id)
            return {"settlement": {"settlement_id": settlement_id, "expense_id": expense_id,
                                   "participant_user_id": participant_user_id,
                                   "amount_minor": amount_minor, "source": "manual",
                                   "payment_reference": payment_reference,
                                   "actor_user_id": owner_user_id, "created_at": created_at},
                    "expense": detail}
        return self._mutate(owner_user_id, operation, key, payload, settle)

    def ensure_financial_preferences(self, owner_user_id, coverage_started_at=None):
        started = coverage_started_at or utc_now()
        with self._lock, closing(self._connect()) as db:
            db.execute("INSERT OR IGNORE INTO financial_preferences(owner_user_id,history_coverage_started_at,updated_at) VALUES(?,?,?)",
                       (owner_user_id, started, utc_now()))
            row = db.execute("SELECT * FROM financial_preferences WHERE owner_user_id=?",
                             (owner_user_id,)).fetchone()
            return dict(row)

    def update_financial_preferences(self, owner_user_id, values, key):
        def update(db):
            row = db.execute("SELECT * FROM financial_preferences WHERE owner_user_id=?",
                             (owner_user_id,)).fetchone()
            if row is None:
                raise FinanceNotFound("Financial preferences not found")
            savings = values.get("savings_target_minor", row["savings_target_minor"])
            buffer = values.get("safety_buffer_minor", row["safety_buffer_minor"])
            if not values:
                raise FinanceValidation("Provide a savings target or safety buffer")
            db.execute("UPDATE financial_preferences SET savings_target_minor=?,safety_buffer_minor=?,updated_at=?,version=version+1 WHERE owner_user_id=?",
                       (savings, buffer, utc_now(), owner_user_id))
            return dict(db.execute("SELECT * FROM financial_preferences WHERE owner_user_id=?",
                                   (owner_user_id,)).fetchone())
        return self._mutate(owner_user_id, "forecast_preferences:update", key, values, update)

    def unpaid_commitment_occurrences(self, owner_user_id, through_date):
        with self._lock, closing(self._connect()) as db:
            rows = db.execute("""SELECT o.occurrence_id,o.due_date,o.amount_minor,o.status,
                    c.commitment_id,c.title,c.category,c.currency
                FROM commitment_occurrences o JOIN commitments c USING(commitment_id)
                WHERE o.owner_user_id=? AND c.owner_user_id=? AND c.status='active'
                  AND o.status='unpaid' AND o.due_date<=?
                ORDER BY o.due_date,o.occurrence_id""",
                (owner_user_id, owner_user_id, through_date)).fetchall()
            return [dict(row) for row in rows]

    def paid_commitment_references(self, owner_user_id):
        with self._lock, closing(self._connect()) as db:
            rows = db.execute("""SELECT o.payment_reference FROM commitment_occurrences o
                JOIN commitments c USING(commitment_id)
                WHERE o.owner_user_id=? AND c.owner_user_id=? AND o.status='paid'
                  AND o.payment_reference IS NOT NULL""",
                (owner_user_id, owner_user_id)).fetchall()
            return [row["payment_reference"] for row in rows]

    def save_forecast_snapshot(self, owner_user_id, period_start, period_end,
                               calculation_date, model_version, inputs, result):
        forecast_id = _id("fc")
        created_at = utc_now()
        with self._lock, closing(self._connect()) as db:
            db.execute("INSERT INTO forecast_snapshots(forecast_id,owner_user_id,period_start,period_end,calculation_date,model_version,inputs_json,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (forecast_id, owner_user_id, period_start, period_end,
                        calculation_date, model_version,
                        json.dumps(inputs, sort_keys=True, separators=(",", ":")),
                        json.dumps(result, sort_keys=True, separators=(",", ":")),
                        created_at))
            db.execute("""DELETE FROM forecast_snapshots WHERE owner_user_id=?
                AND forecast_id NOT IN (SELECT forecast_id FROM forecast_snapshots
                    WHERE owner_user_id=? ORDER BY created_at DESC,forecast_id LIMIT 100)""",
                       (owner_user_id, owner_user_id))
        return forecast_id

    def reset_for_tests(self):
        """Clear finance-only rows for the app's explicitly gated test reset route."""
        with self._lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for table in ("forecast_snapshots", "financial_preferences", "finance_idempotency", "finance_audit_events", "shared_expense_settlements",
                              "shared_expense_shares", "shared_expenses",
                              "commitment_occurrences", "commitments"):
                    db.execute(f"DELETE FROM {table}")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
