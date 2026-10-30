"""Synthetic payment callback processor. TEACHING BASELINE: NOT production code.

This baseline intentionally records duplicate business events for valid replays.
No real money, payment gateway, personal data, or external API is involved.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from typing import Any


class CallbackError(Exception):
    """Rejected input with no business-state changes."""


class InjectedFailure(Exception):
    """Local test-only transaction failure."""


def sign(payload: dict[str, Any], key: bytes) -> str:
    body = {k: v for k, v in payload.items() if k != "signature"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


class PaymentService:
    def __init__(self, db_path: str | Path, key: bytes):
        if len(key) < 32:
            raise ValueError("Use a generated test signing key of at least 32 bytes")
        self.db_path = Path(db_path).resolve()
        self.key = key

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=20, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        """Create a fresh demo database; never overwrite an existing file."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.db_path.open("xb"):
            pass
        with self._connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE orders (
                    order_id TEXT PRIMARY KEY,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                    currency TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending', 'paid'))
                );
                CREATE TABLE payments (
                    channel TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
                    amount_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    PRIMARY KEY(channel, transaction_id)
                );
                CREATE TABLE business_events (
                    id INTEGER PRIMARY KEY,
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    channel TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type = 'payment_succeeded')
                );
            """)

    def add_order(self, order_id: str, amount_minor: int = 10000, currency: str = "CNY") -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO orders VALUES (?, ?, ?, 'pending')", (order_id, amount_minor, currency))

    def callback(self, payload: dict[str, Any], *, inject_failure: bool = False) -> dict[str, str]:
        """inject_failure is a test harness switch, NOT a request field."""
        fields = {"event_id", "channel", "transaction_id", "order_id", "amount_minor", "currency", "status", "signature"}
        if not isinstance(payload, dict) or set(payload) != fields:
            raise CallbackError("invalid_fields")
        strings = fields - {"amount_minor"}
        if any(not isinstance(payload[k], str) or not payload[k] or len(payload[k]) > 128 for k in strings):
            raise CallbackError("invalid_string")
        if type(payload["amount_minor"]) is not int or not 0 < payload["amount_minor"] <= 10**12:
            raise CallbackError("invalid_amount")
        if payload["channel"] != "demo" or payload["currency"] != "CNY" or payload["status"] not in {"succeeded", "failed"}:
            raise CallbackError("invalid_value")
        signature = payload["signature"]
        if len(signature) != 64 or any(ch not in "0123456789abcdef" for ch in signature):
            raise CallbackError("invalid_signature")
        if not hmac.compare_digest(signature, sign(payload, self.key)):
            raise CallbackError("invalid_signature")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (payload["order_id"],)).fetchone()
            if order is None:
                raise CallbackError("unknown_order")
            if (order["amount_minor"], order["currency"]) != (payload["amount_minor"], payload["currency"]):
                raise CallbackError("order_mismatch")
            existing = conn.execute("SELECT * FROM payments WHERE channel = ? AND transaction_id = ?", (payload["channel"], payload["transaction_id"])).fetchone()
            if existing and (existing["order_id"], existing["amount_minor"], existing["currency"]) != (payload["order_id"], payload["amount_minor"], payload["currency"]):
                raise CallbackError("transaction_conflict")
            # Idempotent replay: transaction already committed, do not write again.
            if existing and payload["status"] == "succeeded":
                conn.commit()
                return {"result": "already_processed", "order_id": payload["order_id"]}
            paid = conn.execute("SELECT * FROM payments WHERE order_id = ?", (payload["order_id"],)).fetchone()
            if paid and (paid["channel"], paid["transaction_id"]) != (payload["channel"], payload["transaction_id"]):
                raise CallbackError("order_already_paid")
            if payload["status"] != "succeeded":
                conn.commit()
                return {"result": "ignored", "order_id": payload["order_id"]}
            # First committed success for this transaction; replays return above.
            conn.execute("INSERT OR IGNORE INTO payments VALUES (?, ?, ?, ?, ?)", (payload["channel"], payload["transaction_id"], payload["order_id"], payload["amount_minor"], payload["currency"]))
            conn.execute("UPDATE orders SET state = 'paid' WHERE order_id = ?", (payload["order_id"],))
            if inject_failure:
                raise InjectedFailure("simulated_transaction_failure")
            conn.execute("INSERT INTO business_events(order_id, channel, transaction_id, event_type) VALUES (?, ?, ?, 'payment_succeeded')", (payload["order_id"], payload["channel"], payload["transaction_id"]))
            conn.commit()
            return {"result": "accepted", "order_id": payload["order_id"]}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def snapshot(self, order_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            order = conn.execute("SELECT state FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            return {
                "state": order["state"] if order else None,
                "payments": conn.execute("SELECT COUNT(*) FROM payments WHERE order_id = ?", (order_id,)).fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM business_events WHERE order_id = ?", (order_id,)).fetchone()[0],
            }
