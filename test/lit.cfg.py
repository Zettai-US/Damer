# -*- Python -*-

import os
import shutil

import lit.formats

from lit.llvm import llvm_config

config.name = "CXLDataMovement"
use_lit_shell = llvm_config.use_lit_shell if llvm_config else False
config.test_format = lit.formats.ShTest(not use_lit_shell)
config.suffixes = [".mlir", ".py"]
config.test_source_root = os.path.dirname(__file__)
if not hasattr(config, "cxl_data_movement_obj_root"):
    config.cxl_data_movement_obj_root = os.path.join(
        os.path.dirname(config.test_source_root),
        "out",
        "lit",
    )
if not hasattr(config, "cxl_data_movement_tools_dir"):
    config.cxl_data_movement_tools_dir = os.path.join(
        os.path.dirname(config.test_source_root),
        "build",
        "tools",
        "cxl-data-movement-opt",
    )
if not hasattr(config, "llvm_tools_dir"):
    filecheck = shutil.which("FileCheck")
    config.llvm_tools_dir = os.path.dirname(filecheck) if filecheck else "/usr/local/firtool-1.62.0/bin"
config.test_exec_root = os.path.join(config.cxl_data_movement_obj_root, "test")

config.excludes = [
    "Inputs",
    "CMakeLists.txt",
    "damer_middleware_compiler_test.py",
    "lit.cfg.py",
]

tool_dirs = [config.cxl_data_movement_tools_dir, config.llvm_tools_dir]
tools = [
    "cxl-data-movement-opt",
    "FileCheck",
]

tool_search_path = os.pathsep.join(tool_dirs + [os.environ.get("PATH", "")])
if not shutil.which("cxl-data-movement-opt", path=tool_search_path):
    config.excludes.extend(
        [
            "cxl-hw-data-movement.mlir",
            "cxl-sw-data-movement.mlir",
        ]
    )

if llvm_config:
    llvm_config.with_system_environment(["HOME", "INCLUDE", "LIB", "TMP", "TEMP"])
    llvm_config.use_default_substitutions()
    llvm_config.add_tool_substitutions(tools, tool_dirs)
else:
    config.environment["PATH"] = os.pathsep.join(
        [config.llvm_tools_dir, config.environment.get("PATH", os.environ.get("PATH", ""))]
    )
