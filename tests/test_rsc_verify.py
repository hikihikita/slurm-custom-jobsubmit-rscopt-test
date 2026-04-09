import os
import tempfile
import unittest

from tools import rsc_verify


class ParseHelpersTest(unittest.TestCase):
    def test_parse_scontrol_one_line(self):
        parsed = rsc_verify.parse_scontrol_one_line(
            "JobId=10 JobState=COMPLETED NumCPUs=4 StdOut=/tmp/out"
        )
        self.assertEqual(parsed["JobId"], "10")
        self.assertEqual(parsed["JobState"], "COMPLETED")
        self.assertEqual(parsed["NumCPUs"], "4")

    def test_parse_sacct_parsable(self):
        rows = rsc_verify.parse_sacct_parsable(
            "JobID|State|ExitCode\n123|COMPLETED|0:0\n123.batch|COMPLETED|0:0\n"
        )
        self.assertEqual(rows[0]["JobID"], "123")
        self.assertEqual(rows[0]["State"], "COMPLETED")
        self.assertEqual(rows[1]["JobID"], "123.batch")

    def test_evaluate_case_pass(self):
        case_data = {
            "id": "ok",
            "description": "ok",
            "submit_mode": "sbatch",
            "expect": {
                "submit_result": "accepted",
                "env": {"OMP_NUM_THREADS": "2"},
                "job": {"JobState": "COMPLETED"},
                "stdout_contains": ["hello"],
            },
        }
        result_data = {
            "submit": {"returncode": 0, "stdout": "", "stderr": ""},
            "env": {"OMP_NUM_THREADS": "2"},
            "job": {"JobState": "COMPLETED"},
            "stdout": "hello world",
            "stderr": "",
        }
        self.assertEqual(rsc_verify.evaluate_case(case_data, result_data), [])

    def test_evaluate_case_fail(self):
        case_data = {
            "id": "bad",
            "description": "bad",
            "submit_mode": "sbatch",
            "expect": {"submit_result": "rejected", "submit_stderr_contains": ["error"]},
        }
        result_data = {
            "submit": {"returncode": 0, "stdout": "", "stderr": "warning only"},
            "env": {},
            "job": {},
            "stdout": "",
            "stderr": "",
        }
        failures = rsc_verify.evaluate_case(case_data, result_data)
        self.assertEqual(len(failures), 2)

    def test_load_case_requires_fields(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("{}")
            path = fh.name
        try:
            with self.assertRaises(ValueError):
                rsc_verify.load_case(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
