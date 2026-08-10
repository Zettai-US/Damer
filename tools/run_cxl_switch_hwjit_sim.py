#!/usr/bin/env python3
"""Generate and Verilator-simulate a Damer CXL switch Hardware-JIT artifact."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Dict, Optional, Sequence

import damer_compile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "out", "cxl-switch-hwjit-sim")


def run_command(argv: Sequence[str], cwd: str = ROOT) -> Dict[str, object]:
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "cmd": list(argv),
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


def write_json(path: str, data: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def load_switchlets(path: str):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".json") or text.lstrip().startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return [damer_compile.parse_json_switchlet(event) for event in data["events"]]
    return [damer_compile.load_switchlet(path)]


def build_and_run_verilator(
    module_name: str,
    sv_path: str,
    harness_path: str,
    obj_dir: str,
    verilator: str,
    extra_args: Sequence[str],
) -> Dict[str, object]:
    os.makedirs(obj_dir, exist_ok=True)
    verilate_cmd = [
        verilator,
        "--cc",
        sv_path,
        "--exe",
        harness_path,
        "--build",
        "-Mdir",
        obj_dir,
    ] + list(extra_args)
    verilate = run_command(verilate_cmd)
    exe_path = os.path.join(obj_dir, f"V{module_name}")
    sim = None
    if verilate["returncode"] == 0 and os.path.exists(exe_path):
        sim = run_command([exe_path])
    return {
        "ok": verilate["returncode"] == 0 and sim is not None and sim["returncode"] == 0,
        "verilate": verilate,
        "sim": sim,
        "executable": exe_path if os.path.exists(exe_path) else None,
    }


def run_hwjit_sim(args: argparse.Namespace) -> Dict[str, object]:
    out_dir = os.path.abspath(args.out_dir)
    if args.clean and os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    options = damer_compile.CompileOptions(
        max_transforms=args.max_transforms,
        max_rdma_ops=args.max_rdma_ops,
        default_ttl=args.default_ttl,
        strict=args.strict,
    )
    verilator_path = shutil.which(args.verilator) if not os.path.isabs(args.verilator) else args.verilator

    cases = []
    for switchlet in load_switchlets(args.input):
        plan = damer_compile.compile_plan(switchlet, options)
        name = damer_compile.sanitize_c_identifier(str(plan["switchlet"]))
        artifact_dir = os.path.join(out_dir, "artifacts", name)
        os.makedirs(artifact_dir, exist_ok=True)

        damer_compile.emit_all(plan, artifact_dir)
        module_name = damer_compile.hw_jit_module_name(plan)
        hw_paths = {
            "sv": os.path.join(artifact_dir, f"{module_name}.sv"),
            "harness": os.path.join(artifact_dir, f"{module_name}_tb.cpp"),
            "manifest": os.path.join(artifact_dir, f"{module_name}.hwjit.json"),
            "plan": os.path.join(artifact_dir, f"{name}.plan.json"),
        }

        if not verilator_path or not os.path.exists(verilator_path):
            sim = {
                "ok": False,
                "skipped": True,
                "reason": "verilator not found",
                "verilator": args.verilator,
            }
        elif args.no_build:
            sim = {
                "ok": True,
                "skipped": True,
                "reason": "build disabled",
                "verilator": verilator_path,
            }
        else:
            sim = build_and_run_verilator(
                module_name,
                hw_paths["sv"],
                hw_paths["harness"],
                os.path.join(out_dir, "verilator", module_name),
                verilator_path,
                args.verilator_arg or [],
            )

        cases.append(
            {
                "ok": bool(plan["verifier"]["ok"]) and bool(sim.get("ok")),
                "switchlet": plan["switchlet"],
                "module": module_name,
                "edge_count": len(plan["edges"]),
                "artifacts": hw_paths,
                "verifier": plan["verifier"],
                "verilator": sim,
            }
        )

    report = {
        "ok": all(case["ok"] for case in cases),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input": os.path.abspath(args.input),
        "case_count": len(cases),
        "edge_count": sum(int(case["edge_count"]) for case in cases),
        "out_dir": out_dir,
        "cases": cases,
    }
    if len(cases) == 1:
        report.update(
            {
                "switchlet": cases[0]["switchlet"],
                "module": cases[0]["module"],
                "artifacts": cases[0]["artifacts"],
                "verifier": cases[0]["verifier"],
                "verilator": cases[0]["verilator"],
            }
        )
    write_json(os.path.join(out_dir, "cxl_switch_hwjit_sim_report.json"), report)
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run CXL switch Hardware-JIT Verilator simulation")
    parser.add_argument("input", help=".damer DSL or JSON switchlet/event file")
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--max-transforms", type=int, default=8)
    parser.add_argument("--max-rdma-ops", type=int, default=16)
    parser.add_argument("--default-ttl", type=int, default=4)
    parser.add_argument("--verilator", default="verilator")
    parser.add_argument("--verilator-arg", action="append", default=[])
    parser.add_argument("--no-build", action="store_true", help="emit artifacts without running Verilator")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report = run_hwjit_sim(args)
    except (damer_compile.CompileError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"cxl-switch-hwjit-sim: error: {exc}\n")
        return 1

    if report["case_count"] == 1:
        print(
            "HW_JIT_SIM ok={ok} switchlet={switchlet} module={module} commands={commands}".format(
                ok=str(report["ok"]).lower(),
                switchlet=report["switchlet"],
                module=report["module"],
                commands=report["edge_count"],
            )
        )
        print("HW_JIT_ARTIFACT sv={sv}".format(sv=report["artifacts"]["sv"]))
    else:
        print(
            "HW_JIT_SIM ok={ok} cases={cases} commands={commands}".format(
                ok=str(report["ok"]).lower(),
                cases=report["case_count"],
                commands=report["edge_count"],
            )
        )
        for case in report["cases"]:
            print(
                "HW_JIT_CASE switchlet={switchlet} ok={ok} module={module} commands={commands}".format(
                    switchlet=case["switchlet"],
                    ok=str(case["ok"]).lower(),
                    module=case["module"],
                    commands=case["edge_count"],
                )
            )
    print("HW_JIT_REPORT path={path}".format(path=os.path.join(report["out_dir"], "cxl_switch_hwjit_sim_report.json")))
    for case in report["cases"]:
        sim = case.get("verilator", {}).get("sim") if isinstance(case.get("verilator"), dict) else None
        if sim and sim.get("stdout"):
            sys.stdout.write(str(sim["stdout"]))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
