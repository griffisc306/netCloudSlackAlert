import os
import json
import time
import uuid
import re
import boto3 # pyright: ignore[reportMissingImports]
import logging
import base64
from datetime import datetime
from urllib import error, parse, request
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from pytz import timezone as ZoneInfo # pyright: ignore[reportMissingModuleSource]

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

SLACK_URL_CRADLEPOINT = os.environ["SLACK_URL_CRADLEPOINT"]
SLACK_URL_CAM_MON = os.environ["SLACK_URL_CAM_MON"]
SLACK_BOT_TOKEN_CAM_MON = os.environ.get("SLACK_BOT_TOKEN_CAM_MON")
SLACK_CHANNEL_ID_CAM_MON = os.environ.get("SLACK_CHANNEL_ID_CAM_MON")
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
PRESIGNED_URL_EXPIRES = int(os.environ.get("PRESIGNED_URL_EXPIRES", "3600"))

HEADERS = {"Content-Type": "application/json"}
SLACK_POST_RETRY_DELAY_SECONDS = float(os.environ.get("SLACK_POST_RETRY_DELAY_SECONDS", "1.0"))
SLACK_POST_MAX_ATTEMPTS = int(os.environ.get("SLACK_POST_MAX_ATTEMPTS", "3"))
DEFAULT_HTTP_TIMEOUT_SECONDS = 15
UPLOAD_HTTP_TIMEOUT_SECONDS = 30


class HTTPRequestError(Exception):
    pass


class HTTPResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.text = body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


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


def select_slack_route(source):
    route_map = {
        "cradlepoint": {
            "webhook_url": SLACK_URL_CRADLEPOINT,
            "bot_token": None,
            "channel_id": None,
        },
        "cam_mon": {
            "webhook_url": SLACK_URL_CAM_MON,
            "bot_token": SLACK_BOT_TOKEN_CAM_MON,
            "channel_id": SLACK_CHANNEL_ID_CAM_MON,
        },
    }
    return route_map[source]


def with_channel_override(route, channel_id_override):
    if not channel_id_override:
        return route

    updated_route = dict(route)
    updated_route["channel_id"] = channel_id_override
    return updated_route


def format_message_for_slack(message):
    return (message or "[No message supplied]").strip()


def build_basic_slack_payload(title, timestamp, message, fallback_text):
    return {
        "text": fallback_text,
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": title,
                    "emoji": True,
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Time:* {timestamp}",
                    }
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": format_message_for_slack(message),
                }
            }
        ]
    }


def http_post(url, *, headers=None, data=None, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS):
    req = request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return HTTPResponse(response.getcode(), response.read())
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HTTPRequestError(f"HTTP {exc.code} from {url}: {body}") from exc
    except error.URLError as exc:
        raise HTTPRequestError(f"Request failed for {url}: {exc.reason}") from exc


def http_post_json(url, payload, headers=None, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS):
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(payload).encode("utf-8")
    return http_post(url, headers=request_headers, data=data, timeout=timeout)


def http_post_form(url, form_data, headers=None, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS):
    request_headers = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    data = parse.urlencode(form_data).encode("utf-8")
    return http_post(url, headers=request_headers, data=data, timeout=timeout)


def http_post_bytes(url, content, content_type, timeout=UPLOAD_HTTP_TIMEOUT_SECONDS):
    request_headers = {"Content-Type": content_type}
    return http_post(url, headers=request_headers, data=content, timeout=timeout)


def upload_image_to_s3(file_obj, source):
    filename = file_obj["filename"].replace("/", "_")
    key = f"webhook-images/{source}/{int(time.time())}-{uuid.uuid4().hex}-{filename}"

    logger.info(
        "Uploading image to S3 bucket=%s key=%s content_type=%s bytes=%d",
        S3_BUCKET_NAME,
        key,
        file_obj["content_type"],
        len(file_obj["content"]),
    )

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

    logger.info(
        "Generated presigned image URL for Slack bucket=%s key=%s expires=%d url=%s",
        S3_BUCKET_NAME,
        key,
        PRESIGNED_URL_EXPIRES,
        url,
    )

    return {
        "filename": filename,
        "content_type": file_obj["content_type"],
        "url": url,
    }


def build_image_attachment(filename, content_type, content):
    if not content:
        return None

    return {
        "filename": (filename or "image").replace("/", "_"),
        "content_type": content_type or "application/octet-stream",
        "content": content,
    }


def route_supports_direct_slack_upload(route):
    return bool(route.get("bot_token") and route.get("channel_id"))


def decode_base64_image(base64_data, source, filename):
    if not base64_data:
        return None

    try:
        return build_image_attachment(
            filename,
            "image/png",
            base64.b64decode(base64_data),
        )
    except Exception:
        logger.exception("Failed to decode base64 image for source=%s", source)
        return None


def prepare_webhook_images(images, source):
    webhook_images = []

    for image in images:
        if image.get("url"):
            webhook_images.append(image)
            continue

        if not image.get("content"):
            continue

        uploaded = upload_image_to_s3(image, source)
        webhook_images.append({
            "filename": uploaded["filename"],
            "content_type": uploaded["content_type"],
            "url": uploaded["url"],
        })

    return webhook_images


def prepare_message_images(route, images, source):
    if route_supports_direct_slack_upload(route):
        direct_images = [image for image in images if image.get("content")]
        external_images = [image for image in images if image.get("url") and not image.get("content")]
        uploaded_images = upload_images_to_slack(route, direct_images)
        return uploaded_images + external_images

    return prepare_webhook_images(images, source)


def append_uploaded_images_to_payload(payload, uploaded_images):
    if not uploaded_images:
        return payload

    blocks = payload["blocks"]

    for image in uploaded_images:
        block = {
            "type": "image",
            "alt_text": image["filename"],
        }

        if image.get("slack_file_url"):
            block["slack_file"] = {"url": image["slack_file_url"]}
        elif image.get("slack_file_id"):
            block["slack_file"] = {"id": image["slack_file_id"]}
        else:
            block["image_url"] = image["url"]

        blocks.append(block)

    return payload


def count_slack_file_blocks(payload):
    blocks = payload.get("blocks") or []
    return sum(
        1
        for block in blocks
        if block.get("type") == "image" and block.get("slack_file")
    )


def count_image_url_blocks(payload):
    blocks = payload.get("blocks") or []
    return sum(
        1
        for block in blocks
        if block.get("type") == "image" and block.get("image_url")
    )


def build_slack_payload_from_json(body):
    source = normalize_source(body.get("source"))
    route = with_channel_override(
        select_slack_route(source),
        body.get("slack_channel_id_override"),
    )

    timestamp = format_time_to_eastern(body.get("timestamp"))
    text = body.get("text") or body.get("message") or "[No message supplied]"

    title = (
        "Camera Alert"
        if source == "cam_mon"
        else "Cradlepoint Alert"
    )

    payload = build_basic_slack_payload(title, timestamp, text, text)
    uploaded_images = []

    chart_image = decode_base64_image(
        body.get("chart_image_base64_png"),
        source,
        "chart.png",
    )
    if chart_image:
        uploaded_images.append(chart_image)

    image_url = body.get("image_url")
    if image_url:
        uploaded_images.append({
            "filename": "image",
            "content_type": "image/url",
            "url": image_url,
        })

    message_images = prepare_message_images(route, uploaded_images, source)
    append_uploaded_images_to_payload(payload, message_images)
    logger.info(
        "Built JSON Slack payload source=%s has_chart_image=%s has_image_url=%s image_blocks=%d slack_file_blocks=%d image_url_blocks=%d",
        source,
        bool(body.get("chart_image_base64_png")),
        bool(body.get("image_url")),
        len([block for block in payload.get("blocks", []) if block.get("type") == "image"]),
        count_slack_file_blocks(payload),
        count_image_url_blocks(payload),
    )

    return route, payload, uploaded_images


def build_slack_payload_from_cradlepoint_item(item, channel_id_override=None):
    source = "cradlepoint"
    route = with_channel_override(select_slack_route(source), channel_id_override)

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
    return route, payload, []


def build_slack_payload_from_multipart(fields, files):
    source = normalize_source(fields.get("source"))
    route = with_channel_override(route=select_slack_route(source), channel_id_override=fields.get("slack_channel_id_override"))

    message = fields.get("message") or fields.get("text") or "[No message supplied]"
    timestamp = format_time_to_eastern(fields.get("timestamp"))

    uploaded_images = [
        build_image_attachment(f["filename"], f["content_type"], f["content"])
        for f in files
        if f["field_name"] == "images"
    ]
    uploaded_images = [image for image in uploaded_images if image]

    if not uploaded_images:
        raise ValueError("No files found in 'images' field")

    title = (
        "Camera Alert"
        if source == "cam_mon"
        else "Alert"
    )

    payload = build_basic_slack_payload(title, timestamp, message, message)
    message_images = prepare_message_images(route, uploaded_images, source)
    append_uploaded_images_to_payload(payload, message_images)
    logger.info(
        "Built multipart Slack payload source=%s file_count=%d image_blocks=%d slack_file_blocks=%d image_url_blocks=%d",
        source,
        len(uploaded_images),
        len([block for block in payload.get("blocks", []) if block.get("type") == "image"]),
        count_slack_file_blocks(payload),
        count_image_url_blocks(payload),
    )

    return route, payload, uploaded_images


def post_to_slack(webhook_url, payload):
    return http_post_json(
        webhook_url,
        payload,
        headers=HEADERS,
        timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
    )


def post_message_to_slack(route, payload):
    request_payload = {
        "channel": route["channel_id"],
        **payload,
    }
    headers = {
        **slack_api_headers(route["bot_token"]),
        "Content-Type": "application/json; charset=utf-8",
    }

    for attempt in range(1, SLACK_POST_MAX_ATTEMPTS + 1):
        response = http_post_json(
            "https://slack.com/api/chat.postMessage",
            request_payload,
            headers=headers,
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        response_payload = response.json()
        if response_payload.get("ok"):
            return response

        response_metadata = response_payload.get("response_metadata") or {}
        detail_messages = response_metadata.get("messages") or []
        detail_suffix = f" details={detail_messages}" if detail_messages else ""
        error_message = response_payload.get("error", "unknown_error")
        invalid_slack_file = (
            error_message == "invalid_blocks"
            and any("invalid slack file" in message.lower() for message in detail_messages)
        )

        if invalid_slack_file and attempt < SLACK_POST_MAX_ATTEMPTS:
            logger.warning(
                "Slack file block not ready yet; retrying chat.postMessage attempt=%d/%d delay=%.1fs",
                attempt,
                SLACK_POST_MAX_ATTEMPTS,
                SLACK_POST_RETRY_DELAY_SECONDS,
            )
            time.sleep(SLACK_POST_RETRY_DELAY_SECONDS)
            continue

        raise HTTPRequestError(
            f"Slack chat.postMessage failed: {error_message}{detail_suffix}"
        )


def slack_api_headers(bot_token):
    return {
        "Authorization": f"Bearer {bot_token}",
    }


def post_file_to_upload_url(upload_url, image):
    return http_post_bytes(
        upload_url,
        image["content"],
        image["content_type"],
        timeout=UPLOAD_HTTP_TIMEOUT_SECONDS,
    )


def upload_image_to_slack(bot_token, image):
    metadata_response = http_post_form(
        "https://slack.com/api/files.getUploadURLExternal",
        {
            "filename": image["filename"],
            "length": len(image["content"]),
        },
        headers=slack_api_headers(bot_token),
        timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
    )
    metadata = metadata_response.json()
    if not metadata.get("ok"):
        raise HTTPRequestError(
            f"Slack file upload init failed: {metadata.get('error', 'unknown_error')}"
        )

    upload_url = metadata["upload_url"]
    file_id = metadata["file_id"]

    post_file_to_upload_url(upload_url, image)

    complete_response = http_post_form(
        "https://slack.com/api/files.completeUploadExternal",
        {
            "files": json.dumps([{
                "id": file_id,
                "title": image["filename"],
            }]),
        },
        headers=slack_api_headers(bot_token),
        timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
    )
    complete_payload = complete_response.json()
    if not complete_payload.get("ok"):
        raise HTTPRequestError(
            f"Slack file upload complete failed: {complete_payload.get('error', 'unknown_error')}"
        )

    file_info = None
    files = complete_payload.get("files")
    if isinstance(files, list) and files:
        file_info = files[0]

    slack_file_url = None
    if isinstance(file_info, dict):
        slack_file_url = file_info.get("url_private") or file_info.get("permalink")

    return {
        "filename": image["filename"],
        "content_type": image["content_type"],
        "slack_file_id": file_id,
        "slack_file_url": slack_file_url,
    }


def upload_images_to_slack(route, images):
    bot_token = route.get("bot_token")

    if not bot_token:
        logger.info("Skipping direct Slack upload because bot token is missing")
        return []

    uploaded_images = []
    for image in images:
        if not image.get("content"):
            continue
        uploaded_images.append(upload_image_to_slack(bot_token, image))

    return uploaded_images


def send_slack_message(route, payload):
    if route_supports_direct_slack_upload(route):
        return post_message_to_slack(route, payload)

    return post_to_slack(route["webhook_url"], payload)


def lambda_handler(event, context):
    try:
        logger.info("Raw event: %s", json.dumps(event))

        content_type = get_header(event, "Content-Type") or ""

        if "multipart/form-data" in content_type.lower():
            fields, files = parse_multipart_form(event)
            logger.info("Parsed multipart fields: %s", json.dumps(fields))
            logger.info("Parsed multipart file count: %d", len(files))
            route, payload, uploaded_images = build_slack_payload_from_multipart(fields, files)

        elif "application/json" in content_type.lower():
            body = parse_json_body(event)
            logger.info("Parsed JSON body: %s", json.dumps(body))

            if isinstance(body.get("data"), list) and body["data"]:
                route, payload, uploaded_images = build_slack_payload_from_cradlepoint_item(
                    body["data"][0],
                    body.get("slack_channel_id_override"),
                )
            else:
                route, payload, uploaded_images = build_slack_payload_from_json(body)

        else:
            raise ValueError(f"Unsupported Content-Type: {content_type}")

        logger.info(
            "Sending Slack message source_route=%s content_type=%s image_blocks=%d slack_file_blocks=%d image_url_blocks=%d",
            "cam_mon" if route.get("bot_token") else "cradlepoint",
            content_type,
            len([block for block in payload.get("blocks", []) if block.get("type") == "image"]),
            count_slack_file_blocks(payload),
            count_image_url_blocks(payload),
        )
        slack_response = send_slack_message(route, payload)
        slack_file_uploads = count_slack_file_blocks(payload)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Webhook processed successfully",
                "slack_status": slack_response.status_code,
                "slack_file_uploads": slack_file_uploads,
            })
        }

    except HTTPRequestError as e:
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
