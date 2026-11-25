.PHONY: setup pipeline serve test chaos benchmark lint typecheck audit sbom licenses clean

PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest

setup:
	uv sync --python 3.12 --extra dev

pipeline:
	.venv/bin/geo-climate run --compact

serve:
	.venv/bin/geo-climate serve --bootstrap --host 127.0.0.1 --port 8070

test:
	$(PYTEST) tests/unit tests/contracts tests/integration tests/e2e tests/performance

chaos:
	$(PYTEST) tests/chaos -v

benchmark:
	$(PYTHON) benchmarks/run_benchmarks.py

lint:
	.venv/bin/ruff check .

typecheck:
	.venv/bin/mypy geo_climate ingestion processing models services

audit:
	$(PYTHON) scripts/static_audit.py

sbom:
	$(PYTHON) scripts/generate_sbom.py

licenses:
	$(PYTHON) scripts/check_licenses.py

all-checks: lint typecheck audit licenses sbom test chaos

clean:
	rm -rf .local_data .pytest_cache .mypy_cache .ruff_cache .hypothesis
