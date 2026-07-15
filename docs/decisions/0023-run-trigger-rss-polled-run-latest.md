# Run trigger / cadence: an idempotent, RSS-polled `run-latest` (resolves §15 #13)

- Status: accepted
- Date: 2026-07-14
- Deciders: owner, python-dev

## Context and Problem Statement

money-pit is spec'd to run **unattended on a schedule** (`architecture.md` §1/§4/§13), but a run is
manual today: `money-pit run <url>` ingests one URL and drives `run_pipeline`. `architecture.md` §15 #13
left the trigger/cadence open — *"what starts a run — still the video ('new episode' anchors cadence),
a fixed schedule, or any-source arrival? Lean: video stays the anchor at first"* — and §13 named
"APScheduler/cron" for scheduling. This ADR resolves §15 #13 and defines the autonomous trigger.

At N=1 the signal supply is a single YouTube channel (§15 #8), so the trigger is naturally
**video-anchored**: a new episode on that channel is what should start a run.

## Decision Drivers

- Unattended, autonomous operation on the owner's personal machine (Windows).
- Robustness of the single-channel signal supply (§15 #8): the "is there a new episode?" poll runs often
  and must not be fragile.
- Testability and safety: the trigger should be idempotent (safe to run repeatedly) and fully testable
  offline, not a long-running process that has to be kept alive.

## Decision Outcome

1. **Video-anchored new-episode detection.** The newest episode on the configured channel triggers a run.
2. **An idempotent one-shot `money-pit run-latest`, driven by the OS scheduler** (Windows Task Scheduler /
   cron) — **not** an in-process APScheduler daemon. `run-latest` detects the newest episode, skips it if
   already processed (a persistent ledger), runs the full pipeline if it's new, and exits. The OS scheduler
   owns the cadence. This is the "cron" arm of the spec; **no APScheduler dependency is added**. Rationale:
   no daemon to babysit, cadence is the OS's job, and a one-shot with injected seams is fully unit-testable.
3. **Detection via the YouTube RSS feed** (`https://www.youtube.com/feeds/videos.xml?channel_id=<UC…>`,
   parsed with stdlib `xml.etree`) — chosen over yt-dlp channel scraping because it is **officially served
   and far more stable** for a frequent poll, which matters given the whole signal supply is this one
   channel. The actual video **download is unchanged** (the Phase 9 yt-dlp fetcher); only the lightweight
   "newest video id" poll uses RSS.
4. **Dedup via a persistent processed-episodes ledger** in `user_state_folder()` (previously unused),
   keyed by `yt:<videoId>` (the existing `SourceRef.source_id` scheme). **Record only on a completed run**:
   a run that raises is not marked, so it is retried on the next poll.

### Consequences

Good: the system runs itself — the OS scheduler calls `run-latest` on whatever cadence the owner sets, and
it no-ops until a genuinely new episode appears (safe to over-poll). Detection is robust (official feed) and
carries no new dependency; the whole trigger is idempotent and unit-tested offline via injected seams. The
run slug stays timestamp-based (`pipeline_contracts.md` §0); episode identity lives on `source_id`, which
the ledger dedupes on.

Bad / to watch: the single-channel dependency (§15 #8) remains — a channel rename/removal or an RSS format
change breaks detection (yt-dlp channel scraping is the fallback if RSS ever changes). The RSS feed exposes
only the latest ~15 videos, which is sufficient for newest-episode detection but not for backfilling
history. The OS scheduler must be configured (a deployment step, documented). A persistently-failing episode
retries on every poll (loud logs; acceptable, and avoids silently skipping a real episode).

## Considered Options (key rejections)

- **In-process APScheduler daemon (`watch`).** Rejected: a long-running process to keep alive and monitor,
  and harder to test than an idempotent one-shot; the OS scheduler already provides reliable cadence.
- **Fixed-schedule pull, ignoring new-episode detection.** Rejected: it would re-run the same episode or
  miss same-day multiples, and §15 #13's lean is explicitly video-anchored.
- **Any-source arrival.** Not applicable at N=1 (only the video source exists); revisit with N>1.
- **yt-dlp `extract_flat` for detection.** Rejected for the poll: a YouTube-frontend scraper that can break
  on site changes, more fragile than the official RSS feed for a frequently-run detection call.

## More Information

Resolves `architecture.md` §15 #13; the single-channel risk it references is §15 #8. Related:
`src/money_pit/scheduler/channel.py`, `src/money_pit/scheduler/ledger.py`,
`src/money_pit/scheduler/runner.py`, `src/money_pit/__main__.py` (the `run-latest` command reusing the
`run <url>` ingest→pipeline path), `src/money_pit/constants.py` (`user_state_folder()`). Builds on ADR 0022
(ingestion packaging/boundary) and fires into the ADR 0018 recovery entry node.
