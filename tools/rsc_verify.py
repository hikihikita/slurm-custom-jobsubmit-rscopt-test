#!/usr/bin/env python3
from __future__ import print_function

import argparse
import datetime
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid


TERMINAL_JOB_STATES = set([
    "BOOT_FAIL",
    "CANCELLED",
    "COMPLETED",
    "DEADLINE",
    "FAILED",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "TIMEOUT",
])

RUNTIME_ENV_PATTERN = r"^(SLURM_RSC|OMP_|SLURM_CPUS_PER_TASK|SLURM_NTASKS|SLURM_TASKS_PER_NODE|SLURM_JOB_NUM_NODES)="
DEFAULT_ISSUES_TITLE = "Codex 改善依頼メモ"


def utc_timestamp():
    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def read_text(path):
    with open(path, "r") as fh:
        return fh.read()


def write_text(path, content):
    with open(path, "w") as fh:
        fh.write(content)


def write_json(path, data):
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def shell_join(items):
    return " ".join(shlex.quote(item) for item in items)


def command_exists(name):
    return subprocess.call(
        ["bash", "-lc", "command -v {0} >/dev/null 2>&1".format(shlex.quote(name))]
    ) == 0


def parse_scontrol_one_line(output):
    data = {}
    for token in output.strip().split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        data[key] = value
    return data


def parse_sacct_parsable(output):
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    header = lines[0].split("|")
    rows = []
    for line in lines[1:]:
        values = line.split("|")
        if len(values) < len(header):
            values += [""] * (len(header) - len(values))
        row = {}
        for index, key in enumerate(header):
            row[key] = values[index]
        rows.append(row)
    return rows


def load_case(path):
    with open(path, "r") as fh:
        data = json.load(fh)
    required = ["id", "description", "submit_mode", "expect"]
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(
            "case {0} is missing required fields: {1}".format(
                path, ", ".join(missing)
            )
        )
    if data["submit_mode"] not in ("sbatch", "srun", "salloc"):
        raise ValueError(
            "case {0} has unsupported submit_mode: {1}".format(
                path, data["submit_mode"]
            )
        )
    data.setdefault("submit_args", [])
    data.setdefault("env", {})
    data.setdefault("notes_ref", [])
    return data


def discover_case_paths(case_id, path_glob):
    if case_id:
        return sorted(glob.glob(os.path.join("cases", "rsc", case_id + ".json")))
    if path_glob:
        return sorted(glob.glob(path_glob))
    return sorted(glob.glob(os.path.join("cases", "rsc", "*.json")))


def build_runtime_wrapper(case_dir, case_data):
    wrapper_path = os.path.join(case_dir, "runtime_wrapper.sh")
    env_dump_path = os.path.join(case_dir, "runtime_env.txt")
    meta_path = os.path.join(case_dir, "runtime_meta.txt")
    payload_path = os.path.join(case_dir, "payload.sh")

    payload = case_data.get("script")
    if payload is None:
        payload = case_data.get("command", "")

    payload_script = "#!/bin/bash\nset -euo pipefail\n" + payload.rstrip() + "\n"
    write_text(payload_path, payload_script)
    os.chmod(payload_path, 0o755)

    wrapper = """#!/bin/bash
set -euo pipefail
umask 077

cat > {meta_path} <<EOF
SLURM_JOB_ID=${{SLURM_JOB_ID:-}}
SLURM_STEP_ID=${{SLURM_STEP_ID:-}}
HOSTNAME=$(hostname)
PWD=$(pwd)
EOF

env | grep -E '{env_pattern}' | sort > {env_dump_path} || true
exec bash {payload_path}
""".format(
        meta_path=shlex.quote(meta_path),
        env_pattern=RUNTIME_ENV_PATTERN,
        env_dump_path=shlex.quote(env_dump_path),
        payload_path=shlex.quote(payload_path),
    )
    write_text(wrapper_path, wrapper)
    os.chmod(wrapper_path, 0o755)
    return wrapper_path


def parse_env_dump(text):
    env = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def extract_job_id(text):
    patterns = [
        r"Submitted batch job (\d+)",
        r"Granted job allocation (\d+)",
        r"job (\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def run_command(argv, env, cwd):
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    stdout, stderr = proc.communicate()
    return {
        "argv": argv,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def poll_job(job_id, timeout_seconds, poll_interval):
    deadline = time.time() + timeout_seconds
    last_info = {}
    while time.time() < deadline:
        result = subprocess.run(
            ["scontrol", "show", "job", "-o", job_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            last_info = parse_scontrol_one_line(result.stdout)
            state = last_info.get("JobState")
            if state in TERMINAL_JOB_STATES:
                return last_info, None
        time.sleep(poll_interval)
    return last_info, "timeout"


def fetch_sacct(job_id):
    result = subprocess.run(
        [
            "sacct",
            "-j",
            job_id,
            "--parsable2",
            "--noheader",
            "false",
            "--format",
            "JobID,JobName%30,State,ExitCode,Elapsed,NTasks,NNodes,ReqCPUS,ReqMem,ReqTRES,AllocTRES,TresPerTask",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if result.returncode != 0:
        return {"error": result.stderr.strip(), "rows": []}
    rows = parse_sacct_parsable(
        "JobID|JobName|State|ExitCode|Elapsed|NTasks|NNodes|ReqCPUS|ReqMem|ReqTRES|AllocTRES|TresPerTask\n"
        + result.stdout
    )
    return {"rows": rows}


def merged_job_view(scontrol_data, sacct_data, runtime_env, runtime_meta):
    merged = dict(scontrol_data)

    rows = sacct_data.get("rows") or []
    if rows:
        first = rows[0]
        for key, value in first.items():
            merged["sacct." + key] = value

    normalized = {}
    alias_map = {
        "job_id": [
            runtime_meta.get("SLURM_JOB_ID"),
            merged.get("JobId"),
            merged.get("sacct.JobID"),
        ],
        "state": [
            merged.get("JobState"),
            merged.get("sacct.State"),
        ],
        "nodes": [
            merged.get("NumNodes"),
            merged.get("sacct.NNodes"),
            runtime_env.get("SLURM_JOB_NUM_NODES"),
        ],
        "ntasks": [
            merged.get("NumTasks"),
            merged.get("sacct.NTasks"),
            runtime_env.get("SLURM_NTASKS"),
        ],
        "cpus_per_task": [
            merged.get("CPUs/Task"),
            runtime_env.get("SLURM_CPUS_PER_TASK"),
        ],
        "num_cpus": [
            merged.get("NumCPUs"),
            merged.get("sacct.ReqCPUS"),
        ],
        "tasks_per_node": [
            runtime_env.get("SLURM_TASKS_PER_NODE"),
        ],
        "req_tres": [
            merged.get("sacct.ReqTRES"),
            merged.get("ReqTRES"),
        ],
        "alloc_tres": [
            merged.get("sacct.AllocTRES"),
        ],
        "tres_per_task": [
            merged.get("TresPerTask"),
            merged.get("sacct.TresPerTask"),
        ],
        "req_mem": [
            merged.get("sacct.ReqMem"),
        ],
    }

    for key, candidates in alias_map.items():
        for value in candidates:
            if value not in (None, ""):
                normalized[key] = value
                break
    merged["normalized"] = normalized
    return merged


def check_contains(text, patterns, label):
    failures = []
    for pattern in patterns:
        if pattern not in text:
            failures.append("{0} does not contain expected text: {1}".format(label, pattern))
    return failures


def check_contains_any(text, patterns, label):
    if not patterns:
        return []
    for pattern in patterns:
        if pattern in text:
            return []
    return [
        "{0} does not contain any expected text: {1}".format(
            label, ", ".join(patterns)
        )
    ]


def check_mapping(actual, expected, label):
    failures = []
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if str(actual_value) != str(expected_value):
            failures.append(
                "{0}.{1} expected {2!r} but got {3!r}".format(
                    label, key, expected_value, actual_value
                )
            )
    return failures


def check_absent(actual, expected_absent, label):
    failures = []
    for key in expected_absent:
        if key in actual:
            failures.append(
                "{0}.{1} expected to be absent but got {2!r}".format(
                    label, key, actual.get(key)
                )
            )
    return failures


def format_submit_args(submit_args):
    if not submit_args:
        return "(none)"
    return shell_join(submit_args)


def build_case_summary(case_data, result_data, case_dir, failures):
    normalized = result_data.get("job", {}).get("normalized", {})
    return {
        "id": case_data["id"],
        "description": case_data.get("description", ""),
        "submit_mode": case_data["submit_mode"],
        "submit_args": list(case_data.get("submit_args", [])),
        "submit_command": shell_join(result_data["submit"]["argv"]),
        "notes_ref": list(case_data.get("notes_ref", [])),
        "passed": not failures,
        "failures": failures,
        "job_id": result_data.get("job_id"),
        "job_state": result_data.get("job", {}).get("JobState"),
        "submit_returncode": result_data["submit"]["returncode"],
        "artifacts_dir": case_dir,
        "poll_error": result_data.get("poll_error"),
        "normalized_job": normalized,
    }


def print_run_header(run_id, total_cases, run_dir):
    print("run_id:", run_id)
    print("cases:", total_cases)
    print("artifacts:", run_dir)


def print_case_start(case_data, submit_argv, index, total):
    print("")
    print("[{0}/{1}] {2}".format(index, total, case_data["id"]))
    print("  description:", case_data.get("description", ""))
    print("  submit_mode:", case_data["submit_mode"])
    print("  submit_args:", format_submit_args(case_data.get("submit_args", [])))
    print("  submit_command:", shell_join(submit_argv))
    notes_ref = case_data.get("notes_ref", [])
    if notes_ref:
        print("  notes_ref:", ", ".join(notes_ref))


def print_case_result(case_summary):
    status = "PASS" if case_summary["passed"] else "FAIL"
    suffix = []
    if case_summary.get("job_id"):
        suffix.append("job_id={0}".format(case_summary["job_id"]))
    if case_summary.get("job_state"):
        suffix.append("state={0}".format(case_summary["job_state"]))
    suffix.append("submit_rc={0}".format(case_summary["submit_returncode"]))
    print("  result: {0} ({1})".format(status, ", ".join(suffix)))
    if not case_summary["passed"]:
        for failure in case_summary.get("failures", []):
            print("  failure:", failure)
        if case_summary.get("poll_error"):
            print("  poll_error:", case_summary["poll_error"])
    print("  artifacts:", case_summary["artifacts_dir"])


def print_summary(summary):
    print("")
    print("summary:")
    print("  run_id:", summary["run_id"])
    print("  passed:", summary["passed"])
    print("  failed:", summary["failed"])
    for case in summary["cases"]:
        status = "PASS" if case["passed"] else "FAIL"
        detail = "{0} {1} - {2}".format(status, case["id"], case.get("description", ""))
        extras = []
        if case.get("job_id"):
            extras.append("job_id={0}".format(case["job_id"]))
        if case.get("job_state"):
            extras.append("state={0}".format(case["job_state"]))
        if extras:
            detail += " ({0})".format(", ".join(extras))
        print(" ", detail)
        if case.get("submit_command"):
            print("   submit_command:", case["submit_command"])
        if not case["passed"]:
            for failure in case.get("failures", []):
                print("   failure:", failure)
            print("   artifacts:", case["artifacts_dir"])


def evaluate_case(case_data, result_data):
    expect = case_data.get("expect", {})
    failures = []

    submit_result = "accepted" if result_data["submit"]["returncode"] == 0 else "rejected"
    if "submit_result" in expect and expect["submit_result"] != submit_result:
        failures.append(
            "submit_result expected {0!r} but got {1!r}".format(
                expect["submit_result"], submit_result
            )
        )
    if "submit_result_in" in expect and submit_result not in expect["submit_result_in"]:
        failures.append(
            "submit_result expected one of {0!r} but got {1!r}".format(
                expect["submit_result_in"], submit_result
            )
        )

    failures.extend(
        check_contains(
            result_data["submit"]["stdout"],
            expect.get("submit_stdout_contains", []),
            "submit.stdout",
        )
    )
    failures.extend(
        check_contains(
            result_data["submit"]["stderr"],
            expect.get("submit_stderr_contains", []),
            "submit.stderr",
        )
    )
    failures.extend(
        check_contains_any(
            result_data["submit"]["stdout"],
            expect.get("submit_stdout_contains_any", []),
            "submit.stdout",
        )
    )
    failures.extend(
        check_contains_any(
            result_data["submit"]["stderr"],
            expect.get("submit_stderr_contains_any", []),
            "submit.stderr",
        )
    )
    failures.extend(
        check_contains(result_data.get("stdout", ""), expect.get("stdout_contains", []), "stdout")
    )
    failures.extend(
        check_contains(result_data.get("stderr", ""), expect.get("stderr_contains", []), "stderr")
    )

    failures.extend(check_mapping(result_data.get("env", {}), expect.get("env", {}), "env"))
    failures.extend(check_absent(result_data.get("env", {}), expect.get("env_absent", []), "env"))
    failures.extend(check_mapping(result_data.get("job", {}), expect.get("job", {}), "job"))
    failures.extend(
        check_mapping(
            result_data.get("job", {}).get("normalized", {}),
            expect.get("job_normalized", {}),
            "job_normalized",
        )
    )

    if "job_state_in" in expect:
        actual_state = result_data.get("job", {}).get("JobState")
        if actual_state not in expect["job_state_in"]:
            failures.append(
                "job.JobState expected one of {0!r} but got {1!r}".format(
                    expect["job_state_in"], actual_state
                )
            )

    return failures


def prepare_submit_command(case_data, case_dir, wrapper_path):
    submit_mode = case_data["submit_mode"]
    submit_args = list(case_data.get("submit_args", []))
    stdout_path = os.path.join(case_dir, "job.stdout")
    stderr_path = os.path.join(case_dir, "job.stderr")

    if submit_mode == "sbatch":
        argv = ["sbatch", "--output", stdout_path, "--error", stderr_path] + submit_args + [wrapper_path]
    elif submit_mode == "srun":
        argv = ["srun"] + submit_args + ["bash", wrapper_path]
    else:
        argv = ["salloc"] + submit_args + ["bash", wrapper_path]

    return argv, stdout_path, stderr_path


def run_case(case_path, run_dir, timeout_seconds, poll_interval):
    case_data = load_case(case_path)
    case_dir = os.path.join(run_dir, case_data["id"])
    ensure_dir(case_dir)
    write_json(os.path.join(case_dir, "case.json"), case_data)

    wrapper_path = build_runtime_wrapper(case_dir, case_data)
    argv, stdout_path, stderr_path = prepare_submit_command(case_data, case_dir, wrapper_path)

    env = os.environ.copy()
    env.update(case_data.get("env", {}))
    submit_result = run_command(argv, env=env, cwd=os.getcwd())
    write_json(os.path.join(case_dir, "submit.json"), submit_result)
    write_text(os.path.join(case_dir, "submit.stdout"), submit_result["stdout"])
    write_text(os.path.join(case_dir, "submit.stderr"), submit_result["stderr"])

    combined_submit = submit_result["stdout"] + "\n" + submit_result["stderr"]
    job_id = extract_job_id(combined_submit)

    scontrol_data = {}
    sacct_data = {"rows": []}
    runtime_env = {}
    runtime_meta = {}
    poll_error = None

    if job_id and command_exists("scontrol"):
        scontrol_data, poll_error = poll_job(job_id, timeout_seconds, poll_interval)
    if job_id and command_exists("sacct"):
        sacct_data = fetch_sacct(job_id)

    if os.path.exists(stdout_path):
        job_stdout = read_text(stdout_path)
    else:
        job_stdout = submit_result["stdout"] if case_data["submit_mode"] != "sbatch" else ""
    if os.path.exists(stderr_path):
        job_stderr = read_text(stderr_path)
    else:
        job_stderr = submit_result["stderr"] if case_data["submit_mode"] != "sbatch" else ""

    runtime_env_path = os.path.join(case_dir, "runtime_env.txt")
    if os.path.exists(runtime_env_path):
        runtime_env = parse_env_dump(read_text(runtime_env_path))

    runtime_meta_path = os.path.join(case_dir, "runtime_meta.txt")
    if os.path.exists(runtime_meta_path):
        runtime_meta = parse_env_dump(read_text(runtime_meta_path))

    job_view = merged_job_view(scontrol_data, sacct_data, runtime_env, runtime_meta)

    result_data = {
        "case_id": case_data["id"],
        "case_path": case_path,
        "submit": submit_result,
        "job_id": job_id,
        "stdout": job_stdout,
        "stderr": job_stderr,
        "env": runtime_env,
        "job": job_view,
        "scontrol": scontrol_data,
        "sacct": sacct_data,
        "runtime_meta": runtime_meta,
        "poll_error": poll_error,
    }
    failures = evaluate_case(case_data, result_data)
    assertion_data = {"passed": not failures, "failures": failures}
    write_json(os.path.join(case_dir, "job.json"), result_data)
    write_json(os.path.join(case_dir, "assertions.json"), assertion_data)
    write_text(os.path.join(case_dir, "stdout.txt"), job_stdout)
    write_text(os.path.join(case_dir, "stderr.txt"), job_stderr)

    return build_case_summary(case_data, result_data, case_dir, failures)


def run_suite(case_paths, run_id, timeout_seconds, poll_interval):
    if not case_paths:
        raise SystemExit("no cases matched")

    run_root = os.path.join("artifacts", "runs")
    ensure_dir(run_root)
    run_dir = os.path.join(run_root, "{0}-{1}".format(utc_timestamp(), run_id))
    ensure_dir(run_dir)

    summary = {
        "run_id": os.path.basename(run_dir),
        "started_at": utc_timestamp(),
        "cases": [],
    }

    for index, case_path in enumerate(case_paths, start=1):
        case_data = load_case(case_path)
        case_dir = os.path.join(run_dir, case_data["id"])
        wrapper_path = os.path.join(case_dir, "runtime_wrapper.sh")
        submit_argv, _, _ = prepare_submit_command(case_data, case_dir, wrapper_path)
        if index == 1:
            print_run_header(summary["run_id"], len(case_paths), run_dir)
        print_case_start(case_data, submit_argv, index, len(case_paths))
        result = run_case(case_path, run_dir, timeout_seconds, poll_interval)
        print_case_result(result)
        summary["cases"].append(result)

    passed = len([case for case in summary["cases"] if case["passed"]])
    summary["passed"] = passed
    summary["failed"] = len(summary["cases"]) - passed
    summary["completed_at"] = utc_timestamp()
    write_json(os.path.join(run_dir, "summary.json"), summary)
    return run_dir, summary


def report_run(run_path):
    summary = load_run_summary(run_path)
    print_summary(summary)


def load_run_summary(run_path):
    summary_path = os.path.join(run_path, "summary.json")
    if not os.path.exists(summary_path):
        raise SystemExit("summary.json not found under {0}".format(run_path))
    with open(summary_path, "r") as fh:
        return json.load(fh)


def resolve_run_path(run_arg):
    if os.path.isdir(run_arg):
        return run_arg
    return os.path.join("artifacts", "runs", run_arg)


def load_run_details(run_path):
    summary = load_run_summary(run_path)
    detailed_cases = []
    for case_summary in summary.get("cases", []):
        case_dir = os.path.join(run_path, case_summary["id"])
        detail = {"summary": case_summary}
        for name in ["case", "submit", "job", "assertions"]:
            path = os.path.join(case_dir, name + ".json")
            if os.path.exists(path):
                with open(path, "r") as fh:
                    detail[name] = json.load(fh)
            else:
                detail[name] = {}
        for name in ["stdout", "stderr"]:
            path = os.path.join(case_dir, name + ".txt")
            detail[name] = read_text(path) if os.path.exists(path) else ""
        detailed_cases.append(detail)
    return summary, detailed_cases


def format_mapping_lines(mapping, keys=None):
    lines = []
    used_keys = keys or sorted(mapping.keys())
    for key in used_keys:
        if key not in mapping:
            continue
        value = mapping.get(key)
        if value in (None, "", {}):
            continue
        lines.append("- `{0}`: `{1}`".format(key, value))
    return lines


def trim_block(text, limit):
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... (truncated)"


def render_markdown_report(run_path, summary, detailed_cases):
    lines = []
    lines.append("# rsc_verify Run Report")
    lines.append("")
    lines.append("- run_id: `{0}`".format(summary["run_id"]))
    lines.append("- artifacts: `{0}`".format(run_path))
    lines.append("- started_at: `{0}`".format(summary.get("started_at", "")))
    lines.append("- completed_at: `{0}`".format(summary.get("completed_at", "")))
    lines.append("- passed: `{0}`".format(summary.get("passed", 0)))
    lines.append("- failed: `{0}`".format(summary.get("failed", 0)))
    lines.append("")
    lines.append("## Case Results")
    lines.append("")

    for detail in detailed_cases:
        case_summary = detail["summary"]
        case_data = detail.get("case", {})
        job_normalized = detail.get("job", {}).get("job", {}).get("normalized", {})
        status = "PASS" if case_summary.get("passed") else "FAIL"
        lines.append("### {0} {1}".format(status, case_summary["id"]))
        lines.append("")
        lines.append(case_summary.get("description", ""))
        lines.append("")
        lines.append("- submit_mode: `{0}`".format(case_summary.get("submit_mode", "")))
        lines.append("- submit_command: `{0}`".format(case_summary.get("submit_command", "")))
        if case_summary.get("job_id"):
            lines.append("- job_id: `{0}`".format(case_summary["job_id"]))
        if case_summary.get("job_state"):
            lines.append("- job_state: `{0}`".format(case_summary["job_state"]))
        if case_summary.get("notes_ref"):
            lines.append("- notes_ref: {0}".format(", ".join("`{0}`".format(item) for item in case_summary["notes_ref"])))
        lines.append("- artifacts: `{0}`".format(case_summary.get("artifacts_dir", "")))
        lines.append("")

        if job_normalized:
            lines.append("Observed job shape:")
            lines.extend(format_mapping_lines(
                job_normalized,
                ["state", "nodes", "ntasks", "cpus_per_task", "num_cpus", "tasks_per_node", "req_tres", "alloc_tres", "tres_per_task", "req_mem"],
            ))
            lines.append("")

        runtime_env = detail.get("job", {}).get("env", {})
        if runtime_env:
            lines.append("Observed env:")
            lines.extend(format_mapping_lines(runtime_env))
            lines.append("")

        if not case_summary.get("passed"):
            lines.append("Failures:")
            for failure in case_summary.get("failures", []):
                lines.append("- {0}".format(failure))
            lines.append("")

        submit_stderr = trim_block(detail.get("submit", {}).get("stderr", ""), 1200)
        if submit_stderr:
            lines.append("submit stderr:")
            lines.append("```text")
            lines.append(submit_stderr)
            lines.append("```")
            lines.append("")

        stdout_text = trim_block(detail.get("stdout", ""), 800)
        if stdout_text:
            lines.append("job stdout:")
            lines.append("```text")
            lines.append(stdout_text)
            lines.append("```")
            lines.append("")

        if case_data.get("expect"):
            lines.append("Expected checks:")
            lines.append("```json")
            lines.append(json.dumps(case_data["expect"], indent=2, sort_keys=True))
            lines.append("```")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_markdown_issues(run_path, summary, detailed_cases, title):
    failed = [detail for detail in detailed_cases if not detail["summary"].get("passed")]

    lines = []
    lines.append("# {0}".format(title))
    lines.append("")
    lines.append("- target_note: `notes/slurm-rsc-option-impl-v2.md`")
    lines.append("- run_id: `{0}`".format(summary["run_id"]))
    lines.append("- artifacts: `{0}`".format(run_path))
    lines.append("- failed_cases: `{0}`".format(len(failed)))
    lines.append("")

    if not failed:
        lines.append("失敗ケースはありません。")
        lines.append("")
        return "\n".join(lines)

    lines.append("## 失敗ケース")
    lines.append("")

    for detail in failed:
        case_summary = detail["summary"]
        case_data = detail.get("case", {})
        job_data = detail.get("job", {})
        normalized = job_data.get("job", {}).get("normalized", {})
        lines.append("### {0}".format(case_summary["id"]))
        lines.append("")
        lines.append("- description: {0}".format(case_summary.get("description", "")))
        lines.append("- submit_command: `{0}`".format(case_summary.get("submit_command", "")))
        lines.append("- job_id: `{0}`".format(case_summary.get("job_id") or ""))
        lines.append("- job_state: `{0}`".format(case_summary.get("job_state") or ""))
        if case_summary.get("notes_ref"):
            lines.append("- notes_ref: {0}".format(", ".join("`{0}`".format(item) for item in case_summary["notes_ref"])))
        lines.append("- artifacts: `{0}`".format(case_summary.get("artifacts_dir", "")))
        lines.append("")

        lines.append("期待:")
        lines.append("```json")
        lines.append(json.dumps(case_data.get("expect", {}), indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")

        lines.append("実測:")
        observed = {
            "submit_returncode": detail.get("submit", {}).get("returncode"),
            "submit_stdout": trim_block(detail.get("submit", {}).get("stdout", ""), 600),
            "submit_stderr": trim_block(detail.get("submit", {}).get("stderr", ""), 600),
            "env": job_data.get("env", {}),
            "job_normalized": normalized,
            "job_raw": {
                "JobState": job_data.get("job", {}).get("JobState"),
                "NumCPUs": job_data.get("job", {}).get("NumCPUs"),
                "NumTasks": job_data.get("job", {}).get("NumTasks"),
                "CPUs/Task": job_data.get("job", {}).get("CPUs/Task"),
                "ReqTRES": job_data.get("job", {}).get("sacct.ReqTRES"),
                "AllocTRES": job_data.get("job", {}).get("sacct.AllocTRES"),
                "TresPerTask": job_data.get("job", {}).get("sacct.TresPerTask"),
            },
        }
        lines.append("```json")
        lines.append(json.dumps(observed, indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")

        lines.append("差分:")
        for failure in case_summary.get("failures", []):
            lines.append("- {0}".format(failure))
        lines.append("")

        lines.append("Codex への依頼ポイント:")
        lines.append("- `notes/slurm-rsc-option-impl-v2.md` の該当節と照合し、期待との差分原因を特定する。")
        lines.append("- submit command で再現し、環境変数・job shape・reject/warn 挙動を修正する。")
        lines.append("- 修正後は同じケースを再実行し、artifacts を更新して差分が消えることを確認する。")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def emit_markdown(content, output_path):
    if output_path:
        write_text(output_path, content)
        print(output_path)
        return
    sys.stdout.write(content)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Run repeatable Slurm --rsc validation cases."
    )
    sub = parser.add_subparsers(dest="subcommand")
    sub.required = True

    run_parser = sub.add_parser("run", help="run one or more validation cases")
    run_parser.add_argument("--case", help="case id under cases/rsc/")
    run_parser.add_argument("--glob", dest="path_glob", help="glob for case JSON files")
    run_parser.add_argument(
        "--run-id",
        default=uuid.uuid4().hex[:8],
        help="suffix for artifacts/runs/<timestamp>-<run-id>",
    )
    run_parser.add_argument("--timeout", type=int, default=600, help="job wait timeout in seconds")
    run_parser.add_argument(
        "--poll-interval", type=int, default=5, help="poll interval in seconds"
    )

    report_parser = sub.add_parser("report", help="show a saved run summary")
    report_parser.add_argument("--run", required=True, help="run directory name or path")

    md_report_parser = sub.add_parser("markdown-report", help="render a saved run as Markdown")
    md_report_parser.add_argument("--run", required=True, help="run directory name or path")
    md_report_parser.add_argument("--output", help="write Markdown to the specified path")

    md_issues_parser = sub.add_parser(
        "markdown-issues", help="render failed cases as a Codex handoff Markdown"
    )
    md_issues_parser.add_argument("--run", required=True, help="run directory name or path")
    md_issues_parser.add_argument("--output", help="write Markdown to the specified path")
    md_issues_parser.add_argument(
        "--title",
        default=DEFAULT_ISSUES_TITLE,
        help="document title",
    )

    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.subcommand == "run":
        case_paths = discover_case_paths(args.case, args.path_glob)
        run_dir, summary = run_suite(
            case_paths,
            run_id=args.run_id,
            timeout_seconds=args.timeout,
            poll_interval=args.poll_interval,
        )
        print_summary(summary)
        print("artifacts:", run_dir)
        return 0 if summary["failed"] == 0 else 1

    run_path = resolve_run_path(args.run)

    if args.subcommand == "report":
        report_run(run_path)
        return 0

    summary, detailed_cases = load_run_details(run_path)
    if args.subcommand == "markdown-report":
        content = render_markdown_report(run_path, summary, detailed_cases)
        emit_markdown(content, args.output)
        return 0

    content = render_markdown_issues(run_path, summary, detailed_cases, args.title)
    emit_markdown(content, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
