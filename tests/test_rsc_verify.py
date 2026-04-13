import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

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

    def test_merged_job_view_builds_normalized_shape(self):
        merged = rsc_verify.merged_job_view(
            {"JobId": "10", "JobState": "COMPLETED", "NumCPUs": "8", "CPUs/Task": "4"},
            {"rows": [{"JobID": "10", "State": "COMPLETED", "NTasks": "2", "NNodes": "1"}]},
            {"SLURM_NTASKS": "2", "SLURM_CPUS_PER_TASK": "4"},
            {"SLURM_JOB_ID": "10"},
        )
        self.assertEqual(merged["normalized"]["job_id"], "10")
        self.assertEqual(merged["normalized"]["state"], "COMPLETED")
        self.assertEqual(merged["normalized"]["ntasks"], "2")
        self.assertEqual(merged["normalized"]["cpus_per_task"], "4")

    def test_evaluate_case_pass(self):
        case_data = {
            "id": "ok",
            "description": "ok",
            "submit_mode": "sbatch",
            "expect": {
                "submit_result": "accepted",
                "env": {
                    "OMP_NUM_THREADS": "2",
                    "SLURM_RSC_T": "2",
                },
                "env_absent": ["SLURM_RSC_G"],
                "job": {"JobState": "COMPLETED"},
                "job_normalized": {"cpus_per_task": "4", "ntasks": "2"},
                "stdout_contains": ["hello"],
            },
        }
        result_data = {
            "submit": {"returncode": 0, "stdout": "", "stderr": ""},
            "env": {"OMP_NUM_THREADS": "2", "SLURM_RSC_T": "2"},
            "job": {
                "JobState": "COMPLETED",
                "normalized": {"cpus_per_task": "4", "ntasks": "2"},
            },
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
            "job": {"normalized": {}},
            "stdout": "",
            "stderr": "",
        }
        failures = rsc_verify.evaluate_case(case_data, result_data)
        self.assertEqual(len(failures), 2)

    def test_evaluate_case_supports_contains_any_and_submit_result_in(self):
        case_data = {
            "id": "conflict",
            "description": "conflict",
            "submit_mode": "sbatch",
            "expect": {
                "submit_result_in": ["accepted", "rejected"],
                "submit_stderr_contains_any": ["conflicts with --rsc", "warning:"],
            },
        }
        result_data = {
            "submit": {"returncode": 1, "stdout": "", "stderr": "rsc: option --ntasks conflicts with --rsc"},
            "env": {},
            "job": {"normalized": {}},
            "stdout": "",
            "stderr": "",
        }
        self.assertEqual(rsc_verify.evaluate_case(case_data, result_data), [])

    def test_evaluate_case_env_absent_failure(self):
        case_data = {
            "id": "gpu",
            "description": "gpu",
            "submit_mode": "sbatch",
            "expect": {
                "submit_result": "accepted",
                "env": {"SLURM_RSC_G": "1"},
                "env_absent": ["SLURM_RSC_P", "OMP_NUM_THREADS"],
            },
        }
        result_data = {
            "submit": {"returncode": 0, "stdout": "", "stderr": ""},
            "env": {
                "SLURM_RSC_G": "1",
                "SLURM_RSC_P": "1",
                "OMP_NUM_THREADS": "2",
            },
            "job": {"normalized": {}},
            "stdout": "",
            "stderr": "",
        }
        failures = rsc_verify.evaluate_case(case_data, result_data)
        self.assertIn("env.SLURM_RSC_P expected to be absent but got '1'", failures)
        self.assertIn("env.OMP_NUM_THREADS expected to be absent but got '2'", failures)

    def test_load_case_requires_fields(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("{}")
            path = fh.name
        try:
            with self.assertRaises(ValueError):
                rsc_verify.load_case(path)
        finally:
            os.unlink(path)

    def test_format_submit_args(self):
        self.assertEqual(rsc_verify.format_submit_args([]), "(none)")
        self.assertEqual(
            rsc_verify.format_submit_args(["--rsc", "p=4:t=2:c=4"]),
            "--rsc p=4:t=2:c=4",
        )

    def test_build_case_summary(self):
        case_data = {
            "id": "cpu-defaults",
            "description": "default flow",
            "submit_mode": "sbatch",
            "submit_args": ["--rsc", "p=1:c=1"],
            "notes_ref": ["notes/slurm-rsc-option-impl-v2.md:303"],
        }
        result_data = {
            "job_id": "123",
            "job": {
                "JobState": "COMPLETED",
                "normalized": {"ntasks": "1", "cpus_per_task": "1"},
            },
            "submit": {"returncode": 0, "argv": ["sbatch", "--rsc", "p=1:c=1", "job.sh"]},
            "poll_error": None,
        }
        summary = rsc_verify.build_case_summary(
            case_data, result_data, "artifacts/runs/demo/cpu-defaults", []
        )
        self.assertEqual(summary["id"], "cpu-defaults")
        self.assertEqual(summary["description"], "default flow")
        self.assertEqual(summary["submit_args"], ["--rsc", "p=1:c=1"])
        self.assertEqual(summary["submit_command"], "sbatch --rsc p=1:c=1 job.sh")
        self.assertEqual(summary["job_state"], "COMPLETED")
        self.assertEqual(summary["normalized_job"]["ntasks"], "1")
        self.assertEqual(summary["artifacts_dir"], "artifacts/runs/demo/cpu-defaults")

    def test_print_case_start_includes_submit_command(self):
        case_data = {
            "id": "cpu-defaults",
            "description": "default flow",
            "submit_mode": "sbatch",
            "submit_args": ["--rsc", "p=1:c=1"],
            "notes_ref": ["notes/slurm-rsc-option-impl-v2.md:303"],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            rsc_verify.print_case_start(
                case_data,
                ["sbatch", "--output", "job.stdout", "--error", "job.stderr", "--rsc", "p=1:c=1", "runtime_wrapper.sh"],
                1,
                3,
            )
        text = buf.getvalue()
        self.assertIn("[1/3] cpu-defaults", text)
        self.assertIn("submit_command: sbatch --output job.stdout --error job.stderr --rsc p=1:c=1 runtime_wrapper.sh", text)

    def test_print_case_result_failure_includes_artifacts(self):
        case_summary = {
            "id": "bad",
            "description": "bad case",
            "passed": False,
            "failures": ["submit_result expected 'accepted' but got 'rejected'"],
            "job_id": "456",
            "job_state": "FAILED",
            "submit_returncode": 1,
            "artifacts_dir": "artifacts/runs/demo/bad",
            "poll_error": "timeout",
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            rsc_verify.print_case_result(case_summary)
        text = buf.getvalue()
        self.assertIn("FAIL", text)
        self.assertIn("job_id=456", text)
        self.assertIn("failure:", text)
        self.assertIn("artifacts/runs/demo/bad", text)
        self.assertIn("poll_error: timeout", text)

    def test_print_summary_uses_description(self):
        summary = {
            "run_id": "demo-run",
            "passed": 1,
            "failed": 1,
            "cases": [
                {
                    "id": "ok",
                    "description": "success case",
                    "passed": True,
                    "failures": [],
                    "job_id": "10",
                    "job_state": "COMPLETED",
                    "submit_command": "sbatch --rsc p=1:c=1 ok.sh",
                    "artifacts_dir": "artifacts/runs/demo/ok",
                },
                {
                    "id": "bad",
                    "description": "failure case",
                    "passed": False,
                    "failures": ["something failed"],
                    "job_id": None,
                    "job_state": None,
                    "submit_command": "sbatch --rsc p=1:c=1 bad.sh",
                    "artifacts_dir": "artifacts/runs/demo/bad",
                },
            ],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            rsc_verify.print_summary(summary)
        text = buf.getvalue()
        self.assertIn("PASS ok - success case", text)
        self.assertIn("FAIL bad - failure case", text)
        self.assertIn("submit_command: sbatch --rsc p=1:c=1 ok.sh", text)
        self.assertIn("artifacts/runs/demo/bad", text)

    def test_render_markdown_report(self):
        summary = {
            "run_id": "demo-run",
            "started_at": "20260413T000000Z",
            "completed_at": "20260413T000100Z",
            "passed": 1,
            "failed": 0,
            "cases": [
                {
                    "id": "cpu-defaults",
                    "description": "default flow",
                    "submit_mode": "sbatch",
                    "submit_command": "sbatch --rsc p=1:c=1 job.sh",
                    "job_id": "123",
                    "job_state": "COMPLETED",
                    "passed": True,
                    "failures": [],
                    "notes_ref": ["notes/slurm-rsc-option-impl-v2.md:303"],
                    "artifacts_dir": "artifacts/runs/demo-run/cpu-defaults",
                }
            ],
        }
        detailed_cases = [
            {
                "summary": summary["cases"][0],
                "case": {"expect": {"submit_result": "accepted"}},
                "submit": {"stderr": ""},
                "job": {
                    "env": {"SLURM_RSC_P": "1", "OMP_NUM_THREADS": "1"},
                    "job": {"normalized": {"ntasks": "1", "cpus_per_task": "1"}},
                },
                "stdout": "cpu-defaults\n",
            }
        ]
        text = rsc_verify.render_markdown_report("artifacts/runs/demo-run", summary, detailed_cases)
        self.assertIn("# rsc_verify Run Report", text)
        self.assertIn("### PASS cpu-defaults", text)
        self.assertIn("Observed env:", text)
        self.assertIn("Expected checks:", text)

    def test_render_markdown_issues(self):
        summary = {
            "run_id": "demo-run",
            "failed": 1,
            "cases": [
                {
                    "id": "warn-or-deny-conflict",
                    "description": "conflict case",
                    "submit_command": "sbatch --ntasks 2 --rsc p=1:c=1 job.sh",
                    "job_id": None,
                    "job_state": None,
                    "passed": False,
                    "failures": ["submit.stderr does not contain any expected text: conflicts with --rsc, warning:"],
                    "notes_ref": ["notes/slurm-rsc-option-impl-v2.md:759"],
                    "artifacts_dir": "artifacts/runs/demo-run/warn-or-deny-conflict",
                }
            ],
        }
        detailed_cases = [
            {
                "summary": summary["cases"][0],
                "case": {"expect": {"submit_result_in": ["accepted", "rejected"]}},
                "submit": {"returncode": 1, "stdout": "", "stderr": "unexpected stderr"},
                "job": {
                    "env": {},
                    "job": {"JobState": None, "normalized": {"ntasks": "1"}},
                },
            }
        ]
        text = rsc_verify.render_markdown_issues(
            "artifacts/runs/demo-run",
            summary,
            detailed_cases,
            "Codex 改善依頼メモ",
        )
        self.assertIn("# Codex 改善依頼メモ", text)
        self.assertIn("### warn-or-deny-conflict", text)
        self.assertIn("差分:", text)
        self.assertIn("Codex への依頼ポイント:", text)

    def test_load_run_details_reads_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = {
                "run_id": "demo-run",
                "cases": [
                    {
                        "id": "cpu-defaults",
                        "description": "default flow",
                        "passed": True,
                        "failures": [],
                    }
                ],
            }
            os.mkdir(os.path.join(tmpdir, "cpu-defaults"))
            with open(os.path.join(tmpdir, "summary.json"), "w") as fh:
                json.dump(summary, fh)
            with open(os.path.join(tmpdir, "cpu-defaults", "case.json"), "w") as fh:
                json.dump({"id": "cpu-defaults"}, fh)
            with open(os.path.join(tmpdir, "cpu-defaults", "submit.json"), "w") as fh:
                json.dump({"returncode": 0}, fh)
            with open(os.path.join(tmpdir, "cpu-defaults", "job.json"), "w") as fh:
                json.dump({"env": {}}, fh)
            with open(os.path.join(tmpdir, "cpu-defaults", "assertions.json"), "w") as fh:
                json.dump({"passed": True}, fh)
            with open(os.path.join(tmpdir, "cpu-defaults", "stdout.txt"), "w") as fh:
                fh.write("stdout")
            with open(os.path.join(tmpdir, "cpu-defaults", "stderr.txt"), "w") as fh:
                fh.write("stderr")

            loaded_summary, detailed_cases = rsc_verify.load_run_details(tmpdir)
            self.assertEqual(loaded_summary["run_id"], "demo-run")
            self.assertEqual(detailed_cases[0]["stdout"], "stdout")
            self.assertEqual(detailed_cases[0]["stderr"], "stderr")


if __name__ == "__main__":
    unittest.main()
