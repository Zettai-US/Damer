arVM figures
============

Mechanism figures are original, editable TikZ drawings. Their organization
uses the supplied SkVM v2 reference: muted module colors, separate compiler
and runtime regions, explicit transform stages, and a worked plan example.
Reference: https://arxiv.org/html/2604.03088v2 (Figures 5 and 7).
No source-paper artwork, experimental values, or system components are copied.

Build all vector PDFs and PNG previews: make diagrams
Build the complete paper: make
Validate the 12-page body and typography: make check

sharing.tex / .pdf / .png
  Figure 1: private payload copies versus shared immutable CXL backing.
  This is a conceptual capacity diagram, not a measured latency chart.
architecture.tex / .pdf / .png
  Figure 2: accepted plan and registry facts -> compiler passes -> epoch
  contract -> runtime admission and shared memory; feedback updates epochs.
lowering.tex / .pdf / .png
  Figure 3: one concrete plan, object versions on graph edges, and the memory
  arrangement. Dashed arrows retain the passage version for synthesis.
epoch.tex / .pdf / .png
  Figure 4: candidate construction, atomic validation/admission, execution,
  and stale-candidate retry. The full design includes optional movement policy.
diagram-style.tex
  Common fonts, colors, line weights, arrows, and node styles.

The main paper includes the vector PDFs. Labels and line styles carry meaning
without depending on color. Text is embedded and selectable in the PDFs.

The measured_fanout, measured_phases, and measured_compiler figures are built
by experiments/summarize.py from the saved observations. This revision keeps
their data, scales, and uncertainty definitions unchanged. See
experiments/README.txt for workload and measurement scope.

capacity, service, and prefetch are analytical companion plots, generated
by analysis/model.py and not used as measured evidence in the manuscript.
