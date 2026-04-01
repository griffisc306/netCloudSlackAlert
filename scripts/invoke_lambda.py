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
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "ok": True,
            "upload_url": "https://example.com/slack-upload",
            "file_id": "F123456",
        }


class DummyS3Client:
    def put_object(self, **kwargs):
        return None

    def generate_presigned_url(self, **kwargs):
        return "https://example.com/test-image"


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

    with mock.patch("boto3.client", return_value=DummyS3Client()):
        lambda_module = importlib.import_module("lambda_function")

    with ExitStack() as stack:
        if not args.allow_network:
            stack.enter_context(
                mock.patch.object(lambda_module.requests, "post", return_value=DummyResponse())
            )
            stack.enter_context(mock.patch.object(lambda_module, "s3", DummyS3Client()))

        result = lambda_module.lambda_handler(event, None)

    print(json.dumps(result, indent=2))

    if result.get("statusCode", 500) >= 400:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
