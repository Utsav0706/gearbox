# For Linux, CI and Docker. On Windows (no make), run the commands directly:
#   py -m ruff check . ; py -m ruff format --check . ; py -m pytest
.PHONY: install lint format test check

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
