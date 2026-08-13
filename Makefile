.PHONY: help install test check clean

PYTHON ?= python3

help:
	@echo "install  Install DevFreeze in editable mode"
	@echo "test     Run the unittest suite"
	@echo "check    Compile sources and run tests"
	@echo "clean    Remove generated Python caches and build output"

install:
	$(PYTHON) -m pip install -e .

test:
	$(PYTHON) -m unittest discover -s tests -v

check:
	$(PYTHON) -m compileall -q src tests
	$(PYTHON) -m unittest discover -s tests -v

clean:
	find src tests -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf build dist
