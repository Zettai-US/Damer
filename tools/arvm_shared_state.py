#!/usr/bin/env python3
"""Analyze observed OpenCode memory effects into arVM versioned dataflow."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"workloads/agent_sharing"))
from dataflow import analyze_run

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("episode",type=Path)
    a=p.parse_args()
    r=analyze_run(a.episode)
    print(json.dumps(dict(metrics=r["metrics"],grant_path=r["grant_path"]),indent=2))
