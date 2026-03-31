import os
import json
import time
import uuid
import re
import boto3
import requests
import logging
import base64
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from pytz import timezone as ZoneInfo

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

SLACK_URL_CRADLEPOINT = os.environ["SLACK_URL_CRADLEPOINT"]
SLACK_URL_CAM_MON = os.environ["SLACK_URL_CAM_MON"]
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
PRESIGNED_URL_EXPIRES = int(os.environ.get("PRESIGNED_URL_EXPIRES", "3600"))

HEADERS = {"Content-Type": "application/json"}


def format_time_to_eastern(dt_str=None):
    try:
        if dt_str:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        else:
            dt = datetime.now(tz=ZoneInfo("UTC"))
    except Exception:
        dt = datetime.now(tz=ZoneInfo("UTC"))

    eastern = ZoneInfo("America/New_York")
    return dt.astimezone(eastern).strftime("%m/%d/%Y %I:%M %p")


def get_header(event, name):
    headers = event.get("headers") or {}
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def parse_json_body(event):
    body = event.get("body")
    if body is None:
        return {}

    if isinstance(body, dict):
        return body

    if event.get("isBase64Encoded", False):
        body = base64.b64decode(body).decode("utf-8")

    return json.loads(body)


def parse_multipart_form(event):
    content_type = get_header(event, "Content-Type")
    if not content_type:
        raise ValueError("Missing Content-Type header")

    match = re.search(r'boundary="?([^";]+)"?', content_type, re.IGNORECASE)
    if not match:
        raise ValueError("No boundary found in Content-Type header")

    boundary = match.group(1)

    body = event.get("body")
    if body is None:
        raise ValueError("Missing request body")

    if event.get("isBase64Encoded", False):
        body_bytes = base64.b64decode(body)
    else:
        body_bytes = body.encode("utf-8")

    boundary_bytes = ("--" + boundary).encode("utf-8")
    parts = body_bytes.split(boundary_bytes)

    fields = {}
    files = []

    for part in parts:
        if not part:
            continue

        part = part.strip(b"\r\n")

        if part == b"--":
            continue

        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue

        header_blob = part[:header_end].decode("utf-8", errors="ignore")
        content = part[header_end + 4:]

        disposition_match = re.search(
            r'Content-Disposition:\s*form-data;\s*name="([^"]+)"(?:;\s*filename="([^"]+)")?',
            header_blob,
            re.IGNORECASE
        )
        if not disposition_match:
            continue

        field_name = disposition_match.group(1)
        filename = disposition_match.group(2)

        content_type_match = re.search(
            r"Content-Type:\s*([^\r\n]+)",
            header_blob,
            re.IGNORECASE
        )
        part_content_type = (
            content_type_match.group(1).strip()
            if content_type_match
            else "application/octet-stream"
        )

        if filename:
            files.append({
                "field_name": field_name,
                "filename": filename,
                "content_type": part_content_type,
                "content": content.rstrip(b"\r\n"),
            })
        else:
            fields[field_name] = content.decode("utf-8", errors="ignore").rstrip("\r\n")

    return fields, files


def normalize_source(source):
    if not source:
        raise ValueError("No source found in payload")

    s = source.strip().lower()

    if s in {"cradlepoint", "netcloud", "netcloud manager"}:
        return "cradlepoint"

    if s in {"cam_mon", "camera", "camera monitor", "umci camera monitor"}:
        return "cam_mon"

    raise ValueError(f"Unsupported source: {source}")


def select_slack_url(source):
    route_map = {
        "cradlepoint": SLACK_URL_CRADLEPOINT,
        "cam_mon": SLACK_URL_CAM_MON,
    }
    return route_map[source]


def upload_image_to_s3(file_obj, source):
    filename = file_obj["filename"].replace("/", "_")
    key = f"webhook-images/{source}/{int(time.time())}-{uuid.uuid4().hex}-{filename}"

    s3.put_object(
        Bucket=S3_BUCKET_NAME,
        Key=key,
        Body=file_obj["content"],
        ContentType=file_obj["content_type"],
    )

    url = s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": S3_BUCKET_NAME, "Key": key},
        ExpiresIn=PRESIGNED_URL_EXPIRES,
    )

    return {
        "filename": filename,
        "content_type": file_obj["content_type"],
        "url": url,
    }


def build_slack_payload_from_json(body):
    source = normalize_source(body.get("source"))
    slack_url = select_slack_url(source)

    event_name = body.get("event") or body.get("alert_type") or "event"
    timestamp = format_time_to_eastern(body.get("timestamp"))
    text = body.get("text") or body.get("message") or "[No message supplied]"
    device_id = body.get("device_id") or "Unknown"

    title = (
        "*:camera: CAMERA ALERT :camera:*"
        if source == "cam_mon"
        else "*:rotating_light: CRADLEPOINT ALERT :rotating_light:*"
    )

    payload = {
        "text": f"{event_name} from {device_id}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{title}\n"
                        f"*Time*: {timestamp}\n"
                        f"*Source*: `{source}`\n"
                        f"*Event*: {event_name}\n"
                        f"*Device ID*: {device_id}\n"
                        f"*Message*: {text}"
                    )
                }
            }
        ]
    }

    return slack_url, payload


def build_slack_payload_from_cradlepoint_item(item):
    source = "cradlepoint"
    slack_url = select_slack_url(source)

    created_at = item.get("created_at") or item.get("detected_at")
    formatted_time = format_time_to_eastern(created_at)

    router_details = item.get("router_details", {})
    router_name = router_details.get("name")
    router_desc = router_details.get("description")
    router_mac = router_details.get("mac")
    device_id = item.get("device_id") or "Unknown"

    info = item.get("info")
    if info and isinstance(info, dict) and info.get("message"):
        message = info["message"]
    elif item.get("friendly_info"):
        message = item["friendly_info"]
    else:
        message = "[No message supplied]"

    alert_type = item.get("alert_type") or item.get("type") or "Unknown"

    device_info = []
    if router_name:
        device_info.append(router_name)
    if router_desc:
        device_info.append(f"({router_desc})")
    if router_mac:
        device_info.append(f"[MAC: {router_mac}]")
    if device_id:
        device_info.append(f"[Device ID: {device_id}]")

    lines = [
        "*:rotating_light: CRADLEPOINT ALERT :rotating_light:*",
        f"*Time*: {formatted_time}",
    ]

    if device_info:
        lines.append(f"*Device*: {' '.join(device_info)}")

    lines.append(f"*Message*: {message}")
    lines.append(f"*Alert Type*: {alert_type}")

    payload = {"text": "\n".join(lines)}
    return slack_url, payload


def build_slack_payload_from_multipart(fields, files):
    source = normalize_source(fields.get("source"))
    slack_url = select_slack_url(source)

    message = fields.get("message") or fields.get("text") or "[No message supplied]"
    alert_type = fields.get("alert_type") or fields.get("event") or "camera_event"
    device_id = fields.get("device_id") or "Unknown"
    timestamp = format_time_to_eastern(fields.get("timestamp"))

    uploaded_images = [
        upload_image_to_s3(f, source)
        for f in files
        if f["field_name"] == "images"
    ]

    if not uploaded_images:
        raise ValueError("No files found in 'images' field")

    title = (
        "*:camera: CAMERA ALERT :camera:*"
        if source == "cam_mon"
        else "*:rotating_light: ALERT :rotating_light:*"
    )

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{title}\n"
                    f"*Time*: {timestamp}\n"
                    f"*Source*: `{source}`\n"
                    f"*Device ID*: {device_id}\n"
                    f"*Alert Type*: {alert_type}\n"
                    f"*Message*: {message}"
                )
            }
        }
    ]

    for image in uploaded_images:
        blocks.append({
            "type": "image",
            "image_url": image["url"],
            "alt_text": image["filename"],
            "title": {
                "type": "plain_text",
                "text": image["filename"]
            }
        })

    payload = {
        "text": f"{alert_type} from {device_id}",
        "blocks": blocks
    }

    return slack_url, payload


def post_to_slack(webhook_url, payload):
    response = requests.post(
        webhook_url,
        json=payload,
        headers=HEADERS,
        timeout=15
    )
    response.raise_for_status()
    return response


def lambda_handler(event, context):
    try:
        logger.info("Raw event: %s", json.dumps(event))

        content_type = get_header(event, "Content-Type") or ""

        if "multipart/form-data" in content_type.lower():
            fields, files = parse_multipart_form(event)
            logger.info("Parsed multipart fields: %s", json.dumps(fields))
            logger.info("Parsed multipart file count: %d", len(files))
            slack_url, payload = build_slack_payload_from_multipart(fields, files)

        elif "application/json" in content_type.lower():
            body = parse_json_body(event)
            logger.info("Parsed JSON body: %s", json.dumps(body))

            if isinstance(body.get("data"), list) and body["data"]:
                slack_url, payload = build_slack_payload_from_cradlepoint_item(body["data"][0])
            else:
                slack_url, payload = build_slack_payload_from_json(body)

        else:
            raise ValueError(f"Unsupported Content-Type: {content_type}")

        slack_response = post_to_slack(slack_url, payload)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Webhook processed successfully",
                "slack_status": slack_response.status_code
            })
        }

    except requests.exceptions.RequestException as e:
        logger.exception("Slack request failed")
        return {
            "statusCode": 502,
            "body": json.dumps({"error": f"Slack request failed: {str(e)}"})
        }

    except Exception as e:
        logger.exception("Webhook processing failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }