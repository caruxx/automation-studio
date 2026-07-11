# YouTube API Compliance Notes

## Scopes

- `https://www.googleapis.com/auth/youtube.upload`: upload videos and thumbnails for the selected channel.
- `https://www.googleapis.com/auth/youtube`: update video metadata, privacy, live metadata, and read channel/video data needed by operations.
- `https://www.googleapis.com/auth/youtube.readonly`: read channel/video metadata and public statistics for benchmark, tracking, and review features.
- `https://www.googleapis.com/auth/yt-analytics.readonly`: optional 28-day watch-hour summaries for monetization review.

## Quota and Metering

`Python/app_quota.py` owns `QUOTA_COSTS` and appends every metered YouTube call to `~/.config/{app_id}/youtube_api_quota.jsonl` as `{method,cost,when,channel_id}`. `GET /api/quota` returns today's standard quota usage, method breakdown, and a separate `videos.batchGetStats` pool.

Current assumptions:

- `videos.insert`: 100 units.
- `videos.batchGetStats`: 1 unit in its dedicated default 10,000 units/day pool.
- `videos.list` remains as fallback when `batchGetStats` is unavailable or fails.

## Retention and Deletion

YouTube API-derived caches and snapshots include `fetched_at`. `Python/app_retention.py` migrates older JSON records that lack `fetched_at`.

- Non-statistical API-derived cache files older than 30 days are deleted or refreshed by the retention job.
- Statistical snapshots are retained only with periodic token-validity evidence in `data_retention_log.jsonl`.
- `retention.enabled` is ON by default and can be stored in a channel config if a channel needs an explicit override.

## Disconnect Flow

The channel management UI exposes `YouTube連携解除`. The API route `POST /api/channels/{channel_id}/youtube-disconnect` revokes the channel token through Google's revoke endpoint and immediately deletes API-derived channel data, including snapshots, benchmark caches, tracking caches, learned patterns, and the channel token. Actions are recorded in `youtube_disconnect_audit.jsonl`.

Verification must use dummy channels or `dry_run/revoke=false`; production tokens and production channel data must not be deleted during tests.

## Attribution

The dashboard displays `データ提供: YouTube` with a `youtube.com` link on benchmark, statistics, tracking, and thumbnail-source surfaces. The privacy page at `/privacy` also discloses YouTube API Services usage and links to Google Privacy Policy and Google Security Settings.
