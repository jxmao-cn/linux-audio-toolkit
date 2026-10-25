PYTHON ?= python3
PIP ?= $(PYTHON) -m pip

.PHONY: help test lint demo install uninstall build clean

help:
	@echo "airpodsd development targets"
	@echo "  make test       run the unit tests"
	@echo "  make lint       compile-check the sources (and run ruff if installed)"
	@echo "  make demo       show the mock UI"
	@echo "  make install    install for the current user + systemd user unit"
	@echo "  make uninstall  remove the user installation"
	@echo "  make build      build a wheel and sdist"
	@echo "  make clean      remove build artefacts"

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -t . -v

lint:
	$(PYTHON) -m compileall -q src tests
	@if command -v ruff >/dev/null 2>&1; then ruff check src tests; else echo "ruff not installed; skipping"; fi

demo:
	PYTHONPATH=src $(PYTHON) -m airpods --mock status

install:
	./install.sh

uninstall:
	./uninstall.sh

build:
	$(PIP) install --upgrade build
	$(PYTHON) -m build

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.py[co]' -delete
