"""Run real acceptance tests. --expect-baseline checks intentional known failures only."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import platform
import re
import sys
import unittest
from datetime import datetime, timezone

from test_acceptance import AcceptanceTests


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_tests = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed_tests.append(test.id().split(".")[-1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-baseline", action="store_true", help="Check that the teaching baseline fails exactly AC02/AC03/AC06. This is NOT acceptance success.")
    parser.add_argument("--output", type=Path, help="Write a new JSON evidence file; refuses overwrite")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AcceptanceTests)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        result = unittest.TextTestRunner(stream=captured, verbosity=2, resultclass=EvidenceResult).run(suite)
    transcript = captured.getvalue()
    failures = [test.id().split(".")[-1] for test, _ in result.failures]
    errors = [test.id().split(".")[-1] for test, _ in result.errors]
    expected = {"test_AC02_sequential_duplicates", "test_AC03_concurrent_duplicates", "test_AC06_new_process_replay"}
    baseline_matches = result.testsRun == 8 and not errors and not result.skipped and set(failures) == expected
    acceptance_passed = result.testsRun == 8 and result.wasSuccessful() and not result.skipped
    document = {
        "purpose": "synthetic teaching baseline; not production assurance",
        "mode": "baseline_contract" if args.expect_baseline else "acceptance",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "files_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ("payment_service.py", "test_acceptance.py", "run_checks.py")},
        "tests_run": result.testsRun,
        "passed": result.passed_tests,
        "failed": failures,
        "errors": errors,
        "skipped": len(result.skipped),
        "acceptance_passed": acceptance_passed,
        "baseline_contract_matched": baseline_matches,
        "evidence": [{"case": match.group(1), "actual": json.loads(match.group(2))} for match in re.finditer(r"EVIDENCE (AC-\d+) ([^\n]+)", transcript)],
        "transcript": transcript,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
    print(transcript, end="")
    print(json.dumps({key: document[key] for key in ("mode", "tests_run", "passed", "failed", "errors", "acceptance_passed", "baseline_contract_matched")}, ensure_ascii=False, indent=2))
    return 0 if (baseline_matches if args.expect_baseline else acceptance_passed) else 1


if __name__ == "__main__":
    sys.exit(main())
