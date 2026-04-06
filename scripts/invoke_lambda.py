import argparse
import importlib
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class DummyResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"ok": True}

    def json(self):
        return self._payload


class DummyS3Client:
    def put_object(self, **kwargs):
        return None

    def generate_presigned_url(self, **kwargs):
        return "https://example.com/test-image"


class DummyDynamoDBClient:
    def __init__(self):
        self.items = []

    def put_item(self, **kwargs):
        item = kwargs.get("Item") or {}
        self.items.append(item)
        return {}

    def query(self, **kwargs):
        return {"Items": []}

    def batch_write_item(self, **kwargs):
        return {"UnprocessedItems": {}}


def fake_boto3_client(service_name, *args, **kwargs):
    if service_name == "s3":
        return DummyS3Client()
    if service_name == "dynamodb":
        return DummyDynamoDBClient()
    raise ValueError(f"Unsupported mocked boto3 client: {service_name}")


def fake_http_post_json(url, payload, headers=None, timeout=15):
    if "chat.postMessage" in url:
        return DummyResponse(200, {"ok": True, "ts": "12345.6789"})
    return DummyResponse(200, {"ok": True})


def fake_http_post_form(url, form_data, headers=None, timeout=15):
    if "files.getUploadURLExternal" in url:
        return DummyResponse(200, {
            "ok": True,
            "upload_url": "https://example.com/slack-upload",
            "file_id": "F123456",
        })
    if "files.completeUploadExternal" in url:
        return DummyResponse(200, {
            "ok": True,
            "files": [{
                "id": "F123456",
                "url_private": "https://files.slack.com/files-pri/T123-F123456/test.png",
                "permalink": "https://slack-files.com/T123-F123456-test",
            }],
        })
    return DummyResponse(200, {"ok": True})


def fake_http_post_bytes(url, content, content_type, timeout=30):
    return DummyResponse(200, {"ok": True})


def main():
    parser = argparse.ArgumentParser(
        description="Invoke lambda_handler with a saved Lambda-style test event."
    )
    parser.add_argument("event_path", help="Path to the JSON event file")
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Use real outbound network calls instead of mocked Slack/S3 calls",
    )
    parser.add_argument(
        "--channel-id",
        help="Override the Slack channel ID in the JSON request body for bot-token routes",
    )
    args = parser.parse_args()

    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("SLACK_URL_CRADLEPOINT", "https://example.com/slack/cradlepoint")
    os.environ.setdefault("SLACK_URL_CAM_MON", "https://example.com/slack/cam-mon")
    os.environ.setdefault("SLACK_BOT_TOKEN_CAM_MON", "xoxb-test-cam-mon")
    os.environ.setdefault("SLACK_CHANNEL_ID_CAM_MON", "C123CAMMON")
    os.environ.setdefault("S3_BUCKET_NAME", "test-bucket")
    os.environ.setdefault("PRESIGNED_URL_EXPIRES", "3600")

    with open(args.event_path, "r", encoding="utf-8") as event_file:
        event = json.load(event_file)

    if args.channel_id:
        body = event.get("body")
        if isinstance(body, str):
            parsed_body = json.loads(body)
        elif isinstance(body, dict):
            parsed_body = dict(body)
        else:
            parsed_body = {}
        parsed_body["slack_channel_id_override"] = args.channel_id
        event["body"] = json.dumps(parsed_body)

    with mock.patch("boto3.client", side_effect=fake_boto3_client):
        lambda_module = importlib.import_module("lambda_function")

    with ExitStack() as stack:
        if not args.allow_network:
            stack.enter_context(mock.patch.object(lambda_module, "s3", DummyS3Client()))
            stack.enter_context(mock.patch.object(lambda_module, "http_post_json", side_effect=fake_http_post_json))
            stack.enter_context(mock.patch.object(lambda_module, "http_post_form", side_effect=fake_http_post_form))
            stack.enter_context(mock.patch.object(lambda_module, "http_post_bytes", side_effect=fake_http_post_bytes))

        result = lambda_module.lambda_handler(event, None)

    print(json.dumps(result, indent=2))

    if result.get("statusCode", 500) >= 400:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
