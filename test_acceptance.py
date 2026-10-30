"""Eight immutable acceptance scenarios; baseline intentionally fails 02/03/06."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from payment_service import CallbackError, InjectedFailure, PaymentService, sign


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="payment-demo-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = os.urandom(32)
        self.service = PaymentService(self.root / "demo.sqlite3", self.key)
        self.service.initialize()
        self.service.add_order("DEMO-ORDER-001")

    def payload(self, **changes):
        p = {"event_id": "EV-001", "channel": "demo", "transaction_id": "TX-001", "order_id": "DEMO-ORDER-001", "amount_minor": 10000, "currency": "CNY", "status": "succeeded"}
        p.update(changes)
        p["signature"] = sign(p, self.key)
        return p

    def state(self):
        return self.service.snapshot("DEMO-ORDER-001")

    def test_AC01_first_valid_success(self):
        self.assertEqual(self.service.callback(self.payload())["result"], "accepted")
        actual = self.state()
        print("EVIDENCE AC-01", json.dumps(actual))
        self.assertEqual(actual, {"state": "paid", "payments": 1, "events": 1})

    def test_AC02_sequential_duplicates(self):
        results = [self.service.callback(self.payload())["result"] for _ in range(3)]
        results.append(self.service.callback(self.payload(event_id="EV-OTHER"))["result"])
        print("EVIDENCE AC-02", json.dumps({"requests": 4, "results": results, **self.state()}))
        self.assertEqual(self.state(), {"state": "paid", "payments": 1, "events": 1})
        self.assertEqual(results, ["accepted", "already_processed", "already_processed", "already_processed"])

    def test_AC03_concurrent_duplicates(self):
        records = []
        for run in range(3):
            order = f"DEMO-CONCURRENT-{run}"
            self.service.add_order(order)
            payload = self.payload(order_id=order, transaction_id=f"TX-CONCURRENT-{run}")
            gate = threading.Barrier(10, timeout=15)
            lock = threading.Lock()
            active = 0
            max_inflight = 0
            intervals = []

            def invoke():
                nonlocal active, max_inflight
                start = time.monotonic_ns()
                with lock:
                    active += 1
                    max_inflight = max(max_inflight, active)
                try:
                    gate.wait()
                    return self.service.callback(dict(payload))["result"]
                finally:
                    end = time.monotonic_ns()
                    with lock:
                        active -= 1
                        intervals.append((start, end))

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                responses = list(pool.map(lambda _: invoke(), range(10)))
            record = {"round": run + 1, "requests": 10, "max_inflight": max_inflight, "overlap_verified": max(start for start, _ in intervals) < min(end for _, end in intervals), "accepted": responses.count("accepted"), "already_processed": responses.count("already_processed"), **self.service.snapshot(order)}
            records.append(record)
        print("EVIDENCE AC-03", json.dumps(records))
        for row in records:
            self.assertEqual(row["max_inflight"], 10)
            self.assertTrue(row["overlap_verified"])
            self.assertEqual((row["state"], row["payments"], row["events"]), ("paid", 1, 1))
            self.assertEqual((row["accepted"], row["already_processed"]), (1, 9))

    def test_AC04_rejected_inputs_do_not_pollute(self):
        invalid_signature = self.payload()
        invalid_signature["signature"] = "0" * 64
        missing = self.payload()
        missing.pop("event_id")
        malformed_signature = self.payload()
        malformed_signature["signature"] = "bad"
        cases = [invalid_signature, missing, self.payload(order_id="UNKNOWN"), self.payload(amount_minor=9999), self.payload(currency="USD"), self.payload(amount_minor=True), malformed_signature]
        # Also exercise non-ASCII signature rejection without leaking an exception.
        non_ascii = self.payload()
        non_ascii["signature"] = "非" * 64
        cases.append(non_ascii)
        for i, payload in enumerate(cases):
            with self.subTest(case=i):
                with self.assertRaises(CallbackError):
                    self.service.callback(payload)
                self.assertEqual(self.state(), {"state": "pending", "payments": 0, "events": 0})
        self.service.callback(self.payload())
        self.assertEqual(self.state(), {"state": "paid", "payments": 1, "events": 1})
        print("EVIDENCE AC-04", json.dumps({"rejected_cases": len(cases), "legitimate_retry": self.state()}))

    def test_AC05_conflicting_replays(self):
        self.service.callback(self.payload())
        self.service.add_order("DEMO-ORDER-002")
        for p in [self.payload(order_id="DEMO-ORDER-002"), self.payload(amount_minor=9000), self.payload(currency="USD"), self.payload(transaction_id="TX-DIFFERENT")]:
            with self.assertRaises(CallbackError):
                self.service.callback(p)
        self.assertEqual(self.state(), {"state": "paid", "payments": 1, "events": 1})
        self.assertEqual(self.service.snapshot("DEMO-ORDER-002"), {"state": "pending", "payments": 0, "events": 0})
        print("EVIDENCE AC-05", json.dumps({"conflicts_rejected": 4, **self.state()}))

    def test_AC06_new_process_replay(self):
        self.service.callback(self.payload())
        script = """import json, sys
from payment_service import PaymentService
x=json.load(sys.stdin)
s=PaymentService(x['db'],bytes.fromhex(x['key']))
result=s.callback(x['payload'])
print(json.dumps({'result':result,'state':s.snapshot(x['payload']['order_id'])}))
"""
        completed = subprocess.run([sys.executable, "-c", script], input=json.dumps({"db": str(self.service.db_path), "key": self.key.hex(), "payload": self.payload()}), text=True, capture_output=True, timeout=30, cwd=Path(__file__).parent)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        actual = json.loads(completed.stdout)
        print("EVIDENCE AC-06", json.dumps({"new_process": True, **actual}))
        self.assertEqual(actual["state"], {"state": "paid", "payments": 1, "events": 1})
        self.assertEqual(actual["result"]["result"], "already_processed")

    def test_AC07_transaction_rollback(self):
        with self.assertRaises(InjectedFailure):
            self.service.callback(self.payload(), inject_failure=True)
        before_retry = self.state()
        self.assertEqual(before_retry, {"state": "pending", "payments": 0, "events": 0})
        self.service.callback(self.payload())
        self.assertEqual(self.state(), {"state": "paid", "payments": 1, "events": 1})
        print("EVIDENCE AC-07", json.dumps({"after_failure": before_retry, "after_retry": self.state()}))

    def test_AC08_normal_regression(self):
        self.assertEqual(self.service.callback(self.payload(status="failed"))["result"], "ignored")
        self.assertEqual(self.state(), {"state": "pending", "payments": 0, "events": 0})
        self.service.callback(self.payload())
        self.assertEqual(self.service.callback(self.payload(status="failed"))["result"], "ignored")
        self.service.add_order("DEMO-ORDER-002")
        self.service.callback(self.payload(order_id="DEMO-ORDER-002", transaction_id="TX-002"))
        for order in ["DEMO-ORDER-001", "DEMO-ORDER-002"]:
            self.assertEqual(self.service.snapshot(order), {"state": "paid", "payments": 1, "events": 1})
        with self.assertRaises(FileExistsError):
            self.service.initialize()
        print("EVIDENCE AC-08", json.dumps({"independent_orders": 2, "failed_notice_ignored": True, "overwrite_rejected": True}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
