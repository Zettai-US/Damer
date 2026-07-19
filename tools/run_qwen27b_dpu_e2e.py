#!/usr/bin/env python3
"""Run Qwen 27B end-to-end Damer/bpftime DPU policy tests.

This is a data-movement policy E2E test. It does not require Qwen model weights:
the workload models the communication phases a Qwen 27B serving stack would
expose to Damer and verifies that each phase compiles into bpftime-facing eBPF
artifacts suitable for DPU deployment.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WORKLOAD = os.path.join(ROOT, "examples", "qwen27b", "qwen27b_workload.json")
DEFAULT_OUT = os.path.join(ROOT, "out", "qwen27b-dpu-e2e")


def run_command(
    argv: Sequence[str],
    cwd: str = ROOT,
    check: bool = False,
    capture: bool = True,
) -> Dict[str, object]:
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    result = {
        "cmd": list(argv),
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(argv)}\n{result['stdout']}{result['stderr']}"
        )
    return result


def load_workload(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as f:
        workload = json.load(f)
    if "events" not in workload or not isinstance(workload["events"], list):
        raise ValueError("workload JSON must contain an events list")
    return workload


def write_json(path: str, data: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def read_json(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def detect_environment() -> Dict[str, object]:
    bpftime_paths = []
    for candidate in [
        "/root/bpftime-dpu",
        os.path.expanduser("~/.bpftime"),
        "/usr/local/bin/bpftime",
    ]:
        if os.path.exists(candidate):
            bpftime_paths.append(candidate)

    lspci = run_command(["lspci"], capture=True)
    nvidia_smi = run_command(
        ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
        capture=True,
    )
    rshim = run_command(["bash", "-lc", "ls -1 /dev/rshim* 2>/dev/null || true"])
    ip_addr = run_command(["bash", "-lc", "ip -br addr 2>/dev/null || true"])
    clang = run_command(["bash", "-lc", "command -v clang || true"])

    lspci_text = lspci["stdout"]
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uname": run_command(["uname", "-a"])["stdout"].strip(),
        "bluefield_pcie": "BlueField" in lspci_text or "Mellanox" in lspci_text,
        "bluefield_lspci": [
            line
            for line in lspci_text.splitlines()
            if any(token in line.lower() for token in ("bluefield", "mellanox", "connectx"))
        ],
        "gpu_driver_ready": nvidia_smi["returncode"] == 0,
        "nvidia_smi": (nvidia_smi["stdout"] or nvidia_smi["stderr"]).strip(),
        "rshim_devices": [line for line in rshim["stdout"].splitlines() if line],
        "network_interfaces": ip_addr["stdout"].splitlines(),
        "bpftime_paths": bpftime_paths,
        "clang": clang["stdout"].strip(),
    }


def compile_event(
    event: Dict[str, object],
    out_dir: str,
    clang: str,
    compile_bpf: bool,
) -> Dict[str, object]:
    name = str(event["switchlet"])
    event_dir = os.path.join(out_dir, "events")
    artifact_dir = os.path.join(out_dir, "artifacts", name)
    event_path = os.path.join(event_dir, f"{name}.event.json")
    write_json(event_path, event)

    result = {
        "switchlet": name,
        "event": event_path,
        "artifact_dir": artifact_dir,
        "compile": None,
        "plan_ok": False,
        "bpf_object_ok": False,
        "bpf_object": None,
        "errors": [],
    }

    compile_cmd = [
        os.path.join(ROOT, "tools", "damer_compile.py"),
        event_path,
        "--emit",
        "all",
        "--output-dir",
        artifact_dir,
        "--max-rdma-ops",
        "16",
    ]
    compile_result = run_command(compile_cmd)
    result["compile"] = compile_result
    if compile_result["returncode"] != 0:
        result["errors"].append("damer_compile failed")
        return result

    plan_path = os.path.join(artifact_dir, f"{name}.plan.json")
    manifest_path = os.path.join(artifact_dir, f"{name}.bpftime.json")
    bpf_c_path = os.path.join(artifact_dir, f"{name}.bpf.c")
    result.update({"plan": plan_path, "manifest": manifest_path, "bpf_c": bpf_c_path})

    plan = read_json(plan_path)
    result["kind"] = plan["edges"][0]["kind"] if plan.get("edges") else "unknown"
    result["placement"] = plan["edges"][0]["placement"] if plan.get("edges") else "unknown"
    result["verifier"] = plan.get("verifier", {})
    result["plan_ok"] = bool(plan.get("verifier", {}).get("ok"))
    if not result["plan_ok"]:
        result["errors"].append("verifier rejected plan")

    manifest = read_json(manifest_path)
    result["bpftime_section"] = manifest.get("section")

    if compile_bpf:
        bpf_o_path = os.path.join(artifact_dir, f"{name}.bpf.o")
        clang_cmd = [
            clang,
            "-target",
            "bpf",
            "-O2",
            "-g",
            "-I",
            os.path.join(ROOT, "include"),
            "-c",
            bpf_c_path,
            "-o",
            bpf_o_path,
        ]
        clang_result = run_command(clang_cmd)
        result["clang"] = clang_result
        result["bpf_object_ok"] = clang_result["returncode"] == 0 and os.path.exists(bpf_o_path)
        result["bpf_object"] = bpf_o_path if result["bpf_object_ok"] else None
        if not result["bpf_object_ok"]:
            result["errors"].append("clang -target bpf failed")

    return result


def copy_and_run_remote(out_dir: str, dpu_host: str, remote_dir: str, clang: str) -> Dict[str, object]:
    package = os.path.join(out_dir, "qwen27b-dpu-e2e.tar.gz")
    if os.path.exists(package):
        os.remove(package)

    run_command(
        ["tar", "-C", ROOT, "-czf", package, os.path.relpath(out_dir, ROOT), "include/damer"],
        check=True,
    )

    remote_setup = run_command(["ssh", dpu_host, f"mkdir -p {remote_dir}"])
    if remote_setup["returncode"] != 0:
        return {"ok": False, "stage": "mkdir", "result": remote_setup}

    copy = run_command(["scp", package, f"{dpu_host}:{remote_dir}/qwen27b-dpu-e2e.tar.gz"])
    if copy["returncode"] != 0:
        return {"ok": False, "stage": "scp", "result": copy}

    remote_script = f"""
set -eu
cd {remote_dir}
tar -xzf qwen27b-dpu-e2e.tar.gz
OUT=$(find . -type d -path '*/qwen27b-dpu-e2e' | head -1)
test -n "$OUT"
for c in $(find "$OUT/artifacts" -name '*.bpf.c' | sort); do
  o="${{c%.c}}.o"
  {clang} -target bpf -O2 -g -I ./include -c "$c" -o "$o"
done
find "$OUT/artifacts" -name '*.bpf.o' | sort
"""
    remote = run_command(["ssh", dpu_host, remote_script])
    return {
        "ok": remote["returncode"] == 0,
        "stage": "remote-compile",
        "result": remote,
    }


def run_e2e(args: argparse.Namespace) -> Dict[str, object]:
    workload = load_workload(args.workload)
    out_dir = os.path.abspath(args.out_dir)
    if args.clean and os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    env = detect_environment()
    phases: List[Dict[str, object]] = []
    for event in workload["events"]:
        phases.append(
            compile_event(
                event,
                out_dir,
                clang=args.clang,
                compile_bpf=not args.no_bpf_object,
            )
        )

    remote = None
    if args.dpu_host:
        remote = copy_and_run_remote(out_dir, args.dpu_host, args.remote_dir, args.remote_clang)

    ok = all(phase["plan_ok"] for phase in phases)
    if not args.no_bpf_object:
        ok = ok and all(phase["bpf_object_ok"] for phase in phases)
    if remote is not None:
        ok = ok and bool(remote.get("ok"))

    report = {
        "ok": ok,
        "workload": {
            "model": workload.get("model", "qwen27b"),
            "description": workload.get("description", ""),
            "events": len(workload["events"]),
        },
        "out_dir": out_dir,
        "environment": env,
        "phases": phases,
        "remote": remote,
    }
    write_json(os.path.join(out_dir, "qwen27b_e2e_report.json"), report)
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Qwen27B Damer/bpftime DPU E2E")
    parser.add_argument("--workload", default=DEFAULT_WORKLOAD)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--clean", action="store_true", help="remove output dir before running")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--no-bpf-object", action="store_true", help="skip clang -target bpf")
    parser.add_argument("--dpu-host", help="optional ssh target for remote DPU validation")
    parser.add_argument("--remote-dir", default="/tmp/damer-qwen27b-e2e")
    parser.add_argument("--remote-clang", default="clang")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report = run_e2e(args)
    except Exception as exc:
        sys.stderr.write(f"qwen27b-dpu-e2e: error: {exc}\n")
        return 1

    print(json.dumps(
        {
            "ok": report["ok"],
            "out_dir": report["out_dir"],
            "events": report["workload"]["events"],
            "failed": [phase["switchlet"] for phase in report["phases"] if phase["errors"]],
            "bluefield_pcie": report["environment"]["bluefield_pcie"],
            "gpu_driver_ready": report["environment"]["gpu_driver_ready"],
            "remote_ok": None if report["remote"] is None else report["remote"].get("ok"),
        },
        indent=2,
        sort_keys=True,
    ))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
