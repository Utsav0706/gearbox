# For Linux, CI and Docker. On Windows (no make), run the commands directly:
#   py -m ruff check . ; py -m ruff format --check . ; py -m pytest ; py -m gearbox.eval
.PHONY: install lint format test check eval eval-live dashboard

install:
	pip install -e ".[dev]"

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

test:
	pytest

check: lint test

# The headline numbers, replayed from stored answers and judge scores: free, same
# numbers every run. Writes reports/eval.md, eval.json and cost_quality.svg.
eval:
	python -m gearbox.eval

# Test prompts through the running API (port 8002), judged by Dyno (DYNO_BIN: Dyno's
# own `dyno` command). 30 prompts cost ~GBP 0.12; without YES=1 it only prints the estimate.
eval-live:
	python -m gearbox.eval.dyno run --n $(or $(N),30) $(if $(YES),--yes,)

dashboard:
	python -m streamlit run src/gearbox/dashboard/app.py --server.port 8502
