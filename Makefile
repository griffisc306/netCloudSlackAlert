PYTHON ?= python3
EVENT ?= events/console-test-event.json
LIVE_URL ?= https://khgcza01c8.execute-api.us-east-1.amazonaws.com/Prod/webhook
LIVE_PAYLOAD ?= /tmp/netcloud-live-payload.json

.PHONY: install-dev test live-send
install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) scripts/invoke_lambda.py $(EVENT)

live-send:
	$(PYTHON) -c 'import json, pathlib; event = json.load(open("$(EVENT)", "r", encoding="utf-8")); pathlib.Path("$(LIVE_PAYLOAD)").write_text(event["body"], encoding="utf-8")'
	curl -sS -X POST "$(LIVE_URL)" -H 'Content-Type: application/json' --data-binary @"$(LIVE_PAYLOAD)"
	rm -f "$(LIVE_PAYLOAD)"
