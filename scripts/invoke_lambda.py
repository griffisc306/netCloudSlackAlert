import argparse
import importlib
import json
import os
from contextlib import ExitStack
from unittest import mock


class DummyResponse:
    status_code = 200

    def raise_for_status(self):
        return None


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
    args = parser.parse_args()

    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("SLACK_URL_CRADLEPOINT", "https://example.com/slack/cradlepoint")
    os.environ.setdefault("SLACK_URL_CAM_MON", "https://example.com/slack/cam-mon")
    os.environ.setdefault("S3_BUCKET_NAME", "test-bucket")
    os.environ.setdefault("PRESIGNED_URL_EXPIRES", "3600")

    with open(args.event_path, "r", encoding="utf-8") as event_file:
        event = json.load(event_file)

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
