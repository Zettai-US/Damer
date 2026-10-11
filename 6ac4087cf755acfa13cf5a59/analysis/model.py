#!/usr/bin/env python3
"""Reproduce the paper's analytical sensitivity figures, not hardware results.

Chart contract: explicit static PDF figures for a third-party OSDI manuscript.
(1) Capacity vs overlap: 21 overlap values x four branch counts; line chart.
(2) Aggregate service-demand ratio vs bytes per branch: 41 sizes x four scans.
(3) Expected exposed-stall saving vs useful-prefetch probability: 41 values x
    four lookahead intervals. These are hypothetical inputs, not observations.
Use grayscale plus distinct line styles; 3.34 x 2.45 inch column figures.
Raw CSVs preserve parameters, denominators and adjacent measures. QA in main.pdf.
"""
from pathlib import Path
import csv
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'analysis'
FIG = ROOT / 'figures'
MiB = 2**20
GiB = 2**30
BP = 16 * GiB
BD = 64 * GiB
COMPILE = 50e-6
MAP = 5e-6

plt.rcParams.update({
    'font.family': 'serif', 'font.serif': ['Liberation Serif', 'DejaVu Serif'],
    'font.size': 9, 'axes.labelsize': 9, 'axes.titlesize': 9,
    'legend.fontsize': 8, 'xtick.labelsize': 8, 'ytick.labelsize': 8,
    'pdf.fonttype': 42, 'ps.fonttype': 42,
    'axes.spines.top': False, 'axes.spines.right': False,
    'axes.linewidth': .7, 'lines.linewidth': 1.3,
})
STYLES = ['-', '--', '-.', ':']
COLORS = ['0.1', '0.35', '0.1', '0.35']


def demand(n, size, rho, scans):
    """Sum of pool and DRAM service demands under an uncached-scan model."""
    copy_pool = n * size
    share_pool = n * size * ((1 - rho) + rho * scans)
    copy_dram = n * scans * size
    share_dram = n * scans * size * (1 - rho)
    overhead = COMPILE + n * MAP
    copy_t = copy_pool / BP + copy_dram / BD
    share_t = share_pool / BP + share_dram / BD + overhead
    return dict(branches=n, bytes_per_branch=size, overlap=rho, scans=scans,
                pool_bytes_copy=copy_pool, pool_bytes_share=share_pool,
                dram_bytes_copy=copy_dram, dram_bytes_share=share_dram,
                pool_bandwidth_Bps=BP, dram_bandwidth_Bps=BD,
                compile_s=COMPILE, map_s_per_branch=MAP,
                copy_service_s=copy_t, shared_service_s=share_t,
                shared_to_copy_ratio=share_t/copy_t)


def write_csv(name, rows):
    with (OUT / name).open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def finish(fig, ax, name):
    ax.grid(axis='y', color='0.88', linewidth=.45)
    ax.set_axisbelow(True)
    fig.tight_layout(pad=.5)
    fig.savefig(FIG / f'{name}.pdf', metadata={'Creator': 'analysis/model.py',
        'Title': f'{name}: analytical model, not hardware measurement'})
    fig.savefig(FIG / f'{name}.png', dpi=180)
    plt.close(fig)


def main():
    FIG.mkdir(exist_ok=True)
    rows = []
    fig, ax = plt.subplots(figsize=(3.34, 2.45))
    for n, style, color in zip([2, 4, 8, 16], STYLES, COLORS):
        overlap = np.linspace(0, 1, 21)
        fraction = overlap * (1 - 1/n)
        ax.plot(overlap, 100*fraction, style, color=color, label=f'{n} branches')
        for rho, saved in zip(overlap, fraction):
            rows.append(dict(branches=n, overlap=float(rho), bytes_per_branch=8*MiB,
                private_payload_bytes=n*8*MiB,
                shared_payload_bytes=(rho + n*(1-rho))*8*MiB,
                payload_saved_fraction=float(saved),
                provenance='closed-form capacity model'))
    ax.set(xlabel='Common fraction of each input', ylabel='Payload capacity saved (%)',
           xlim=(0, 1), ylim=(0, 100), title='Sharing capacity (analytical)')
    ax.legend(loc='upper left', frameon=False)
    finish(fig, ax, 'capacity')
    write_csv('capacity.csv', rows)

    rows = []
    fig, ax = plt.subplots(figsize=(3.34, 2.45))
    sizes = np.geomspace(64*1024, 64*MiB, 41)
    for scans, style, color in zip([1, 2, 4, 8], STYLES, COLORS):
        values = [demand(8, float(size), .75, scans) for size in sizes]
        rows.extend(values)
        ax.plot(sizes/MiB, [v['shared_to_copy_ratio'] for v in values],
                style, color=color, label=f'{scans} scan'+('' if scans == 1 else 's'))
    ax.axhline(1, color='0.55', linewidth=.8)
    ax.set_xscale('log', base=2)
    ax.set_xticks([.0625, .25, 1, 4, 16, 64], ['1/16', '1/4', '1', '4', '16', '64'])
    ax.set(xlabel='Input per branch (MiB)', ylabel='Shared / copied service demand',
           xlim=(.0625, 64), ylim=(0, 4.5), title='Service demand (analytical)')
    ax.legend(loc='upper right', ncol=2, frameon=False)
    finish(fig, ax, 'service')
    write_csv('service.csv', rows)

    rows = []
    fig, ax = plt.subplots(figsize=(3.34, 2.45))
    size = 8 * MiB
    duration = size / BP
    for lookahead, style, color in zip([0, .25, .5, 1], STYLES, COLORS):
        probabilities = np.linspace(0, 1, 41)
        saving = probabilities * min(lookahead, 1) - .2
        ax.plot(probabilities, saving, style, color=color, label=f'L/d = {lookahead:g}')
        for p, saved in zip(probabilities, saving):
            rows.append(dict(use_probability=float(p), lookahead_over_transfer=lookahead,
                bytes_prefetched=size, pool_bandwidth_Bps=BP,
                transfer_duration_s=duration, control_cost_s=.2*duration,
                expected_unused_bytes=(1-p)*size,
                expected_exposed_stall_saving_s=float(saved)*duration,
                normalized_stall_saving=float(saved),
                provenance='isolated speculative-transfer model'))
    ax.axhline(0, color='0.55', linewidth=.8)
    ax.set(xlabel='Probability that prefetched input is used',
           ylabel='Expected stall saving / transfer time',
           xlim=(0, 1), ylim=(-.25, .9), title='Lookahead value (analytical)')
    ax.legend(loc='upper left', frameon=False)
    finish(fig, ax, 'prefetch')
    write_csv('prefetch.csv', rows)

    summary = {
        'provenance': 'deterministic analytical examples; no hardware measurements',
        'capacity_8_branches_75pct_overlap': {'private_MiB': 64, 'shared_MiB': 22,
                                            'saved_percent': 65.625},
        'one_scan': demand(8, 8*MiB, .75, 1),
        'four_scans': demand(8, 8*MiB, .75, 4),
        'rows': {'capacity': 84, 'service': 164, 'prefetch': 164},
    }
    (OUT/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
