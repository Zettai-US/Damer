# Makefile for LaTeX compilation

# Main document (without .tex extension)
MAIN = main

# LaTeX compiler
LATEX = pdflatex -interaction=nonstopmode -halt-on-error
BIBTEX = bibtex

# All source files
TEXFILES = $(wildcard *.tex)
BIBFILES = $(wildcard *.bib)
STYLEFILES = $(wildcard *.sty)
MODEL_FIGURES = figures/capacity.pdf figures/service.pdf figures/prefetch.pdf
MEASURED_FIGURES = figures/measured_fanout.pdf figures/measured_phases.pdf figures/measured_compiler.pdf
PROFILE_FIGURES = figures/profile-observed-dataflow.pdf figures/profile-sharing-profile.pdf
PROFILE_METRICS = experiments/results/shared_state_profile/metrics.tex
DIAGRAMS = figures/sharing.pdf figures/architecture.pdf figures/lowering.pdf figures/epoch.pdf
DIAGRAM_PREVIEWS = $(DIAGRAMS:.pdf=.png)
FIGURES = $(MEASURED_FIGURES) $(DIAGRAMS) $(PROFILE_FIGURES)
RESULTS = experiments/results/fanout.jsonl experiments/results/phases.jsonl experiments/results/compiler.jsonl

.PHONY: all model bench controls plots diagrams profile check clean distclean

all: $(MAIN).pdf

$(MAIN).pdf: $(MAIN).tex $(TEXFILES) $(BIBFILES) $(STYLEFILES) $(FIGURES) $(PROFILE_METRICS)
	$(LATEX) $(MAIN)
	$(BIBTEX) $(MAIN)
	$(LATEX) $(MAIN)
	$(LATEX) $(MAIN)

$(MODEL_FIGURES) &: analysis/model.py
	python3 analysis/model.py

$(MEASURED_FIGURES) &: experiments/summarize.py $(RESULTS)
	python3 experiments/summarize.py

$(DIAGRAMS): figures/%.pdf: figures/%.tex figures/diagram-style.tex
	$(LATEX) -output-directory=figures $<

$(DIAGRAM_PREVIEWS): figures/%.png: figures/%.pdf
	pdftoppm -r 180 -png -singlefile $< $(basename $@)

diagrams: $(DIAGRAMS) $(DIAGRAM_PREVIEWS)

model:
	python3 analysis/model.py

bench:
	python3 experiments/run_native.py
	python3 experiments/bench_compiler.py
	python3 experiments/summarize.py

controls:
	python3 experiments/test_runtime.py

plots:
	python3 experiments/summarize.py

# Re-analyze archived live traces; this does not launch agents or the model.
profile:
	python3 ../workloads/agent_sharing/report_profile.py ../workloads/agent_sharing/results/arvm-profile-20261011 --probe ../workloads/agent_sharing/results/arvm-sharing-probe-20261011
	python3 ../workloads/agent_sharing/export_profile_paper.py ../workloads/agent_sharing/results/arvm-profile-20261011 --paper .

check: $(MAIN).pdf
	@if [ ! -f "$(MAIN).aux" ] || [ ! -f "$(MAIN).log" ]; then \
		$(LATEX) $(MAIN) && $(BIBTEX) $(MAIN) && $(LATEX) $(MAIN) && $(LATEX) $(MAIN); \
	fi
	python3 analysis/check_format.py

clean:
	rm -f *.aux *.log *.bbl *.blg *.out *.toc *.lof *.lot *.fls *.fdb_latexmk *.synctex.gz comment.cut

distclean: clean
	rm -f $(MAIN).pdf
