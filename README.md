# netCloudSlackAlert

AWS Lambda webhook that accepts alert payloads, stores Cradlepoint alerts in DynamoDB for hourly Slack summaries, and still sends camera monitor alerts to Slack immediately.

Today the Lambda supports two alert sources:

- `cradlepoint`
- `umci camera monitor`

It is designed so each source can route to a different Slack destination, which makes it usable for multiple customers over time.

## What It Does

- Accepts `application/json` and `multipart/form-data` requests.
- Detects the alert source from the payload.
- Normalizes and stores Cradlepoint alerts in DynamoDB.
- Does not send live Slack messages for Cradlepoint alerts.
- Keeps `umci camera monitor` alerts on their existing immediate Slack path.
- Sends hourly Cradlepoint Slack summaries from an EventBridge schedule.
- Keeps Cradlepoint production and test routes separated by API path.

## Supported Sources

### `cradlepoint`

Accepted source aliases:

- `cradlepoint`
- `netcloud`
- `netcloud manager`

If the JSON payload contains a `data` array, the Lambda treats it as a Cradlepoint-style payload and formats the first item in the array into a text alert.

### `umci camera monitor`

Accepted source value:

- `umci camera monitor`

This is now the only accepted camera-monitor source string. Older shorthand values such as `cam_mon` are not accepted.

For `umci camera monitor` payloads:

- If `title` is present and non-empty, Slack uses that value as the message header.
- If `title` is missing or blank, the header defaults to `Camera Alert`.
- Message body text comes from `text`, then `message`, then `[No message supplied]`.

## Request Formats

### JSON

Typical JSON body fields:

```json
{
  "source": "UMCI Camera Monitor",
  "title": "Daily Camera Report",
  "timestamp": "2026-04-01T18:55:42Z",
  "text": "Camera status summary",
  "chart_image_base64_png": "<base64 PNG>",
  "image_url": "https://example.com/image.png",
  "slack_channel_id_override": "C1234567890"
}
```

Notes:

- `source` matching is case-insensitive.
- `chart_image_base64_png` is decoded and attached as `chart.png`.
- `image_url` is added as an image block.
- `slack_channel_id_override` is applied only when the selected route has both a bot token and a channel ID configured.

### Multipart Form

Supported multipart fields:

- `source`
- `title`
- `timestamp`
- `message` or `text`
- `slack_channel_id_override`
- one or more files in the `images` field

For multipart requests, at least one `images` file is required.

## Slack Routing

Routing is selected by normalized source:

- `cradlepoint` uses `SLACK_URL_CRADLEPOINT`
- `cradlepoint` requests sent to a test path can use `SLACK_URL_CRADLEPOINT_TEST`
- `umci camera monitor` uses the camera-monitor Slack settings:
  - `SLACK_URL_CAM_MON`
  - `SLACK_BOT_TOKEN_CAM_MON`
  - `SLACK_CHANNEL_ID_CAM_MON`

The environment variable names still use `CAM_MON`, but the payload source value is `umci camera monitor`.

For Cradlepoint, the Lambda can differentiate production vs test alerts without any payload changes by looking at the API Gateway request path:

- paths ending in `/test`
- paths containing `cradlepoint-test`

If the request matches one of those patterns and `SLACK_URL_CRADLEPOINT_TEST` is set, the Lambda sends that route's hourly summary to the test Slack webhook. Otherwise it uses `SLACK_URL_CRADLEPOINT`.

If a route has both `bot_token` and `channel_id`, the Lambda uses Slack Web API calls for summary delivery:

- `files.getUploadURLExternal`
- `files.completeUploadExternal`
- `chat.postMessage`

Otherwise, it posts summary messages to the configured Slack webhook URL.

## Data Storage And Summaries

Each Cradlepoint webhook alert is stored as a normalized record in DynamoDB. The Lambda groups records by source and route, then on the hourly schedule it summarizes the previous completed hour.

The current summary includes:

- summary window start and end time
- total alert count
- alert categories with counts and affected device counts
- alert detail lines with timestamp, alert name, device, MAC, and status
- deletion of the summarized Cradlepoint records after a successful summary send

Current behavior:

- Cradlepoint webhook requests: store only
- camera monitor webhook requests: send live Slack alerts
- scheduled EventBridge invocation: query DynamoDB, send Cradlepoint summaries, then delete the summarized records

## Environment Variables

Required:

- `SLACK_URL_CRADLEPOINT`
- `SLACK_URL_CAM_MON`
- `S3_BUCKET_NAME`
- `DYNAMODB_ALERT_TABLE`

Optional:

- `SLACK_URL_CRADLEPOINT_TEST`
- `SLACK_BOT_TOKEN_CAM_MON`
- `SLACK_CHANNEL_ID_CAM_MON`
- `DYNAMODB_ALERT_GSI_NAME` default `gsi1`
- `ALERT_RETENTION_DAYS` default `30`
- `PRESIGNED_URL_EXPIRES` default `3600`
- `SLACK_POST_RETRY_DELAY_SECONDS` default `1.5`
- `SLACK_POST_MAX_ATTEMPTS` default `5`

## AWS Setup For Hourly Summaries

You need three AWS pieces for the hourly-summary flow:

1. an API Gateway route for incoming webhooks
2. a DynamoDB table for stored alerts
3. an EventBridge schedule that invokes the Lambda every hour

### DynamoDB Table

Recommended table:

- table name: `netCloudSlackAlertEvents`
- partition key: `pk` (String)
- sort key: `sk` (String)

Recommended global secondary index:

- index name: `gsi1`
- partition key: `gsi1pk` (String)
- sort key: `gsi1sk` (String)

The Lambda stores one normalized item per incoming Cradlepoint alert and uses `gsi1` to read the last hour of alerts for each route bucket.

### EventBridge Schedule

Create an hourly EventBridge rule that targets this same Lambda.

Suggested schedule expression:

```text
rate(1 hour)
```

On each scheduled invocation, the Lambda summarizes the previous completed hour and posts one Cradlepoint summary per route that had alerts during that window.

## AWS Setup For A Cradlepoint Test Feed

If you cannot change the Cradlepoint payload, create a second API Gateway endpoint and let the Lambda route based on the request path.

Suggested pattern:

- production URL: `/webhook`
- test URL: `/webhook/test`

### 1. Add the test Slack webhook to Lambda

In the Lambda environment variables, add:

```text
SLACK_URL_CRADLEPOINT_TEST=https://hooks.slack.com/services/...
```

Keep your existing production variable:

```text
SLACK_URL_CRADLEPOINT=https://hooks.slack.com/services/...
```

### 2. Add a second API Gateway route

In API Gateway for this Lambda:

1. Open your HTTP API.
2. Go to `Routes`.
3. Create a new route:
   - method: `POST`
   - path: `/webhook/test`
4. Attach it to the same Lambda integration already used by `/webhook`.
5. Deploy the API.

After deployment you should have two usable URLs:

- `https://<api-id>.execute-api.<region>.amazonaws.com/Prod/webhook`
- `https://<api-id>.execute-api.<region>.amazonaws.com/Prod/webhook/test`

### 3. Point the Cradlepoint test feed at the new URL

Leave production Cradlepoint sending to:

```text
.../Prod/webhook
```

Send the Cradlepoint test feed to:

```text
.../Prod/webhook/test
```

The payload can remain identical for both.

### 4. Confirm routing

Expected behavior:

- `/webhook` -> `SLACK_URL_CRADLEPOINT`
- `/webhook/test` -> `SLACK_URL_CRADLEPOINT_TEST`

If `SLACK_URL_CRADLEPOINT_TEST` is not set, test-path requests fall back to the production Cradlepoint webhook.

## Local Development

Install dev dependencies:

```bash
make install-dev
```

Run the saved test event locally with mocked Slack, S3, and DynamoDB calls:

```bash
make test
```

Send the saved event to the live API:

```bash
make live-send
```

Useful overrides:

- `EVENT=path/to/event.json`
- `LIVE_URL=https://...`
- `LIVE_CHANNEL_ID=C1234567890`

## Example Test Event

The repository includes a sample API Gateway event at `events/console-test-event.json` that uses an `UMCI Camera Monitor` JSON payload.

## Response Shape

Successful requests return HTTP `200` with a body like:

```json
{
  "message": "Webhook stored successfully",
  "stored_alerts": 1,
  "live_alerts_sent": 0
}
```

Failures return:

- HTTP `502` for summary Slack request errors
- HTTP `500` for payload parsing or other processing errors
