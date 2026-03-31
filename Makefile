PYTHON ?= python3
EVENT ?= events/console-test-event.json

.PHONY: install-dev test
install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) scripts/invoke_lambda.py $(EVENT)
