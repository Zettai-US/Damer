#!/usr/bin/env python3
"""Dependency-free newline JSON-RPC stdio MCP adapter for the measured store."""
import argparse
import json
import os
import sys
import time
from dsec import SandboxStore


TOOLS = [
    dict(name="search", description="Search a shared source snapshot; return matching lines and nearby code plus object version. Scans shared memory, returns bounded text.",
         inputSchema=dict(type="object",properties={"object":{"type":"string"},"query":{"type":"string"}},
                          required=["object","query"],additionalProperties=False)),
    dict(name="objects", description="List shared CXL object IDs.",
         inputSchema=dict(type="object", properties={}, additionalProperties=False)),
    dict(name="read", description="Read a shared object version. Returns bounded text plus version and producer.",
         inputSchema=dict(type="object", properties={"object": {"type": "string"}},
                          required=["object"], additionalProperties=False)),
    dict(name="write", description="Publish text with compare-and-swap. Read first; on version_conflict reread and merge. The lock covers validation and publication.",
         inputSchema=dict(type="object", properties={"object": {"type": "string"}, "text": {"type": "string"},
                         "expected_version": {"type": "integer", "minimum": 0}},
                         required=["object", "text", "expected_version"], additionalProperties=False)),
    dict(name="append", description="Atomically append one contribution to a shared object, preserving other agents' contributions.",
         inputSchema=dict(type="object", properties={"object": {"type": "string"}, "text": {"type": "string"}},
                          required=["object", "text"], additionalProperties=False)),
]


def dispatch(store, method, params):
    if method == "initialize":
        return dict(protocolVersion=params.get("protocolVersion", "2024-11-05"),
                    capabilities={"tools": {}}, serverInfo={"name": "damer-sharing", "version": "1.0"})
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            name, args = params["name"], params.get("arguments", {})
            if name == "objects":
                result = store.list_objects()
            elif name in {"read", "write", "append","search"}:
                result = store.access(args["object"], op="read" if name=="search" else name, text=args.get("text", ""),
                                      expected_version=args.get("expected_version"),
                                      query=args.get("query") if name=="search" else None,
                                      session_id=params.get("_meta", {}).get("ai.opencode/sessionID"))
            else:
                raise ValueError("unknown tool")
            return dict(content=[dict(type="text", text=json.dumps(result))],
                        isError=isinstance(result, dict) and "error" in result)
        except Exception as exc:
            return dict(content=[dict(type="text", text=str(exc))], isError=True)
    raise ValueError("method not found")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--agent", required=True)
    a = p.parse_args()
    store = SandboxStore(a.store, a.agent, "opencode-live")
    try:
        for line in sys.stdin:
            try:
                req = json.loads(line)
                with (store.root/f"mcp-{store.agent}-{os.getpid()}.jsonl").open("a") as log:
                    log.write(json.dumps(dict(method=req.get("method"), received_ns=time.monotonic_ns()))+"\n")
                if "id" not in req:
                    continue
                try:
                    response = dict(jsonrpc="2.0", id=req["id"],
                                    result=dispatch(store, req["method"], req.get("params", {})))
                except ValueError as exc:
                    response = dict(jsonrpc="2.0", id=req["id"], error=dict(code=-32601, message=str(exc)))
                print(json.dumps(response), flush=True)
            except Exception as exc:
                print(str(exc), file=sys.stderr, flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
