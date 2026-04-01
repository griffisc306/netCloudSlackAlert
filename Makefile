PYTHON ?= python3
EVENT ?= events/console-test-event.json
LIVE_URL ?= https://khgcza01c8.execute-api.us-east-1.amazonaws.com/Prod/webhook
LIVE_PAYLOAD ?= /tmp/netcloud-live-payload.json
LIVE_CHANNEL_ID ?=

.PHONY: install-dev test live-send
install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) scripts/invoke_lambda.py $(EVENT)

live-send:
	LIVE_CHANNEL_ID="$(LIVE_CHANNEL_ID)" $(PYTHON) -c 'import json, os, pathlib; event = json.load(open("$(EVENT)", "r", encoding="utf-8")); body = event["body"]; body = json.loads(body) if isinstance(body, str) else body; channel = os.environ.get("LIVE_CHANNEL_ID", "").strip(); body = dict(body); body["slack_channel_id_override"] = channel if channel else body.get("slack_channel_id_override"); pathlib.Path("$(LIVE_PAYLOAD)").write_text(json.dumps(body), encoding="utf-8")'
	curl -sS -X POST "$(LIVE_URL)" -H 'Content-Type: application/json' --data-binary @"$(LIVE_PAYLOAD)"
	rm -f "$(LIVE_PAYLOAD)"
