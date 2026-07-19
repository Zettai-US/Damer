#!/usr/bin/env python3
"""Damer eBPF middleware compiler.

This compiler is intentionally independent from CIRCT/MLIR. bpftime is the
eBPF frontend/runtime; Damer is the middleware compiler that turns movement
intent into an optimized, verified data-movement plan and bpftime-facing C.
"""

import argparse
import json
import os
import re
import sys
import zlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


TRANSFORM_FLAGS = {
    "quantize": "DAMER_TRANSFORM_QUANTIZE",
    "compress": "DAMER_TRANSFORM_COMPRESS",
    "checksum": "DAMER_TRANSFORM_CHECKSUM",
    "filter": "DAMER_TRANSFORM_FILTER",
    "reduce": "DAMER_TRANSFORM_REDUCE",
    "scatter_gather": "DAMER_TRANSFORM_SCATTER_GATHER",
    "replicate": "DAMER_TRANSFORM_REPLICATE",
    "persist": "DAMER_TRANSFORM_PERSIST",
}

ACTION_ALIASES = {
    "move": "move",
    "quantize": "quantize",
    "compress": "compress",
    "checksum": "checksum",
    "filter": "filter",
    "reduce": "reduce",
    "scatter": "scatter_gather",
    "gather": "scatter_gather",
    "scatter_gather": "scatter_gather",
    "scattergather": "scatter_gather",
    "replicate": "replicate",
    "persist": "persist",
}

NODE_ENUMS = {
    "unknown": "DAMER_NODE_UNKNOWN",
    "host_memory": "DAMER_NODE_HOST_MEMORY",
    "cxl_memory_device": "DAMER_NODE_CXL_MEMORY_DEVICE",
    "gpu_memory": "DAMER_NODE_GPU_MEMORY",
    "accelerator": "DAMER_NODE_ACCELERATOR",
    "switch_compute_engine": "DAMER_NODE_SWITCH_COMPUTE_ENGINE",
}

ORDERING_ENUMS = {
    "none": "DAMER_ORDER_NONE",
    "program_order": "DAMER_ORDER_PROGRAM",
    "program": "DAMER_ORDER_PROGRAM",
    "acquire_release": "DAMER_ORDER_ACQUIRE_RELEASE",
    "total": "DAMER_ORDER_TOTAL",
}

OWNERSHIP_ENUMS = {
    "unknown": "DAMER_OWNERSHIP_UNKNOWN",
    "borrowed": "DAMER_OWNERSHIP_BORROWED",
    "source": "DAMER_OWNERSHIP_SOURCE",
    "destination": "DAMER_OWNERSHIP_DESTINATION",
    "shared": "DAMER_OWNERSHIP_SHARED",
}

DATA_REDUCTION_TRANSFORMS = {"quantize", "compress", "checksum", "filter", "reduce"}
FABRIC_ROUTING_TRANSFORMS = {"scatter_gather", "scatter", "gather", "replicate", "persist"}


@dataclass
class Argument:
    name: str
    type: str
    node: str = "unknown"


@dataclass
class Operation:
    op: str
    result: Optional[str] = None
    operands: List[str] = field(default_factory=list)
    attrs: Dict[str, object] = field(default_factory=dict)


@dataclass
class Switchlet:
    name: str
    args: List[Argument]
    ops: List[Operation]
    attrs: Dict[str, object] = field(default_factory=dict)


@dataclass
class Edge:
    edge_id: str
    source_arg: str
    destination_arg: str
    source_node: str
    destination_node: str
    transforms: List[str]
    attrs: Dict[str, object]
    placement: str

    @property
    def actions(self) -> List[str]:
        return ["move"] + self.transforms

    @property
    def kind(self) -> str:
        return "+".join(self.actions)


@dataclass
class CompileOptions:
    max_transforms: int = 8
    max_rdma_ops: int = 16
    default_ttl: int = 4
    emit_decision: bool = True
    strict: bool = False


class CompileError(Exception):
    pass


def normalize_name(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace("/", "_").replace("+", "_")
    return ACTION_ALIASES.get(normalized, normalized)


def sanitize_c_identifier(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_]", "_", value)
    if not value:
        return "anonymous"
    if value[0].isdigit():
        value = "_" + value
    return value


def split_top_level_commas(value: str) -> List[str]:
    parts = []
    start = 0
    depth = 0
    in_string = False
    escape = False

    for index, char in enumerate(value):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1

    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def parse_args(arg_text: str) -> List[Argument]:
    args = []
    for item in split_top_level_commas(arg_text):
        match = re.fullmatch(r"%([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)", item)
        if not match:
            raise CompileError(f"cannot parse switchlet argument: {item}")
        name, typ = match.groups()
        args.append(Argument(name=name, type=typ.strip(), node=classify_node(typ)))
    return args


def parse_damer_dsl(text: str) -> Switchlet:
    match = re.search(
        r"damer\.switchlet\s+@([A-Za-z_][A-Za-z0-9_]*)\s*"
        r"\((.*?)\)\s*\{(.*)\}\s*$",
        text,
        re.DOTALL,
    )
    if not match:
        raise CompileError("expected `damer.switchlet @name(args) { ... }`")

    name, arg_text, body = match.groups()
    args = parse_args(arg_text)
    ops = []

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        line = line.rstrip(";")

        read = re.fullmatch(
            r"%([A-Za-z_][A-Za-z0-9_]*)\s*=\s*damer\.read\s+%([A-Za-z_][A-Za-z0-9_]*)",
            line,
        )
        if read:
            result, operand = read.groups()
            ops.append(Operation(op="read", result=result, operands=[operand]))
            continue

        transform = re.fullmatch(
            r"%([A-Za-z_][A-Za-z0-9_]*)\s*=\s*damer\.([A-Za-z_][A-Za-z0-9_-]*)\s+%([A-Za-z_][A-Za-z0-9_]*)",
            line,
        )
        if transform:
            result, op, operand = transform.groups()
            ops.append(Operation(op=normalize_name(op), result=result, operands=[operand]))
            continue

        write = re.fullmatch(
            r"damer\.write\s+%([A-Za-z_][A-Za-z0-9_]*)\s*,\s*%([A-Za-z_][A-Za-z0-9_]*)",
            line,
        )
        if write:
            value, destination = write.groups()
            ops.append(Operation(op="write", operands=[value, destination]))
            continue

        raise CompileError(f"cannot parse switchlet body line: {line}")

    return Switchlet(name=name, args=args, ops=ops, attrs={"frontend": "bpftime"})


def require_mapping(value: object, context: str) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise CompileError(f"{context} must be an object")
    return value


def require_sequence(value: object, context: str) -> List[object]:
    if not isinstance(value, list):
        raise CompileError(f"{context} must be a list")
    return value


def parse_arg_mapping(raw_arg: object, context: str) -> Argument:
    arg = require_mapping(raw_arg, context)
    if "name" not in arg or "type" not in arg:
        raise CompileError(f"{context} requires name and type")
    typ = str(arg["type"])
    return Argument(
        name=str(arg["name"]),
        type=typ,
        node=str(arg.get("node") or classify_node(typ)),
    )


def parse_operands(raw_operands: object, context: str) -> List[str]:
    operands = require_sequence(raw_operands, context)
    return [str(value).lstrip("%") for value in operands]


def parse_json_switchlet(data: Dict[str, object]) -> Switchlet:
    data = require_mapping(data, "JSON switchlet")
    name = str(data.get("switchlet") or data.get("program") or "anonymous_switchlet")
    raw_args = data.get("args")
    if raw_args is None and ("source" in data or "destination" in data):
        if "source" not in data or "destination" not in data:
            raise CompileError("JSON switchlet requires both source and destination")
        raw_args = [
            require_mapping(data["source"], "source"),
            require_mapping(data["destination"], "destination"),
        ]
    if raw_args is None:
        raw_args = []

    raw_args = require_sequence(raw_args, "JSON switchlet args")

    args = [
        parse_arg_mapping(arg, f"JSON switchlet arg {index}")
        for index, arg in enumerate(raw_args)
    ]

    if len(args) < 2:
        raise CompileError("JSON switchlet requires at least src/dst args")

    ops = []
    if "pipeline" in data:
        for index, raw_op in enumerate(require_sequence(data["pipeline"], "pipeline")):
            raw_op = require_mapping(raw_op, f"pipeline op {index}")
            if "op" not in raw_op:
                raise CompileError(f"pipeline op {index} requires op")
            op_name = normalize_name(str(raw_op["op"]))
            result = raw_op.get("result")
            operands = []
            if op_name == "write":
                if "operands" in raw_op:
                    operands = parse_operands(raw_op["operands"], f"pipeline op {index} operands")
                elif "value" in raw_op:
                    operands = [str(raw_op["value"]).lstrip("%")]
                    if "destination" in raw_op:
                        operands.append(str(raw_op["destination"]).lstrip("%"))
                    elif "arg" in raw_op:
                        operands.append(str(raw_op["arg"]).lstrip("%"))
                else:
                    raise CompileError("pipeline write op requires value and destination")
            elif "operands" in raw_op:
                operands = parse_operands(raw_op["operands"], f"pipeline op {index} operands")
            elif "arg" in raw_op:
                operands = [str(raw_op["arg"]).lstrip("%")]
            elif "value" in raw_op:
                operands = [str(raw_op["value"]).lstrip("%")]
            attrs = raw_op.get("attrs", {})
            if attrs is None:
                attrs = {}
            attrs = require_mapping(attrs, f"pipeline op {index} attrs")
            ops.append(
                Operation(
                    op=op_name,
                    result=str(result).lstrip("%") if result else None,
                    operands=operands,
                    attrs=dict(attrs),
                )
            )
    else:
        raw_actions = data.get("actions", ["move"])
        raw_actions = require_sequence(raw_actions, "actions")
        actions = [normalize_name(str(action)) for action in raw_actions]
        transforms = [action for action in actions if action != "move"]
        edge_attrs = data.get("edge", {})
        if edge_attrs is None:
            edge_attrs = {}
        edge_attrs = require_mapping(edge_attrs, "edge")
        ops.append(Operation(op="read", result="v0", operands=[args[0].name], attrs=edge_attrs))
        current = "v0"
        for index, transform in enumerate(transforms, start=1):
            result = f"v{index}"
            ops.append(Operation(op=transform, result=result, operands=[current], attrs=edge_attrs))
            current = result
        ops.append(Operation(op="write", operands=[current, args[1].name], attrs=edge_attrs))

    return Switchlet(name=name, args=args, ops=ops, attrs={"frontend": "bpftime"})


def load_switchlet(path: str) -> Switchlet:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    if path.endswith(".json") or text.lstrip().startswith("{"):
        return parse_json_switchlet(json.loads(text))
    return parse_damer_dsl(text)


def classify_node(typ: str) -> str:
    lowered = typ.lower()
    memory_space = ""
    match = re.search(r"memref<.*,\s*\"([^\"]+)\"\s*>", typ)
    if match:
        memory_space = match.group(1).lower()

    if not memory_space:
        return "host_memory" if "memref<" in lowered else "unknown"
    if "cxl" in memory_space:
        return "cxl_memory_device"
    if any(token in memory_space for token in ("gpu", "cuda", "nvptx", "hbm")):
        return "gpu_memory"
    if any(token in memory_space for token in ("switch", "dpa", "bluefield")):
        return "switch_compute_engine"
    if any(token in memory_space for token in ("accel", "device")):
        return "accelerator"
    if any(token in memory_space for token in ("host", "cpu", "dram")):
        return "host_memory"
    return "unknown"


def op_definitions(switchlet: Switchlet) -> Dict[str, Operation]:
    return {op.result: op for op in switchlet.ops if op.result}


def trace_write(write: Operation, definitions: Dict[str, Operation]) -> Tuple[str, List[str], List[Operation]]:
    if len(write.operands) < 2:
        raise CompileError("damer.write requires value and destination operands")

    current = write.operands[0]
    transforms = []
    property_ops = [write]
    visited = set()

    while current in definitions:
        if current in visited:
            raise CompileError("cycle in switchlet SSA values")
        visited.add(current)

        op = definitions[current]
        property_ops.append(op)
        if op.op == "read":
            if not op.operands:
                raise CompileError("damer.read requires a source operand")
            property_ops.reverse()
            transforms.reverse()
            return op.operands[0], transforms, property_ops

        if op.op in TRANSFORM_FLAGS or op.op in {"scatter", "gather"}:
            transforms.append("scatter_gather" if op.op in {"scatter", "gather"} else op.op)
            if not op.operands:
                raise CompileError(f"damer.{op.op} requires an operand")
            current = op.operands[0]
            continue

        raise CompileError(f"unsupported operation in movement pipeline: damer.{op.op}")

    raise CompileError(f"cannot trace write value `%{write.operands[0]}` back to damer.read")


def merged_attrs(ops: Iterable[Operation]) -> Dict[str, object]:
    merged = {}
    for op in ops:
        merged.update(op.attrs)
    return merged


def choose_placement(source_node: str, destination_node: str, transforms: Sequence[str]) -> str:
    transform_set = set(transforms)
    if transform_set & (DATA_REDUCTION_TRANSFORMS | FABRIC_ROUTING_TRANSFORMS):
        if source_node in {"cxl_memory_device", "gpu_memory"}:
            return "switch_compute_engine"
        if destination_node in {"cxl_memory_device", "gpu_memory"}:
            return "switch_compute_engine"
        return "accelerator"
    if source_node == destination_node:
        return source_node
    if "gpu_memory" in {source_node, destination_node}:
        return "accelerator"
    return destination_node


def build_edges(switchlet: Switchlet, options: CompileOptions) -> List[Edge]:
    args = {arg.name: arg for arg in switchlet.args}
    definitions = op_definitions(switchlet)
    edges = []

    for index, write in enumerate(op for op in switchlet.ops if op.op == "write"):
        source_name, transforms, property_ops = trace_write(write, definitions)
        destination_name = write.operands[1]
        if source_name not in args:
            raise CompileError(f"unknown source argument `%{source_name}`")
        if destination_name not in args:
            raise CompileError(f"unknown destination argument `%{destination_name}`")

        source_node = args[source_name].node
        destination_node = args[destination_name].node
        placement = choose_placement(source_node, destination_node, transforms)
        edge_attrs = default_edge_attrs(options.default_ttl)
        edge_attrs.update(merged_attrs(property_ops))
        edges.append(
            Edge(
                edge_id=f"{switchlet.name}.edge{index}",
                source_arg=source_name,
                destination_arg=destination_name,
                source_node=source_node,
                destination_node=destination_node,
                transforms=transforms,
                attrs=edge_attrs,
                placement=placement,
            )
        )

    if not edges:
        raise CompileError("switchlet contains no damer.write operations")
    return edges


def default_edge_attrs(default_ttl: int) -> Dict[str, object]:
    return {
        "bytes": 0,
        "stride": "contiguous",
        "reuse_distance": "unknown",
        "read_write_ratio": "1:1",
        "ordering_requirement": "program_order",
        "alias_set": "unknown",
        "ownership": "borrowed",
        "ttl": default_ttl,
        "priority": 0,
    }


def parse_int(value: object, fallback: int = 0) -> int:
    if value is None:
        return fallback
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text in {"unknown", "dynamic", "contiguous"}:
        return fallback
    try:
        return int(text, 0)
    except ValueError:
        return fallback


def parse_alias(value: object) -> int:
    text = str(value)
    if text in {"", "unknown"}:
        return 0
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    return zlib.crc32(text.encode("utf-8"))


def parse_ratio(value: object) -> Tuple[int, int]:
    text = str(value)
    match = re.fullmatch(r"\s*([0-9]+)\s*:\s*([0-9]+)\s*", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    return 1, 1


def transform_mask(transforms: Sequence[str]) -> str:
    flags = [TRANSFORM_FLAGS[transform] for transform in transforms if transform in TRANSFORM_FLAGS]
    return " | ".join(flags) if flags else "DAMER_TRANSFORM_NONE"


def enum_lookup(table: Dict[str, str], value: object, fallback: str) -> str:
    key = normalize_name(str(value))
    return table.get(key, fallback)


def estimate_rdma_ops(edge: Edge) -> int:
    fanout = parse_int(edge.attrs.get("fanout"), 1)
    if "replicate" in edge.transforms:
        fanout = max(fanout, 2)
    if "scatter_gather" in edge.transforms:
        fanout = max(fanout, parse_int(edge.attrs.get("fragments"), 2))
    return fanout


def verify_edges(edges: Sequence[Edge], options: CompileOptions) -> Dict[str, object]:
    diagnostics = []
    total_rdma_ops = 0

    for edge in edges:
        if len(edge.transforms) > options.max_transforms:
            diagnostics.append(
                {
                    "edge": edge.edge_id,
                    "severity": "error",
                    "message": f"too many transforms: {len(edge.transforms)} > {options.max_transforms}",
                }
            )

        rdma_ops = estimate_rdma_ops(edge)
        total_rdma_ops += rdma_ops
        if rdma_ops > options.max_rdma_ops:
            diagnostics.append(
                {
                    "edge": edge.edge_id,
                    "severity": "error",
                    "message": f"unbounded RDMA fanout: {rdma_ops} > {options.max_rdma_ops}",
                }
            )

        if edge.attrs.get("redirect") or edge.attrs.get("retry"):
            ttl = parse_int(edge.attrs.get("ttl"), 0)
            if ttl <= 0:
                diagnostics.append(
                    {
                        "edge": edge.edge_id,
                        "severity": "error",
                        "message": "redirect/retry edges must carry a positive TTL",
                    }
                )

        if edge.source_node == "unknown" or edge.destination_node == "unknown":
            severity = "error" if options.strict else "warning"
            diagnostics.append(
                {
                    "edge": edge.edge_id,
                    "severity": severity,
                    "message": "source or destination node kind is unknown",
                }
            )

    hard_errors = [item for item in diagnostics if item["severity"] == "error"]
    return {
        "ok": not hard_errors,
        "diagnostics": diagnostics,
        "effect_summary": {
            "input_events": 1,
            "emitted_edges": len(edges),
            "max_helper_calls_per_event": 2,
            "max_rdma_ops_per_event": total_rdma_ops,
            "retry_ttl_required": True,
            "epoch_atomic_update": True,
            "no_blocking_waits": True,
        },
    }


def compile_plan(switchlet: Switchlet, options: CompileOptions) -> Dict[str, object]:
    edges = build_edges(switchlet, options)
    verifier = verify_edges(edges, options)
    nodes = [
        {"id": arg.name, "kind": arg.node, "type": arg.type}
        for arg in switchlet.args
    ]

    placements = sorted({edge.placement for edge in edges})
    if placements:
        nodes.append({"id": "compute", "kind": placements[0] if len(placements) == 1 else "mixed"})

    return {
        "version": 1,
        "kind": "damer.ebpf.middleware.plan",
        "switchlet": switchlet.name,
        "frontend": "bpftime",
        "nodes": nodes,
        "edges": [edge_to_plan(edge) for edge in edges],
        "bpftime": {
            "program_section": f"damer/switchlet/{switchlet.name}",
            "helpers": ["damer_emit_edge", "damer_submit_decision"],
            "maps": ["damer_edge_events", "damer_node_state", "damer_decisions"],
            "runtime": "bpftime",
        },
        "verifier": verifier,
    }


def edge_to_plan(edge: Edge) -> Dict[str, object]:
    return {
        "id": edge.edge_id,
        "kind": edge.kind,
        "actions": edge.actions,
        "source": {"arg": edge.source_arg, "node": edge.source_node},
        "destination": {"arg": edge.destination_arg, "node": edge.destination_node},
        "placement": edge.placement,
        "properties": {
            "bytes": edge.attrs.get("bytes"),
            "stride": edge.attrs.get("stride"),
            "reuse_distance": edge.attrs.get("reuse_distance"),
            "read_write_ratio": edge.attrs.get("read_write_ratio"),
            "ordering_requirement": edge.attrs.get("ordering_requirement"),
            "alias_set": edge.attrs.get("alias_set"),
            "ownership": edge.attrs.get("ownership"),
            "ttl": edge.attrs.get("ttl"),
            "priority": edge.attrs.get("priority"),
        },
        "effect": {
            "max_rdma_ops": estimate_rdma_ops(edge),
            "bounded": True,
            "blocking_waits": [],
        },
    }


def compile_bpf_c(plan: Dict[str, object]) -> str:
    function_name = sanitize_c_identifier(str(plan["switchlet"]))
    edges = plan["edges"]
    if len(edges) != 1:
        return compile_multi_edge_bpf_c(plan)

    edge = edges[0]
    props = edge["properties"]
    read_ratio, write_ratio = parse_ratio(props.get("read_write_ratio"))
    placement = NODE_ENUMS.get(edge["placement"], "DAMER_NODE_UNKNOWN")
    priority = parse_int(props.get("priority"), 0)
    ttl = parse_int(props.get("ttl"), 4)

    lines = [
        '#include "damer/bpftime_frontend.h"',
        "",
        f'SEC("damer/switchlet/{function_name}")',
        f"int {function_name}(struct damer_bpf_ctx *ctx) {{",
        "  struct damer_edge_event edge = {",
        f"      .source_node = {NODE_ENUMS.get(edge['source']['node'], 'DAMER_NODE_UNKNOWN')},",
        f"      .destination_node = {NODE_ENUMS.get(edge['destination']['node'], 'DAMER_NODE_UNKNOWN')},",
        f"      .transformations = {transform_mask(edge['actions'][1:])},",
        f"      .ordering = {enum_lookup(ORDERING_ENUMS, props.get('ordering_requirement'), 'DAMER_ORDER_PROGRAM')},",
        f"      .ownership = {enum_lookup(OWNERSHIP_ENUMS, props.get('ownership'), 'DAMER_OWNERSHIP_BORROWED')},",
        f"      .alias_set = {parse_alias(props.get('alias_set'))}u,",
        f"      .bytes = {parse_int(props.get('bytes'), 0)}ull,",
        f"      .stride = {parse_int(props.get('stride'), 1)}ull,",
        f"      .reuse_distance = {parse_int(props.get('reuse_distance'), 0)}ull,",
        f"      .read_ratio = {read_ratio}u,",
        f"      .write_ratio = {write_ratio}u,",
        "      .source_addr = 0,",
        "      .destination_addr = 0,",
        "  };",
        "",
        "  long ret = damer_emit_edge(ctx, &edge);",
        "  if (ret < 0)",
        "    return ret;",
        "",
        "  struct damer_decision decision = {",
        f"      .placement_node = {placement},",
        f"      .action_mask = {transform_mask(edge['actions'][1:])},",
        f"      .priority = {priority}u,",
        f"      .ttl = {ttl}u,",
        "  };",
        "  return damer_submit_decision(ctx, &decision);",
        "}",
        "",
        'char LICENSE[] SEC("license") = "Dual BSD/GPL";',
        "",
    ]
    return "\n".join(lines)


def compile_multi_edge_bpf_c(plan: Dict[str, object]) -> str:
    function_name = sanitize_c_identifier(str(plan["switchlet"]))
    lines = [
        '#include "damer/bpftime_frontend.h"',
        "",
        f'SEC("damer/switchlet/{function_name}")',
        f"int {function_name}(struct damer_bpf_ctx *ctx) {{",
        "  long ret = 0;",
    ]

    for index, edge in enumerate(plan["edges"]):
        props = edge["properties"]
        read_ratio, write_ratio = parse_ratio(props.get("read_write_ratio"))
        lines.extend(
            [
                f"  struct damer_edge_event edge_{index} = {{",
                f"      .source_node = {NODE_ENUMS.get(edge['source']['node'], 'DAMER_NODE_UNKNOWN')},",
                f"      .destination_node = {NODE_ENUMS.get(edge['destination']['node'], 'DAMER_NODE_UNKNOWN')},",
                f"      .transformations = {transform_mask(edge['actions'][1:])},",
                f"      .ordering = {enum_lookup(ORDERING_ENUMS, props.get('ordering_requirement'), 'DAMER_ORDER_PROGRAM')},",
                f"      .ownership = {enum_lookup(OWNERSHIP_ENUMS, props.get('ownership'), 'DAMER_OWNERSHIP_BORROWED')},",
                f"      .alias_set = {parse_alias(props.get('alias_set'))}u,",
                f"      .bytes = {parse_int(props.get('bytes'), 0)}ull,",
                f"      .stride = {parse_int(props.get('stride'), 1)}ull,",
                f"      .reuse_distance = {parse_int(props.get('reuse_distance'), 0)}ull,",
                f"      .read_ratio = {read_ratio}u,",
                f"      .write_ratio = {write_ratio}u,",
                "      .source_addr = 0,",
                "      .destination_addr = 0,",
                "  };",
                f"  ret = damer_emit_edge(ctx, &edge_{index});",
                "  if (ret < 0)",
                "    return ret;",
            ]
        )

    lines.extend(
        [
            "",
            "  return 0;",
            "}",
            "",
            'char LICENSE[] SEC("license") = "Dual BSD/GPL";',
            "",
        ]
    )
    return "\n".join(lines)


def compile_manifest(plan: Dict[str, object]) -> Dict[str, object]:
    return {
        "version": 1,
        "kind": "damer.bpftime.middleware.manifest",
        "switchlet": plan["switchlet"],
        "section": plan["bpftime"]["program_section"],
        "object": f"{sanitize_c_identifier(str(plan['switchlet']))}.bpf.o",
        "headers": ["include/damer/bpftime_frontend.h"],
        "helpers": plan["bpftime"]["helpers"],
        "maps": plan["bpftime"]["maps"],
        "schema": {
            "nodes": sorted(NODE_ENUMS.keys()),
            "actions": ["move"] + sorted(TRANSFORM_FLAGS.keys()),
            "edge": [
                "bytes",
                "stride",
                "reuse_distance",
                "read_write_ratio",
                "source",
                "destination",
                "ordering_requirement",
                "alias_set",
                "ownership",
                "transformation",
            ],
        },
        "verifier": plan["verifier"],
    }


def emit_all(plan: Dict[str, object], output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)
    name = sanitize_c_identifier(str(plan["switchlet"]))
    paths = {
        "plan": os.path.join(output_dir, f"{name}.plan.json"),
        "bpf_c": os.path.join(output_dir, f"{name}.bpf.c"),
        "manifest": os.path.join(output_dir, f"{name}.bpftime.json"),
    }
    with open(paths["plan"], "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, sort_keys=True)
        f.write("\n")
    with open(paths["bpf_c"], "w", encoding="utf-8") as f:
        f.write(compile_bpf_c(plan))
    with open(paths["manifest"], "w", encoding="utf-8") as f:
        json.dump(compile_manifest(plan), f, indent=2, sort_keys=True)
        f.write("\n")


def print_output(plan: Dict[str, object], emit: str) -> None:
    if emit == "plan":
        json.dump(plan, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    elif emit == "bpf-c":
        sys.stdout.write(compile_bpf_c(plan))
    elif emit == "manifest":
        json.dump(compile_manifest(plan), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        raise CompileError("--emit all requires --output-dir")


def parse_args_cli(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile Damer switchlets for bpftime")
    parser.add_argument("input", help=".damer DSL or JSON switchlet/event file")
    parser.add_argument(
        "--emit",
        choices=["plan", "bpf-c", "manifest", "all"],
        default="plan",
        help="artifact to emit",
    )
    parser.add_argument("--output-dir", help="directory for --emit all")
    parser.add_argument("--strict", action="store_true", help="treat unknown nodes as verifier errors")
    parser.add_argument("--max-transforms", type=int, default=8)
    parser.add_argument("--max-rdma-ops", type=int, default=16)
    parser.add_argument("--default-ttl", type=int, default=4)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args_cli(argv)
    options = CompileOptions(
        max_transforms=args.max_transforms,
        max_rdma_ops=args.max_rdma_ops,
        default_ttl=args.default_ttl,
        strict=args.strict,
    )

    try:
        switchlet = load_switchlet(args.input)
        plan = compile_plan(switchlet, options)
        if args.emit == "all":
            if not args.output_dir:
                raise CompileError("--emit all requires --output-dir")
            emit_all(plan, args.output_dir)
        else:
            print_output(plan, args.emit)
    except (CompileError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"damer-compile: error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
