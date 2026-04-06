import os
import json
import time
import uuid
import re
from collections import defaultdict
import boto3 # pyright: ignore[reportMissingImports]
import logging
import base64
from datetime import datetime, timedelta, timezone
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer # pyright: ignore[reportMissingImports]
from urllib import error, parse, request
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from pytz import timezone as ZoneInfo # pyright: ignore[reportMissingModuleSource]

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")
ddb_serializer = TypeSerializer()
ddb_deserializer = TypeDeserializer()

SLACK_URL_CRADLEPOINT = os.environ["SLACK_URL_CRADLEPOINT"]
SLACK_URL_CRADLEPOINT_TEST = os.environ.get("SLACK_URL_CRADLEPOINT_TEST")
SLACK_URL_CAM_MON = os.environ["SLACK_URL_CAM_MON"]
SLACK_BOT_TOKEN_CAM_MON = os.environ.get("SLACK_BOT_TOKEN_CAM_MON")
SLACK_CHANNEL_ID_CAM_MON = os.environ.get("SLACK_CHANNEL_ID_CAM_MON")
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
PRESIGNED_URL_EXPIRES = int(os.environ.get("PRESIGNED_URL_EXPIRES", "3600"))
DYNAMODB_ALERT_TABLE = os.environ.get("DYNAMODB_ALERT_TABLE", "netCloudSlackAlertEvents")
DYNAMODB_ALERT_GSI_NAME = os.environ.get("DYNAMODB_ALERT_GSI_NAME", "gsi1")
ALERT_RETENTION_DAYS = int(os.environ.get("ALERT_RETENTION_DAYS", "30"))

HEADERS = {"Content-Type": "application/json"}
SLACK_POST_RETRY_DELAY_SECONDS = float(os.environ.get("SLACK_POST_RETRY_DELAY_SECONDS", "1.5"))
SLACK_POST_MAX_ATTEMPTS = int(os.environ.get("SLACK_POST_MAX_ATTEMPTS", "5"))
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


def utc_now():
    return datetime.now(timezone.utc)


def parse_iso_datetime(dt_str=None):
    if not dt_str:
        return utc_now()

    try:
        parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return utc_now()


def format_time_to_utc(dt_str=None):
    return parse_iso_datetime(dt_str).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_time_to_eastern(dt_str=None):
    try:
        dt = parse_iso_datetime(dt_str)
    except Exception:
        dt = utc_now()

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

    if s == "umci camera monitor":
        return "umci camera monitor"

    raise ValueError(f"Unsupported source: {source}")


def get_request_path(event):
    if not isinstance(event, dict):
        return ""

    return (
        event.get("rawPath")
        or event.get("path")
        or ((event.get("requestContext") or {}).get("http") or {}).get("path")
        or ""
    )


def is_test_cradlepoint_route(event):
    path = get_request_path(event).strip().lower()
    if not path:
        return False

    return path.endswith("/test") or "cradlepoint-test" in path


def get_route_key(source, event=None):
    if source == "cradlepoint":
        return "test" if is_test_cradlepoint_route(event) else "prod"
    return "default"


def build_summary_bucket(source, route_key):
    return f"SOURCE#{source}#ROUTE#{route_key}"


def select_slack_route(source, event=None):
    route_map = {
        "cradlepoint": {
            "webhook_url": (
                SLACK_URL_CRADLEPOINT_TEST
                if is_test_cradlepoint_route(event) and SLACK_URL_CRADLEPOINT_TEST
                else SLACK_URL_CRADLEPOINT
            ),
            "bot_token": None,
            "channel_id": None,
        },
        "umci camera monitor": {
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


def resolve_alert_title(source, title=None, default_title="Alert"):
    if source == "umci camera monitor":
        return (title or "").strip() or "Camera Alert"

    return default_title


def is_scheduled_event(event):
    if not isinstance(event, dict):
        return False

    return (
        event.get("source") == "aws.events"
        or event.get("detail-type") == "Scheduled Event"
    )


def sanitize_payload_for_storage(payload):
    if isinstance(payload, dict):
        sanitized = {}
        for key, value in payload.items():
            if key == "chart_image_base64_png":
                sanitized[key] = "[omitted]"
            else:
                sanitized[key] = sanitize_payload_for_storage(value)
        return sanitized

    if isinstance(payload, list):
        return [sanitize_payload_for_storage(item) for item in payload]

    if isinstance(payload, bytes):
        return "[binary omitted]"

    return payload


def serialize_ddb_item(item):
    return {
        key: ddb_serializer.serialize(value)
        for key, value in item.items()
        if value is not None
    }


def deserialize_ddb_item(item):
    return {
        key: ddb_deserializer.deserialize(value)
        for key, value in item.items()
    }


def normalize_string(value, default="Unknown"):
    text = str(value).strip() if value is not None else ""
    return text or default


def build_cradlepoint_storage_record(item, event):
    source = "cradlepoint"
    route_key = get_route_key(source, event)
    detected_at = item.get("detected_at") or item.get("created_at")

    router_details = item.get("router_details") or {}
    info = item.get("info") or {}

    message = (
        info.get("message")
        if isinstance(info, dict) and info.get("message")
        else item.get("friendly_info") or "[No message supplied]"
    )

    device_name = (
        router_details.get("name")
        or item.get("device_name")
        or item.get("router_name")
        or "Unknown"
    )
    device_mac = (
        router_details.get("mac")
        or item.get("mac")
        or item.get("device_mac")
        or "Unknown"
    )

    return {
        "source": source,
        "route_key": route_key,
        "account_name": normalize_string(item.get("account_name") or item.get("account"), "Cradlepoint"),
        "alert_name": normalize_string(item.get("alert_type") or item.get("type"), "Unknown"),
        "device_name": normalize_string(device_name),
        "device_mac": normalize_string(device_mac),
        "router_id": normalize_string(item.get("router"), ""),
        "status": normalize_string(message, "[No message supplied]"),
        "detected_at": parse_iso_datetime(detected_at).isoformat(),
        "display_timestamp": format_time_to_eastern(detected_at),
        "raw_payload": sanitize_payload_for_storage(item),
    }


def build_camera_storage_record(body, event=None, fields=None, files=None):
    payload = body if body is not None else fields or {}
    source = "umci camera monitor"
    route_key = get_route_key(source, event)
    timestamp = payload.get("timestamp")

    file_count = 0
    if files:
        file_count = sum(1 for file_item in files if file_item.get("field_name") == "images")

    raw_payload = sanitize_payload_for_storage(payload)
    if files:
        raw_payload = {
            **raw_payload,
            "files": [
                {
                    "field_name": file_item.get("field_name"),
                    "filename": file_item.get("filename"),
                    "content_type": file_item.get("content_type"),
                }
                for file_item in files
            ],
        }

    return {
        "source": source,
        "route_key": route_key,
        "account_name": "UMCI Camera Monitor",
        "alert_name": normalize_string(
            payload.get("title") or payload.get("event") or "Camera Alert",
            "Camera Alert",
        ),
        "device_name": normalize_string(payload.get("device_name"), "UMCI Camera Monitor"),
        "device_mac": normalize_string(payload.get("device_mac")),
        "status": normalize_string(payload.get("text") or payload.get("message"), "[No message supplied]"),
        "detected_at": parse_iso_datetime(timestamp).isoformat(),
        "display_timestamp": format_time_to_eastern(timestamp),
        "file_count": file_count,
        "raw_payload": raw_payload,
    }


def persist_alert_records(records):
    stored = 0

    for record in records:
        detected_dt = parse_iso_datetime(record.get("detected_at"))
        expires_at = int((detected_dt + timedelta(days=ALERT_RETENTION_DAYS)).timestamp())
        item = {
            "pk": f"ALERT#{record['source']}#{record['route_key']}",
            "sk": f"{detected_dt.isoformat()}#{uuid.uuid4().hex}",
            "gsi1pk": build_summary_bucket(record["source"], record["route_key"]),
            "gsi1sk": detected_dt.isoformat(),
            "source": record["source"],
            "route_key": record["route_key"],
            "account_name": record.get("account_name") or "Unknown",
            "alert_name": record.get("alert_name") or "Unknown",
            "device_name": record.get("device_name") or "Unknown",
            "device_mac": record.get("device_mac") or "Unknown",
            "router_id": record.get("router_id") or "",
            "status": record.get("status") or "[No message supplied]",
            "display_timestamp": record.get("display_timestamp") or format_time_to_eastern(detected_dt.isoformat()),
            "detected_at": detected_dt.isoformat(),
            "raw_payload_json": json.dumps(record.get("raw_payload") or {}, default=str),
            "expires_at": expires_at,
        }

        if record.get("file_count") is not None:
            item["file_count"] = int(record["file_count"])

        dynamodb.put_item(
            TableName=DYNAMODB_ALERT_TABLE,
            Item=serialize_ddb_item(item),
        )
        stored += 1

    return stored


def query_summary_records(source, route_key, start_dt, end_dt):
    response = dynamodb.query(
        TableName=DYNAMODB_ALERT_TABLE,
        IndexName=DYNAMODB_ALERT_GSI_NAME,
        KeyConditionExpression="gsi1pk = :bucket AND gsi1sk BETWEEN :start AND :end",
        ExpressionAttributeValues=serialize_ddb_item({
            ":bucket": build_summary_bucket(source, route_key),
            ":start": start_dt.isoformat(),
            ":end": end_dt.isoformat(),
        }),
    )

    return [deserialize_ddb_item(item) for item in response.get("Items", [])]


def delete_alert_records(records):
    if not records:
        return 0

    deleted = 0
    batches = []

    for record in records:
        pk = record.get("pk")
        sk = record.get("sk")
        if not pk or not sk:
            continue

        batches.append({
            "DeleteRequest": {
                "Key": serialize_ddb_item({
                    "pk": pk,
                    "sk": sk,
                })
            }
        })

    for index in range(0, len(batches), 25):
        request_items = {
            DYNAMODB_ALERT_TABLE: batches[index:index + 25]
        }
        response = dynamodb.batch_write_item(RequestItems=request_items)
        unprocessed = response.get("UnprocessedItems", {})
        deleted += len(request_items[DYNAMODB_ALERT_TABLE]) - len(unprocessed.get(DYNAMODB_ALERT_TABLE, []))

    return deleted


def summarize_records_by_alert(records):
    grouped = defaultdict(lambda: {"count": 0, "devices": set()})

    for record in records:
        key = record.get("alert_name") or "Unknown"
        grouped[key]["count"] += 1
        device_identifier = record.get("device_mac") or record.get("device_name") or "Unknown"
        grouped[key]["devices"].add(device_identifier)

    rows = []
    for alert_name in sorted(grouped):
        summary = grouped[alert_name]
        rows.append({
            "alert_name": alert_name,
            "alert_count": str(summary["count"]),
            "affected_devices": str(len(summary["devices"])),
        })

    return rows


def truncate_text(value, max_length):
    value = value or ""
    if len(value) <= max_length:
        return value
    return value[: max_length - 3] + "..."


def titleize_alert_name(value):
    value = normalize_string(value)
    return value.replace("_", " ").title()


def build_cradlepoint_device_url(router_id):
    router_id = str(router_id or "").strip()
    if not router_id:
        return None
    return f"https://www.cradlepointecm.com/#/devices/routers/router/{router_id}/home/summary"


def format_slack_link(label, url):
    if not url:
        return label
    return f"<{url}|{label}>"


def format_table(headers, rows):
    if not rows:
        return "_No alerts in this window_"

    widths = [len(header) for header in headers]
    normalized_rows = []

    for row in rows:
        normalized = [str(cell) for cell in row]
        normalized_rows.append(normalized)
        for index, cell in enumerate(normalized):
            widths[index] = max(widths[index], len(cell))

    def format_row(row):
        return " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    divider = "-+-".join("-" * width for width in widths)
    table_lines = [format_row(headers), divider]
    table_lines.extend(format_row(row) for row in normalized_rows)
    return "```" + "\n".join(table_lines) + "```"


def summarize_record_details(records, limit=15):
    sorted_records = sorted(records, key=lambda record: record.get("detected_at", ""))
    lines = []

    for record in sorted_records[:limit]:
        device_url = build_cradlepoint_device_url(record.get("router_id"))
        device_name = format_slack_link(
            truncate_text(record.get("device_name", "Unknown"), 24),
            device_url,
        )
        lines.append(
            f"*Alert Name:* {titleize_alert_name(record.get('alert_name', 'Unknown'))}\n"
            f"*MAC Address:* {truncate_text(record.get('device_mac', 'Unknown'), 17)}\n"
            f"*Device Name:* {device_name}\n"
            f"*Status:* {truncate_text(record.get('status', '[No message supplied]'), 72)}\n"
            f"*Timestamp:* {truncate_text(record.get('display_timestamp', ''), 22)}"
        )

    remaining = len(sorted_records) - limit
    if not lines:
        return "_No alert details in this window_"

    if remaining > 0:
        lines.append(f"_...and {remaining} more alerts_")

    return "\n\n".join(lines)


def select_summary_route(source, route_key):
    if source == "cradlepoint":
        return {
            "webhook_url": (
                SLACK_URL_CRADLEPOINT_TEST
                if route_key == "test" and SLACK_URL_CRADLEPOINT_TEST
                else SLACK_URL_CRADLEPOINT
            ),
            "bot_token": None,
            "channel_id": None,
        }

    return select_slack_route(source)


def build_hourly_summary_payload(source, route_key, records, start_dt, end_dt):
    source_label = "Cradlepoint" if source == "cradlepoint" else "UMCI Camera Monitor"
    route_label = route_key.upper()
    account_name = records[0].get("account_name") if records else source_label
    category_rows = summarize_records_by_alert(records)
    details = summarize_record_details(records)
    categories = format_table(
        ["Alert Name", "Alert Count", "Affected Devices"],
        [
            [
                titleize_alert_name(row["alert_name"]),
                row["alert_count"],
                row["affected_devices"],
            ]
            for row in category_rows
        ],
    )

    return {
        "text": f"{source_label} hourly alert summary ({route_label})",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{source_label} Alert Summary",
                    "emoji": True,
                }
            },
            {
                "type": "divider",
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Route:* {route_label}",
                    }
                ]
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Account Name*\n{account_name}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Total Alerts*\n{len(records)}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Start Time*\n{format_time_to_utc(start_dt.isoformat())}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*End Time*\n{format_time_to_utc(end_dt.isoformat())}",
                    },
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Alert Categories*",
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": categories,
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Alert Details*",
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": details,
                }
            }
        ]
    }


def build_webhook_storage_records(event, body=None, fields=None, files=None):
    if body is not None and isinstance(body.get("data"), list) and body["data"]:
        return [
            build_cradlepoint_storage_record(item, event)
            for item in body["data"]
        ]

    if body is not None:
        return [build_camera_storage_record(body, event=event)]

    return [build_camera_storage_record(None, event=event, fields=fields, files=files)]


def iter_summary_targets():
    if SLACK_URL_CRADLEPOINT:
        yield "cradlepoint", "prod"

    if SLACK_URL_CRADLEPOINT_TEST:
        yield "cradlepoint", "test"


def send_hourly_summaries(event):
    event_time = parse_iso_datetime(event.get("time"))
    window_end = event_time.replace(minute=0, second=0, microsecond=0)
    window_start = window_end - timedelta(hours=1)
    summaries_sent = 0
    summary_counts = []
    deleted_alerts = 0

    for source, route_key in iter_summary_targets():
        records = query_summary_records(source, route_key, window_start, window_end)
        if not records:
            continue

        route = select_summary_route(source, route_key)
        payload = build_hourly_summary_payload(source, route_key, records, window_start, window_end)
        send_slack_message(route, payload)
        deleted_alerts += delete_alert_records(records)
        summaries_sent += 1
        summary_counts.append({
            "source": source,
            "route_key": route_key,
            "alert_count": len(records),
        })

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Hourly summaries processed successfully",
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "summaries_sent": summaries_sent,
            "deleted_alerts": deleted_alerts,
            "summary_counts": summary_counts,
        })
    }


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


def build_slack_payload_from_json(body):
    source = normalize_source(body.get("source"))
    route = with_channel_override(
        select_slack_route(source),
        body.get("slack_channel_id_override"),
    )

    timestamp = format_time_to_eastern(body.get("timestamp"))
    text = body.get("text") or body.get("message") or "[No message supplied]"

    title = resolve_alert_title(
        source,
        body.get("title"),
        default_title="Cradlepoint Alert",
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

    return route, payload, uploaded_images


def build_slack_payload_from_cradlepoint_item(item, event=None, channel_id_override=None):
    source = "cradlepoint"
    route = with_channel_override(select_slack_route(source, event=event), channel_id_override)

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

    title = resolve_alert_title(
        source,
        fields.get("title"),
        default_title="Alert",
    )

    payload = build_basic_slack_payload(title, timestamp, message, message)
    message_images = prepare_message_images(route, uploaded_images, source)
    append_uploaded_images_to_payload(payload, message_images)

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
            sleep_seconds = SLACK_POST_RETRY_DELAY_SECONDS * attempt
            logger.warning(
                "Slack file block not ready yet; retrying chat.postMessage attempt=%d/%d delay=%.1fs",
                attempt,
                SLACK_POST_MAX_ATTEMPTS,
                sleep_seconds,
            )
            time.sleep(sleep_seconds)
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
        slack_file_url = file_info.get("permalink") or file_info.get("url_private")

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
        logger.info("Raw event: %s", json.dumps(event, default=str))

        if is_scheduled_event(event):
            return send_hourly_summaries(event)

        content_type = get_header(event, "Content-Type") or ""
        logger.info("Handling webhook request content_type=%s", content_type)

        if "multipart/form-data" in content_type.lower():
            fields, files = parse_multipart_form(event)
            source = normalize_source(fields.get("source"))

            if source == "cradlepoint":
                records = build_webhook_storage_records(event, fields=fields, files=files)
                stored_alerts = persist_alert_records(records)
                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "message": "Webhook stored successfully",
                        "stored_alerts": stored_alerts,
                        "live_alerts_sent": 0,
                    })
                }

            route, payload, _ = build_slack_payload_from_multipart(fields, files)
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

        elif "application/json" in content_type.lower():
            body = parse_json_body(event)

            if isinstance(body.get("data"), list) and body["data"]:
                records = build_webhook_storage_records(event, body=body)
                stored_alerts = persist_alert_records(records)
                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "message": "Webhook stored successfully",
                        "stored_alerts": stored_alerts,
                        "live_alerts_sent": 0,
                    })
                }

            route, payload, _ = build_slack_payload_from_json(body)
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

        else:
            raise ValueError(f"Unsupported Content-Type: {content_type}")

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
