# netCloudSlackAlert

AWS Lambda webhook that accepts alert payloads and forwards them to Slack.

Today the Lambda supports two alert sources:

- `cradlepoint`
- `umci camera monitor`

It is designed so each source can route to a different Slack destination, which makes it usable for multiple customers over time.

## What It Does

- Accepts `application/json` and `multipart/form-data` requests.
- Detects the alert source from the payload.
- Builds a Slack message with a timestamp converted to Eastern time.
- Sends image content either through direct Slack file upload or through presigned S3 URLs, depending on the route configuration.
- Allows a per-request Slack channel override for bot-token based routes.

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

If the request matches one of those patterns and `SLACK_URL_CRADLEPOINT_TEST` is set, the Lambda posts that Cradlepoint alert to the test Slack webhook. Otherwise it uses `SLACK_URL_CRADLEPOINT`.

If a route has both `bot_token` and `channel_id`, the Lambda uses Slack Web API calls:

- `files.getUploadURLExternal`
- `files.completeUploadExternal`
- `chat.postMessage`

Otherwise, it posts to the configured Slack webhook URL and stores binary images in S3 first so Slack can load them from presigned URLs.

## Environment Variables

Required:

- `SLACK_URL_CRADLEPOINT`
- `SLACK_URL_CAM_MON`
- `S3_BUCKET_NAME`

Optional:

- `SLACK_URL_CRADLEPOINT_TEST`
- `SLACK_BOT_TOKEN_CAM_MON`
- `SLACK_CHANNEL_ID_CAM_MON`
- `PRESIGNED_URL_EXPIRES` default `3600`
- `SLACK_POST_RETRY_DELAY_SECONDS` default `1.5`
- `SLACK_POST_MAX_ATTEMPTS` default `5`

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

Run the saved test event locally with mocked Slack and S3 calls:

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
  "message": "Webhook processed successfully",
  "slack_status": 200,
  "slack_file_uploads": 1
}
```

Failures return:

- HTTP `502` for Slack request errors
- HTTP `500` for payload parsing or other processing errors
