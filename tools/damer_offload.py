#!/usr/bin/env python3
"""Build a full Damer data-movement + eBPF offload bundle."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

import damer_compile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DPUTIME_ROOT = os.path.join(ROOT, "third_party", "dputime")
DEFAULT_DPUTIME_BUILD_DIR = os.path.join(ROOT, "out", "dputime-build")
DEFAULT_OBSERVERS_REPORT = os.path.join(ROOT, "out", "dputime-observers", "observers.json")
DEFAULT_OUT = os.path.join(ROOT, "out", "damer-offload")


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


def read_input_cases(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    if path.endswith(".json") or text.lstrip().startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return {
                "name": str(data.get("model") or "workload"),
                "description": str(data.get("description") or ""),
                "cases": [
                    {
                        "kind": "json",
                        "name": str(event.get("switchlet") or f"event_{index}"),
                        "payload": event,
                    }
                    for index, event in enumerate(data["events"])
                ],
            }
        return {
            "name": os.path.splitext(os.path.basename(path))[0],
            "description": "",
            "cases": [
                {
                    "kind": "json",
                    "name": str(data.get("switchlet") or data.get("program") or "event"),
                    "payload": data,
                }
            ],
        }

    return {
        "name": os.path.splitext(os.path.basename(path))[0],
        "description": "",
        "cases": [
            {
                "kind": "dsl",
                "name": os.path.splitext(os.path.basename(path))[0],
                "payload": text,
            }
        ],
    }


def switchlet_from_case(case: Dict[str, object]) -> damer_compile.Switchlet:
    if case["kind"] == "dsl":
        return damer_compile.parse_damer_dsl(str(case["payload"]))
    return damer_compile.parse_json_switchlet(case["payload"])


def dputime_info(path: str, build_dir: str) -> Dict[str, object]:
    exists = os.path.isdir(path)
    bpftime_cli = os.path.join(build_dir, "tools", "cli", "bpftime")
    info: Dict[str, object] = {
        "path": path,
        "available": exists,
        "name": "dputime",
        "upstream": "https://github.com/vickiegpt/dputime",
        "build_dir": build_dir,
        "built": os.path.exists(bpftime_cli),
        "bpftime_cli": bpftime_cli if os.path.exists(bpftime_cli) else None,
    }
    if not exists:
        return info

    head = run_command(["git", "-C", path, "rev-parse", "HEAD"])
    branch = run_command(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"])
    status = run_command(["git", "-C", path, "submodule", "status", "--recursive"])
    info.update(
        {
            "commit": head["stdout"].strip() if head["returncode"] == 0 else None,
            "branch": branch["stdout"].strip() if branch["returncode"] == 0 else None,
            "submodules_initialized": status["returncode"] == 0
            and not any(line.startswith("-") for line in status["stdout"].splitlines()),
        }
    )
    return info


def load_observers_report(path: str) -> Dict[str, object]:
    if not path or not os.path.exists(path):
        return {
            "available": False,
            "path": path,
            "observers": {},
        }
    with open(path, "r", encoding="utf-8") as f:
        report = json.load(f)
    report["available"] = True
    report["path"] = path
    return report


def compile_object(
    bpf_c_path: str,
    bpf_o_path: str,
    clang: str,
    include_dir: str,
) -> Dict[str, object]:
    result = run_command(
        [
            clang,
            "-target",
            "bpf",
            "-O2",
            "-g",
            "-I",
            include_dir,
            "-c",
            bpf_c_path,
            "-o",
            bpf_o_path,
        ]
    )
    result["ok"] = result["returncode"] == 0 and os.path.exists(bpf_o_path)
    return result


def inspect_object(path: str, llvm_objdump: Optional[str]) -> Dict[str, object]:
    if not llvm_objdump or not os.path.exists(path):
        return {"available": False}

    sections = run_command([llvm_objdump, "-h", path])
    disassembly = run_command([llvm_objdump, "-d", path])
    instruction_count = 0
    for line in disassembly["stdout"].splitlines():
        stripped = line.strip()
        if stripped and stripped[0].isdigit() and ":" in stripped:
            instruction_count += 1

    return {
        "available": sections["returncode"] == 0,
        "sections": sections["stdout"],
        "instruction_count": instruction_count,
        "elf64_bpf": "file format elf64-bpf" in sections["stdout"],
    }


def compile_case(
    case: Dict[str, object],
    output_dir: str,
    options: damer_compile.CompileOptions,
    clang: str,
    llvm_objdump: Optional[str],
    compile_bpf: bool,
) -> Dict[str, object]:
    switchlet = switchlet_from_case(case)
    plan = damer_compile.compile_plan(switchlet, options)
    name = damer_compile.sanitize_c_identifier(str(plan["switchlet"]))
    artifact_dir = os.path.join(output_dir, "artifacts", name)
    os.makedirs(artifact_dir, exist_ok=True)
    damer_compile.emit_all(plan, artifact_dir)

    source_path = os.path.join(artifact_dir, f"{name}.input.json")
    if case["kind"] == "json":
        write_json(source_path, case["payload"])
    else:
        source_path = os.path.join(artifact_dir, f"{name}.input.damer")
        with open(source_path, "w", encoding="utf-8") as f:
            f.write(str(case["payload"]))

    bpf_c_path = os.path.join(artifact_dir, f"{name}.bpf.c")
    bpf_o_path = os.path.join(artifact_dir, f"{name}.bpf.o")
    object_result = None
    object_info = {"available": False}
    if compile_bpf:
        object_result = compile_object(bpf_c_path, bpf_o_path, clang, os.path.join(ROOT, "include"))
        if object_result["ok"]:
            object_info = inspect_object(bpf_o_path, llvm_objdump)

    return {
        "name": str(plan["switchlet"]),
        "ok": bool(plan["verifier"]["ok"])
        and (not compile_bpf or bool(object_result and object_result["ok"])),
        "artifact_dir": artifact_dir,
        "input": source_path,
        "plan": os.path.join(artifact_dir, f"{name}.plan.json"),
        "bpf_c": bpf_c_path,
        "bpftime_manifest": os.path.join(artifact_dir, f"{name}.bpftime.json"),
        "bpf_object": bpf_o_path if object_result and object_result["ok"] else None,
        "verifier": plan["verifier"],
        "edges": plan["edges"],
        "program_section": plan["bpftime"]["program_section"],
        "helpers": plan["bpftime"]["helpers"],
        "object_compile": object_result,
        "object_info": object_info,
    }


def make_package(output_dir: str, bundle_path: str) -> str:
    package_path = os.path.join(output_dir, "damer-offload-bundle.tar.gz")
    if os.path.exists(package_path):
        os.remove(package_path)

    with tarfile.open(package_path, "w:gz") as tar:
        tar.add(bundle_path, arcname="offload.bundle.json")
        tar.add(os.path.join(ROOT, "include", "damer"), arcname="include/damer")
        artifacts = os.path.join(output_dir, "artifacts")
        if os.path.isdir(artifacts):
            tar.add(artifacts, arcname="artifacts")
    return package_path


def deploy_remote(
    package_path: str,
    remote: str,
    remote_dir: str,
    ssh_options: Sequence[str],
) -> Dict[str, object]:
    ssh_base = ["ssh"] + list(ssh_options) + [remote]
    scp_base = ["scp"] + list(ssh_options)

    mkdir = run_command(ssh_base + [f"mkdir -p {remote_dir}"])
    if mkdir["returncode"] != 0:
        return {"ok": False, "stage": "mkdir", "result": mkdir}

    copy = run_command(scp_base + [package_path, f"{remote}:{remote_dir}/damer-offload-bundle.tar.gz"])
    if copy["returncode"] != 0:
        return {"ok": False, "stage": "scp", "result": copy}

    remote_script = f"""
set -eu
cd {remote_dir}
rm -rf damer-offload-bundle
mkdir -p damer-offload-bundle
tar -xzf damer-offload-bundle.tar.gz -C damer-offload-bundle
uname -a
command -v bpftime || true
command -v nvidia-smi || true
find damer-offload-bundle/artifacts -name '*.bpf.o' | sort
"""
    inspect = run_command(ssh_base + [remote_script])
    return {
        "ok": inspect["returncode"] == 0,
        "stage": "inspect",
        "result": inspect,
    }


def build_offload(args: argparse.Namespace) -> Dict[str, object]:
    input_info = read_input_cases(args.input)
    output_dir = os.path.abspath(args.output_dir)
    if args.clean and os.path.isdir(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    options = damer_compile.CompileOptions(
        max_transforms=args.max_transforms,
        max_rdma_ops=args.max_rdma_ops,
        default_ttl=args.default_ttl,
        strict=args.strict,
    )
    llvm_objdump = shutil.which(args.llvm_objdump) if args.llvm_objdump else None
    cases = [
        compile_case(
            case,
            output_dir,
            options,
            args.clang,
            llvm_objdump,
            not args.no_bpf_object,
        )
        for case in input_info["cases"]
    ]

    dputime = dputime_info(
        os.path.abspath(args.dputime_root),
        os.path.abspath(args.dputime_build_dir),
    )
    observers = load_observers_report(os.path.abspath(args.observers_report))
    bpf_objects = [case["bpf_object"] for case in cases if case["bpf_object"]]
    bundle = {
        "version": 1,
        "kind": "damer.datamovement.ebpf.offload.bundle",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input": {
            "path": os.path.abspath(args.input),
            "name": input_info["name"],
            "description": input_info["description"],
            "cases": len(cases),
        },
        "pipeline": [
            "movement_intent",
            "damer_semantic_plan",
            "cross_device_effect_verifier",
            "bpftime_bpf_c",
            "clang_target_bpf_object",
            "dputime_runtime_bundle",
        ],
        "runtime": {
            "frontend": "bpftime",
            "offload_runtime": "dputime",
            "dputime": dputime,
            "observers": observers,
            "custom_helpers": {
                "damer_emit_edge": 9001,
                "damer_submit_decision": 9002,
            },
        },
        "offload_contract": {
            "datamovement": "Damer owns graph construction, placement, fusion, and verifier policy.",
            "ebpf": "bpftime/dputime loads eBPF objects and provides custom helper bindings.",
            "observation": "GPU host and DPU Arm dputime observers consume the same bundle metadata and BPF object set.",
            "targets": ["host_memory", "cxl_memory_device", "gpu_memory", "accelerator", "switch_compute_engine"],
        },
        "cases": cases,
    }
    bundle["ok"] = all(case["ok"] for case in cases)
    bundle_path = os.path.join(output_dir, "offload.bundle.json")
    write_json(bundle_path, bundle)

    package_path = None
    if not args.no_package or args.remote:
        package_path = make_package(output_dir, bundle_path)
        bundle["package"] = package_path
        write_json(bundle_path, bundle)

    remote = None
    if args.remote:
        remote = deploy_remote(package_path, args.remote, args.remote_dir, args.ssh_option or [])
        bundle["remote"] = remote
        bundle["ok"] = bool(bundle["ok"] and remote["ok"])
        write_json(bundle_path, bundle)

    return {
        "ok": bundle["ok"],
        "bundle": bundle_path,
        "package": package_path,
        "cases": len(cases),
        "bpf_objects": len(bpf_objects),
        "dputime_available": bool(dputime["available"]),
        "remote": remote,
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Damer data-movement + eBPF offload bundles")
    parser.add_argument("input", help=".damer, event JSON, or workload JSON")
    parser.add_argument("--output-dir", default=DEFAULT_OUT)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--dputime-root", default=DEFAULT_DPUTIME_ROOT)
    parser.add_argument("--dputime-build-dir", default=DEFAULT_DPUTIME_BUILD_DIR)
    parser.add_argument("--observers-report", default=DEFAULT_OBSERVERS_REPORT)
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--llvm-objdump", default="llvm-objdump")
    parser.add_argument("--no-bpf-object", action="store_true")
    parser.add_argument("--no-package", action="store_true")
    parser.add_argument("--max-transforms", type=int, default=8)
    parser.add_argument("--max-rdma-ops", type=int, default=16)
    parser.add_argument("--default-ttl", type=int, default=4)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--remote", help="optional ssh target for package deployment")
    parser.add_argument("--remote-dir", default="/tmp/damer-offload")
    parser.add_argument(
        "--ssh-option",
        action="append",
        default=[],
        help="extra option passed to ssh/scp, for example: -o StrictHostKeyChecking=accept-new",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = build_offload(args)
    except Exception as exc:
        sys.stderr.write(f"damer-offload: error: {exc}\n")
        return 1

    print(
        "OFFLOAD ok={ok} cases={cases} bpf_objects={bpf_objects} dputime={dputime} package={package}".format(
            ok=str(result["ok"]).lower(),
            cases=result["cases"],
            bpf_objects=result["bpf_objects"],
            dputime=str(result["dputime_available"]).lower(),
            package=str(bool(result["package"])).lower(),
        )
    )
    print(f"OFFLOAD_BUNDLE path={result['bundle']}")
    if result["remote"] is not None:
        print(f"OFFLOAD_REMOTE ok={str(result['remote']['ok']).lower()} stage={result['remote']['stage']}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
