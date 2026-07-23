#!/usr/bin/env python3
"""Build dputime observers for Damer host-GPU and DPU-Arm offload paths."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Sequence


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DPUTIME_ROOT = os.path.join(ROOT, "third_party", "dputime")
DEFAULT_HOST_BUILD = os.path.join(ROOT, "out", "dputime-build-cuda")
DEFAULT_DPU_ARM_BUILD = os.path.join(ROOT, "out", "dputime-build-aarch64")
DEFAULT_REPORT = os.path.join(ROOT, "out", "dputime-observers", "observers.json")
DEFAULT_CUDA_SMOKE_DIR = os.path.join(ROOT, "out", "dputime-observers", "live-cuda-smoke")
CUDA_HOST_TARGETS = [
    "bpftime",
    "bpftime-agent",
    "bpftime-syscall-server",
    "ptxpass_kprobe_entry",
    "ptxpass_kretprobe",
    "ptxpass_kprobe_memcapture",
    "nv_attach_impl_ptx_compiler",
]


def run_command(
    argv: Sequence[str],
    cwd: str = ROOT,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, object]:
    merged_env = os.environ.copy()
    merged_env["CCACHE_DISABLE"] = "1"
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        env=merged_env,
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


def git_commit(path: str) -> Optional[str]:
    result = run_command(["git", "-C", path, "rev-parse", "HEAD"])
    return result["stdout"].strip() if result["returncode"] == 0 else None


def detect_cuda_root(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    for candidate in ["/usr/local/cuda-13.1", "/usr/local/cuda-12.8", "/usr/local/cuda"]:
        if os.path.isdir(candidate):
            return candidate
    return None


def host_gpu_status(cuda_root: Optional[str]) -> Dict[str, object]:
    nvcc = shutil.which("nvcc")
    nvidia_smi = run_command(["nvidia-smi"]) if shutil.which("nvidia-smi") else None
    return {
        "cuda_root": cuda_root,
        "cuda_root_exists": bool(cuda_root and os.path.isdir(cuda_root)),
        "nvcc": nvcc,
        "nvidia_smi_ok": bool(nvidia_smi and nvidia_smi["returncode"] == 0),
        "nvidia_smi": None if nvidia_smi is None else (nvidia_smi["stdout"] + nvidia_smi["stderr"]).strip(),
    }


def configure_host_cuda(args: argparse.Namespace, cuda_root: str) -> Dict[str, object]:
    cmake_args = [
        "cmake",
        "-S",
        args.dputime_root,
        "-B",
        args.host_build_dir,
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBPFTIME_ENABLE_LTO=NO",
        "-DBPFTIME_ENABLE_UNIT_TESTING=OFF",
        "-DBPFTIME_BUILD_WITH_LIBBPF=ON",
        "-DBPFTIME_BUILD_KERNEL_BPF=ON",
        "-DBPFTIME_LLVM_JIT=YES",
        "-DENABLE_EBPF_VERIFIER=OFF",
        "-DBPFTIME_ENABLE_CUDA_ATTACH=1",
        f"-DBPFTIME_CUDA_ROOT={cuda_root}",
    ]
    if args.llvm_dir:
        cmake_args.append(f"-DLLVM_DIR={args.llvm_dir}")
    return run_command(cmake_args)


def configure_dpu_arm(args: argparse.Namespace) -> Dict[str, object]:
    toolchain = os.path.join(args.dputime_root, "cmake", "aarch64-toolchain.cmake")
    cmake_args = [
        "cmake",
        "-S",
        args.dputime_root,
        "-B",
        args.dpu_arm_build_dir,
        "-G",
        "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
        "-DCMAKE_SYSTEM_PROCESSOR=aarch64",
        "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=BOTH",
        "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=BOTH",
        "-DBOOST_ROOT=/usr",
        "-DBoost_INCLUDE_DIR=/usr/include",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBPFTIME_ENABLE_LTO=NO",
        "-DBPFTIME_ENABLE_UNIT_TESTING=OFF",
        "-DBPFTIME_BUILD_WITH_LIBBPF=OFF",
        "-DBPFTIME_BUILD_KERNEL_BPF=OFF",
        "-DBPFTIME_LLVM_JIT=NO",
        "-DENABLE_EBPF_VERIFIER=OFF",
    ]
    if args.dpu_try_compile_static:
        cmake_args.append("-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY")
    return run_command(cmake_args)


def build_target(build_dir: str, target: str) -> Dict[str, object]:
    return run_command(["ninja", "-C", build_dir, target])


def build_targets(build_dir: str, targets: Sequence[str]) -> Dict[str, object]:
    result = run_command(["ninja", "-C", build_dir] + list(targets))
    result["targets"] = list(targets)
    return result


def required_host_cuda_paths(build_dir: str) -> Dict[str, str]:
    nv_attach = os.path.join(build_dir, "attach", "nv_attach_impl")
    return {
        "bpftime_cli": os.path.join(build_dir, "tools", "cli", "bpftime"),
        "agent_preload": os.path.join(build_dir, "runtime", "agent", "libbpftime-agent.so"),
        "syscall_server_preload": os.path.join(
            build_dir, "runtime", "syscall-server", "libbpftime-syscall-server.so"
        ),
        "ptxpass_kprobe_entry": os.path.join(
            nv_attach, "pass", "ptxpass_kprobe_entry", "libptxpass_kprobe_entry.so"
        ),
        "ptxpass_kretprobe": os.path.join(
            nv_attach, "pass", "ptxpass_kretprobe", "libptxpass_kretprobe.so"
        ),
        "ptxpass_kprobe_memcapture": os.path.join(
            nv_attach,
            "pass",
            "ptxpass_kprobe_memcapture",
            "libptxpass_kprobe_memcapture.so",
        ),
        "ptx_compiler": os.path.join(
            nv_attach, "ptx_compiler", "libnv_attach_impl_ptx_compiler.so"
        ),
    }


def observer_summary(
    name: str,
    role: str,
    arch: str,
    build_dir: str,
    configure: Optional[Dict[str, object]],
    build: Optional[Dict[str, object]],
    extra: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    bpftime_cli = os.path.join(build_dir, "tools", "cli", "bpftime")
    required_paths = {}
    if extra and isinstance(extra.get("required_paths"), dict):
        required_paths = extra["required_paths"]
    built = os.path.exists(bpftime_cli) and all(os.path.exists(path) for path in required_paths.values())
    summary = {
        "name": name,
        "role": role,
        "arch": arch,
        "build_dir": build_dir,
        "bpftime_cli": bpftime_cli if os.path.exists(bpftime_cli) else None,
        "built": built,
        "configure_ok": None if configure is None else configure["returncode"] == 0,
        "build_ok": None if build is None else build["returncode"] == 0,
        "configure": configure,
        "build": build,
    }
    if extra:
        summary.update(extra)
    return summary


def detect_sm_arch() -> Dict[str, object]:
    query = run_command(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"])
    if query["returncode"] != 0:
        return {"ok": False, "compute_cap": None, "sm_arch": "sm_120", "result": query}
    first = query["stdout"].strip().splitlines()[0].strip()
    digits = first.replace(".", "")
    return {"ok": True, "compute_cap": first, "sm_arch": f"sm_{digits}", "result": query}


def find_clang_cuda(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit if shutil.which(explicit) or os.path.exists(explicit) else None
    for candidate in ["clang++-21", "clang++", "clang++-20", "/usr/lib/llvm-20/bin/clang++"]:
        resolved = shutil.which(candidate) if not os.path.isabs(candidate) else candidate
        if resolved and os.path.exists(resolved):
            return candidate
    return None


def run_cuda_smoke(args: argparse.Namespace, cuda_root: str) -> Dict[str, object]:
    smoke_dir = os.path.abspath(args.cuda_smoke_dir)
    if os.path.isdir(smoke_dir):
        shutil.rmtree(smoke_dir)
    os.makedirs(smoke_dir, exist_ok=True)

    paths = required_host_cuda_paths(os.path.abspath(args.host_build_dir))
    missing_runtime = [name for name, path in paths.items() if not os.path.exists(path)]
    if missing_runtime:
        report = {
            "ok": False,
            "stage": "runtime-paths",
            "missing": missing_runtime,
            "required_paths": paths,
        }
        write_json(os.path.join(smoke_dir, "cuda_smoke.json"), report)
        return report

    clang_cuda = find_clang_cuda(args.cuda_smoke_clang)
    if not clang_cuda:
        report = {"ok": False, "stage": "clang-cuda", "error": "clang++ CUDA compiler not found"}
        write_json(os.path.join(smoke_dir, "cuda_smoke.json"), report)
        return report

    sm = detect_sm_arch()
    source = os.path.join(args.dputime_root, "example", "gpu", "injection-test", "simple_kernel.cu")
    probe_dir = os.path.join(args.dputime_root, "example", "gpu", "injection-test")
    probe = os.path.join(probe_dir, "probe")
    client = os.path.join(smoke_dir, "simple_kernel_clang_ptx")
    server_log = os.path.join(smoke_dir, "server.log")
    client_log = os.path.join(smoke_dir, "client.log")

    compile_client = run_command(
        [
            clang_cuda,
            f"--cuda-path={cuda_root}",
            f"--cuda-gpu-arch={sm['sm_arch']}",
            "--cuda-include-ptx=all",
            "-std=c++17",
            "-U_GNU_SOURCE",
            "-D_DEFAULT_SOURCE",
            source,
            "-L",
            os.path.join(cuda_root, "lib64"),
            "-lcudart",
            "-ldl",
            "-pthread",
            "-o",
            client,
        ]
    )
    if compile_client["returncode"] != 0:
        report = {
            "ok": False,
            "stage": "compile-client",
            "sm": sm,
            "compile_client": compile_client,
            "client": client,
        }
        write_json(os.path.join(smoke_dir, "cuda_smoke.json"), report)
        return report

    probe_build = run_command(
        ["make", "-C", probe_dir, "probe", "EXTRA_CFLAGS=-Wno-error=discarded-qualifiers"]
    )
    if probe_build["returncode"] != 0:
        report = {
            "ok": False,
            "stage": "build-probe",
            "sm": sm,
            "compile_client": compile_client,
            "probe_build": probe_build,
        }
        write_json(os.path.join(smoke_dir, "cuda_smoke.json"), report)
        return report

    extract_dir = os.path.join(smoke_dir, "cuobjdump-ptx")
    os.makedirs(extract_dir, exist_ok=True)
    extract = run_command(["cuobjdump", "--extract-ptx", "all", client], cwd=extract_dir)

    env = os.environ.copy()
    env["BPFTIME_LOG_OUTPUT"] = "console"
    env["LD_LIBRARY_PATH"] = ":".join(
        path
        for path in [
            os.path.join(cuda_root, "lib64"),
            os.path.join(cuda_root, "targets", "x86_64-linux", "lib"),
            env.get("LD_LIBRARY_PATH", ""),
        ]
        if path
    )

    server_env = env.copy()
    server_env["LD_PRELOAD"] = paths["syscall_server_preload"]
    client_env = env.copy()
    client_env["LD_PRELOAD"] = paths["agent_preload"]

    try:
        os.remove("/tmp/bpftime-recompile-nvcc/main.ptx")
    except FileNotFoundError:
        pass
    try:
        os.remove("/tmp/bpftime-recompile-nvcc/out.fatbin")
    except FileNotFoundError:
        pass

    with open(server_log, "w", encoding="utf-8") as server_out:
        server = subprocess.Popen([probe], cwd=ROOT, env=server_env, stdout=server_out, stderr=subprocess.STDOUT)
    time.sleep(args.cuda_smoke_server_wait)

    with open(client_log, "w", encoding="utf-8") as client_out:
        try:
            client_proc = subprocess.run(
                [client],
                cwd=ROOT,
                env=client_env,
                stdout=client_out,
                stderr=subprocess.STDOUT,
                timeout=args.cuda_smoke_timeout,
                check=False,
            )
            client_rc: object = client_proc.returncode
            client_timed_out = False
        except subprocess.TimeoutExpired:
            client_rc = "timeout"
            client_timed_out = True

    server.terminate()
    try:
        server_rc: object = server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server_rc = server.wait(timeout=5)

    with open(server_log, "r", encoding="utf-8", errors="replace") as f:
        server_text = f.read()
    with open(client_log, "r", encoding="utf-8", errors="replace") as f:
        client_text = f.read()

    ptx_path = "/tmp/bpftime-recompile-nvcc/main.ptx"
    fatbin_path = "/tmp/bpftime-recompile-nvcc/out.fatbin"
    observations = {
        "server_started": "bpftime-syscall-server started" in server_text,
        "cuda_probe_loaded": "Created kprobe/kretprobe perf event handler" in server_text
        or "Injection active" in server_text,
        "agent_started": "Starting nv_attach_impl" in client_text,
        "pass_configs_loaded": "Retrived config" in client_text,
        "pass_matched": "Recorded pass" in client_text,
        "ptx_extracted": "Got 1 PTX files" in client_text or "Got 1 PTX file" in client_text,
        "ptx_patched": "[ptxpass] kprobe_entry: matched=1" in client_text,
        "module_loaded": "Loaded module: patched" in client_text,
        "kernel_launch_oom": "CUDA_ERROR_OUT_OF_MEMORY" in client_text,
        "client_iterations": client_text.count("[iter "),
        "legacy_recompile_ptx": os.path.exists(ptx_path),
        "legacy_recompile_fatbin": os.path.exists(fatbin_path),
    }
    report = {
        "ok": bool(
            observations["server_started"]
            and observations["agent_started"]
            and observations["pass_matched"]
            and observations["ptx_extracted"]
            and observations["ptx_patched"]
            and observations["module_loaded"]
        ),
        "stage": "live-cuda-attach",
        "sm": sm,
        "cuda_root": cuda_root,
        "clang_cuda": clang_cuda,
        "client": client,
        "probe": probe,
        "server_log": server_log,
        "client_log": client_log,
        "client_rc": client_rc,
        "client_timed_out": client_timed_out,
        "server_rc": server_rc,
        "compile_client": compile_client,
        "probe_build": probe_build,
        "cuobjdump_extract": extract,
        "observations": observations,
        "note": "kernel_launch_oom is expected on a busy GPU and does not invalidate attach-path observation.",
    }
    write_json(os.path.join(smoke_dir, "cuda_smoke.json"), report)
    return report


def deploy_arm(args: argparse.Namespace, observer: Dict[str, object]) -> Dict[str, object]:
    if not args.remote or not observer["bpftime_cli"]:
        return {"ok": False, "skipped": True, "reason": "missing remote or bpftime_cli"}

    ssh_base = ["ssh"] + args.ssh_option + [args.remote]
    scp_base = ["scp"] + args.ssh_option
    mkdir = run_command(ssh_base + [f"mkdir -p {args.remote_dir}"])
    if mkdir["returncode"] != 0:
        return {"ok": False, "stage": "mkdir", "result": mkdir}

    copy = run_command(scp_base + [observer["bpftime_cli"], f"{args.remote}:{args.remote_dir}/bpftime-aarch64"])
    if copy["returncode"] != 0:
        return {"ok": False, "stage": "scp", "result": copy}

    inspect = run_command(
        ssh_base
        + [
            f"chmod +x {args.remote_dir}/bpftime-aarch64 && "
            f"{args.remote_dir}/bpftime-aarch64 --version && "
            "uname -m"
        ]
    )
    return {"ok": inspect["returncode"] == 0, "stage": "inspect", "result": inspect}


def inspect_remote_native_arm(args: argparse.Namespace) -> Dict[str, object]:
    if not args.remote:
        return {"ok": False, "skipped": True, "reason": "missing remote"}
    ssh_base = ["ssh"] + args.ssh_option + [args.remote]
    inspect = run_command(
        ssh_base
        + [
            f"test -x {args.remote_bpftime} && "
            f"{args.remote_bpftime} --version && "
            f"file {args.remote_bpftime} && "
            "uname -m"
        ]
    )
    return {"ok": inspect["returncode"] == 0, "stage": "remote-native-inspect", "result": inspect}


def build_observers(args: argparse.Namespace) -> Dict[str, object]:
    if args.clean:
        for path in [args.host_build_dir, args.dpu_arm_build_dir]:
            if os.path.isdir(path):
                shutil.rmtree(path)

    cuda_root = detect_cuda_root(args.cuda_root)
    report = {
        "version": 1,
        "kind": "damer.dputime.observers",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dputime": {
            "root": os.path.abspath(args.dputime_root),
            "commit": git_commit(args.dputime_root),
            "upstream": "https://github.com/vickiegpt/dputime",
        },
        "observers": {},
    }

    host_configure = None
    host_build = None
    host_extra = host_gpu_status(cuda_root)
    host_extra["required_paths"] = required_host_cuda_paths(os.path.abspath(args.host_build_dir))
    host_extra["build_targets"] = CUDA_HOST_TARGETS
    if args.host_cuda:
        if not cuda_root or not os.path.isdir(cuda_root):
            host_extra["error"] = "CUDA root not found"
        else:
            host_configure = configure_host_cuda(args, cuda_root)
            if host_configure["returncode"] == 0:
                host_build = build_targets(args.host_build_dir, CUDA_HOST_TARGETS)
                if args.cuda_smoke and host_build["returncode"] == 0:
                    host_extra["live_cuda_smoke"] = run_cuda_smoke(args, cuda_root)
    report["observers"]["gpu_host"] = observer_summary(
        "gpu_host",
        "gpu_host_cuda_attach",
        "x86_64",
        os.path.abspath(args.host_build_dir),
        host_configure,
        host_build,
        host_extra,
    )

    dpu_configure = None
    dpu_build = None
    dpu_extra = {
        "toolchain": os.path.join(args.dputime_root, "cmake", "aarch64-toolchain.cmake"),
        "cross_compiler": shutil.which("aarch64-linux-gnu-g++"),
        "remote_native": args.remote_native_arm,
        "remote": args.remote,
        "remote_bpftime": args.remote_bpftime if args.remote_native_arm else None,
    }
    if args.dpu_arm and not args.remote_native_arm:
        if not dpu_extra["cross_compiler"]:
            dpu_extra["error"] = "aarch64-linux-gnu-g++ not found"
        else:
            dpu_configure = configure_dpu_arm(args)
            if dpu_configure["returncode"] == 0:
                dpu_build = build_target(args.dpu_arm_build_dir, "bpftime")
    if args.remote_native_arm:
        remote_inspect = inspect_remote_native_arm(args)
        dpu_observer = {
            "name": "bluefield_dpu",
            "role": "dpu_arm_runtime",
            "arch": "aarch64",
            "build_dir": args.remote_build_dir,
            "bpftime_cli": f"{args.remote}:{args.remote_bpftime}" if args.remote else args.remote_bpftime,
            "built": bool(remote_inspect["ok"]),
            "configure_ok": None,
            "build_ok": None,
            "configure": None,
            "build": None,
            "remote_native": True,
            "remote_inspect": remote_inspect,
            **dpu_extra,
        }
    else:
        dpu_observer = observer_summary(
            "bluefield_dpu",
            "dpu_arm_runtime",
            "aarch64",
            os.path.abspath(args.dpu_arm_build_dir),
            dpu_configure,
            dpu_build,
            dpu_extra,
        )
        dpu_observer["remote_deploy"] = deploy_arm(args, dpu_observer) if args.deploy_arm else None
    report["observers"]["bluefield_dpu"] = dpu_observer

    report["ok"] = all(
        observer["built"]
        for key, observer in report["observers"].items()
        if (key == "gpu_host" and args.host_cuda) or (key == "bluefield_dpu" and args.dpu_arm)
    )
    if args.cuda_smoke:
        report["ok"] = bool(
            report["ok"]
            and report["observers"]["gpu_host"].get("live_cuda_smoke", {}).get("ok")
        )
    if args.deploy_arm and not args.remote_native_arm:
        report["ok"] = bool(report["ok"] and dpu_observer["remote_deploy"]["ok"])

    write_json(args.report, report)
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build dputime observers for Damer offload")
    parser.add_argument("--dputime-root", default=DEFAULT_DPUTIME_ROOT)
    parser.add_argument("--host-build-dir", default=DEFAULT_HOST_BUILD)
    parser.add_argument("--dpu-arm-build-dir", default=DEFAULT_DPU_ARM_BUILD)
    parser.add_argument("--cuda-root")
    parser.add_argument("--llvm-dir", default="/usr/lib/llvm-20/lib/cmake/llvm")
    parser.add_argument("--host-cuda", action="store_true", default=True)
    parser.add_argument("--no-host-cuda", action="store_false", dest="host_cuda")
    parser.add_argument("--dpu-arm", action="store_true", default=True)
    parser.add_argument("--no-dpu-arm", action="store_false", dest="dpu_arm")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--remote", help="optional DPU SSH target")
    parser.add_argument("--remote-dir", default="/tmp/damer-dputime")
    parser.add_argument("--remote-build-dir", default="/tmp/damer-dputime-build")
    parser.add_argument("--remote-bpftime", default="/tmp/damer-dputime-build/tools/cli/bpftime")
    parser.add_argument("--remote-native-arm", action="store_true")
    parser.add_argument("--deploy-arm", action="store_true")
    parser.add_argument("--dpu-try-compile-static", action="store_true", default=True)
    parser.add_argument("--cuda-smoke", action="store_true", help="run a live CUDA attach smoke on the host GPU")
    parser.add_argument("--cuda-smoke-dir", default=DEFAULT_CUDA_SMOKE_DIR)
    parser.add_argument("--cuda-smoke-clang", help="clang++ binary used for the live CUDA smoke")
    parser.add_argument("--cuda-smoke-timeout", type=float, default=10.0)
    parser.add_argument("--cuda-smoke-server-wait", type=float, default=2.0)
    parser.add_argument(
        "--ssh-option",
        action="append",
        default=[],
        help="extra option passed to ssh/scp; repeat for each argument",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = build_observers(args)
    host = report["observers"]["gpu_host"]
    dpu = report["observers"]["bluefield_dpu"]
    print(
        "DPUTIME_OBSERVERS ok={ok} host_built={host_built} dpu_built={dpu_built} cuda_root={cuda_root}".format(
            ok=str(report["ok"]).lower(),
            host_built=str(host["built"]).lower(),
            dpu_built=str(dpu["built"]).lower(),
            cuda_root=host.get("cuda_root"),
        )
    )
    print(f"DPUTIME_OBSERVERS_REPORT path={args.report}")
    if dpu.get("remote_deploy") is not None:
        print(
            "DPUTIME_ARM_REMOTE ok={ok} stage={stage}".format(
                ok=str(dpu["remote_deploy"].get("ok")).lower(),
                stage=dpu["remote_deploy"].get("stage"),
            )
        )
    if dpu.get("remote_inspect") is not None:
        print(
            "DPUTIME_ARM_REMOTE ok={ok} stage={stage}".format(
                ok=str(dpu["remote_inspect"].get("ok")).lower(),
                stage=dpu["remote_inspect"].get("stage"),
            )
        )
    smoke = host.get("live_cuda_smoke")
    if smoke is not None:
        observations = smoke.get("observations", {})
        print(
            "DPUTIME_CUDA_SMOKE ok={ok} stage={stage} pass_matched={pass_matched} "
            "ptx_patched={ptx_patched} module_loaded={module_loaded} kernel_oom={kernel_oom}".format(
                ok=str(smoke.get("ok")).lower(),
                stage=smoke.get("stage"),
                pass_matched=str(observations.get("pass_matched")).lower(),
                ptx_patched=str(observations.get("ptx_patched")).lower(),
                module_loaded=str(observations.get("module_loaded")).lower(),
                kernel_oom=str(observations.get("kernel_launch_oom")).lower(),
            )
        )
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
