# Operator runbook

## Supported target and update verification

The checked-in tables target native Linux HOI4 **1.19.4.1**, build ID `2265e6e7c1f527bf4b1f2c4b4ca3efd4371dc082`. The static port from the previously tested 1.19.4.0 ELF (`8c82e3f8e54602565d49b9bee308f0ce09813be7`) is complete: 1720 address mappings, unchanged virtual slots and no command-field/constructor-arity changes in regenerated tables. Live Italy reads and known pause/timer calibration passed; the first full integration opening exposed corrected UI and political-briefing regressions. See [validation](submission/VALIDATION.md) for the remaining checks. Do not bypass the guard.

`python3 tools/preflight.py --json` performs local checks. `--api` adds a Studio `GET /models`, never a completion. `--live --expect ITA` adds pure memory reads, only when no harness is active; the running binary is checked too. Startup later calibrates the clock with bounded one-hour auto-pauses before model turns.

To port an update, obtain the **actual previous compatible ELF** and the new ELF. Keep backups outside the public repository. `python3 tools/port_build.py OLD NEW` is a dry run; inspect unresolved/low-confidence mappings, changed constructors, vtable slots and structure field shifts. `--apply` rewrites addresses and regenerates tables, but does not verify object field layout or prove command safety. Inspect those statically, then run read-only smoke, known no-op/CanExecute checks, pause commands and effect readback before any campaign. Never test an unidentified function in the live game.

For the local Steam installation, the cached Linux depot manifest dated 27 October is `394363_4326992221012510745.manifest`. Steam console `download_depot 394360 394363 4326992221012510745` can retrieve that depot using an account owning the game. Verify its ELF build ID before use. This is a retrieval hint, not permission to assume compatibility. The maintainer retrieved that depot on 29 October; its ELF matches 1.19.4.0 and was retained privately for the port. No executable is redistributed here.

## Install and first configuration

Follow README setup. Keep the venv active for build/check/start. `HOI4_PATH` selects the native installation; `HOI4_DIR` selects the GUI source assets (normally the same directory). `tools/install_mod.py --user-dir PATH` supports a non-default Paradox user directory and creates only the launcher descriptor. Enable the mod in the playset yourself. Restart HOI4 after changing generated GUI/DDS/effects.

The masked setup window is `python3 tools/configure_model.py`; it does not connect to the game. Defaults are direct Studio / `mistral-large-4`. Custom setup accepts base URL including `/v1` where appropriate, model ID, key. The client appends `/chat/completions`. Local APIs can use blank keys. Remote cleartext HTTP and URL-embedded credentials are rejected. No redirects are followed for credential-bearing requests.

File route:

```bash
mkdir -p config
cp config/llm.example.json config/llm.json
chmod 600 config/llm.json
# Edit config/llm.json locally; do not paste its contents into an issue/chat.
```

Custom example (replace only in your private file):

```json
{"provider":"custom","base_url":"http://127.0.0.1:8081/v1","model":"your-tool-capable-model","api_key":""}
```

`LECHONK_CONFIG=/absolute/private/llm.json` selects another file. `.env` is a simpler Studio-key-only alternative: `MISTRAL_API_KEY=...`, mode600. The JSON key wins when nonempty and its provider matches. Clearing the stored key does not clear a separately configured environment key. An invalid/insecure JSON file produces an error; it is not silently ignored.

`--models lechonk` always selects Studio/exact contest model. It rejects provider/model overrides. `--models configured` selects the saved provider/model for all roles. The older cheap development profiles remain available but are not contest evidence. No further Claude-subscription tests are needed for the final run. Studio Large 4 currently supports reasoning low/medium/high and rejects none; the client maps none/minimal to low for this exact model. The tiny API-only `tools/smoke_studio.py --run` probe passed; it is paid and accesses no game.

## Optional external search

Large 4 Preview returned “currently does not support builtin connectors” from Studio Conversations during integration. The final profile therefore uses **Chat Completions for all inference**, including summarizing search snippets. No other model/provider is substituted.

Local wiki/game-file lookup is available without another service. To enable general web search, set `LECHONK_SEARCH_URL=https://your-search.example/search` (or localhost HTTP) to an operator-provided **SearXNG JSON search endpoint** with JSON format enabled. The search request sends only the query, never a Studio/custom model key. Its results are explicitly untrusted input; the selected model judges relevance and source age. See [SearXNG search API](https://docs.searxng.org/dev/search_api.html). This optional service is not installed/launched by the harness. Without it, web_search returns a clear unavailable result and the planner can use the bundled wiki and game files.

During preparation DuckDuckGo returned a bot-check, Google no parseable results, and Bing RSS unrelated results; none was accepted as a verified search backend or bypassed. The SearXNG adapter has offline parser/transport tests; no configured external instance has been live-tested.

## Italy recording run

1. Finish version compatibility checks first. Start HOI4 with only the command-desk mod and record the enabled DLC list. Select **Italy, 1 January 1936**, historical AI focuses, regular difficulty, non-Ironman; pause. Ethiopia is already an active war. Save an untouched starting copy.
2. Close any setup window before recording. Run `python3 tools/final_run.py --api` to check. No paid generation or campaign state reset happens without `--start`. An inherited atomic file lock prevents two fresh launchers/harnesses from owning the game simultaneously.
3. Integration smoke: `python3 tools/final_run.py --start --api --days 1`. This is a **paid real Studio test**, not a cheap stand-in. Check every starting staff had a turn, focus/research/construction are represented, marshals can issue plans, chat/report feedback clears, native scroll works and no rejected actions are advertised as success. Model choices may legitimately leave something idle; distinguish choice from missing data/tool.
4. Stop normally. For a clean recording, reload the untouched Italy save on pause; then `python3 tools/final_run.py --start --api --days 30`. The script archives old `campaign.json`, `chat.json`, `roster.json` under `logs/archive/`, writes the Italy roster, and launches **without `--resume`**. It does not delete GUI marker caches. A calibrated country/day guard runs before model calls. Do not run this fresh launcher to continue a campaign.
5. The prepared roster enables every role, speed5 in peace/war, alarm-only routine turns, and asks before a new declaration of war. Planning and opening setup are still paused. Do not promise the entire campaign fits two real-time minutes; edit a truthful two-minute demo from the recording and label cuts/time-lapse.
6. Capture actual decisions and outcomes. Export measured usage with `python3 tools/run_report.py --manifest logs/final-latest.json --output docs/submission/results.json`. Do not substitute UK/USSR Haiku results for the Studio run. Dollar cost is unknown when the provider omits it; token counts are the primary measure.

## Runtime operation

The round staff-cap button opens the desk. Staff cards explain role boundaries; Contact agent opens directives/chat, Ask for report requests a model response. A spinner and queued/reply/report/complete/error states distinguish accepted actions from completion. Scroll long plans, reports and chat with the native scrollbar. Requests are disabled offline or while that agent is busy.

Settings → API SETUP launches the separate credential window only when the harness is connected. For first setup/offline use its standalone command or the **API Setup** button in `python3 tools/launcher.py`. In that launcher select **Mistral Large 4 (Mistral Studio)** for the contest profile; it also offers development profiles. Saving never changes clients already in flight; stop/restart to apply. Other settings and branch ownership are persisted in ignored `roster.json`. CLI speed0–4 corresponds to displayed1–5.

## Stop, continue, preserve evidence

```bash
pgrep -af '^(/[^ ]*/)?python3 -u -m harness[.]run'
kill -TERM $(pgrep -f '^(/[^ ]*/)?python3 -u -m harness[.]run')
# Wait for it to finish saving and exit, then verify pgrep is empty.
```

Allow up to about 200s for an in-flight turn. Avoid repeated signals/kill -9. Normal shutdown pauses the game and writes `logs/campaign.json`. If it crashes, pause the game yourself and preserve the logs/crash directory. Do not start a second writer. Never delete `logs/.lc_markers.json` while the game is running; a reload in the same PID can reuse allocations, so GUI writes require complete-buffer hashes and stopped game threads.

Continue the **same** saved campaign with the original wrapper arguments plus `--resume`; add `--rewound` only after intentionally loading an older save of that campaign. The fresh Italy launcher is not a resume command. Preserve journals (`logs/run-*.jsonl`), `logs/llm.jsonl`, final manifests and the matching save/recording privately. Review journals before publishing: they contain strategic conversations even though keys are excluded.

## Troubleshooting

| Symptom | Check / action |
|---|---|
| Build mismatch | Compare ELF IDs. Port/validate or use compatible installation; never override guard. |
| No game / wrong country | Native `hoi4`, loaded single-player Italy save on pause. Preflight refuses multiple game processes. |
| Permission denied | Same Unix user; ptrace policy; `/proc` visibility. Do not launch as root. |
| No systemd user scope / memory controller | Repair user session/cgroup setup; do not bypass the 6 GiB wrapper. |
| 401 / 403 | Check Studio key and account/model access in setup. Never paste the key into reports. |
| 404 / 422 | Correct API base/model; exact `mistral-large-4`, not latest. Custom server must support the request format. |
| 429 / slow turn | Observe retry/usage logs; lower `LLM_MAX_PARALLEL_PER_MODEL` if throttled. Do not infer a model decision from a stalled request. |
| Old UI / localization keys | Rebuild, enable correct playset descriptor, restart game. No hot-memory patch as a substitute. |
| Tk unavailable / no desktop | Install distro Tk package; use private JSON or .env route on headless machines. |
| Setup button unavailable | No connected harness. Run `python3 tools/configure_model.py` independently. |
| Too many units on quiet front | Read the supreme command's directive/context and tool results; the harness must not override a model allocation because a human dislikes it. |

## Reproduce and package

The source release embeds the exact bridge fork plus its original MIT license. Regenerate local tables/assets, install dev requirements, run the full unittest suite and offline previews. `python3 tools/package_submission.py` builds an allowlisted source zip with per-file hashes and both Git revisions, excluding private config/logs, caches, game binaries and derived DDS textures. Extract it into a fresh directory to check the README route. Upload that source tree to the submission GitHub repository; do not replace the fork with the upstream checkout. Never push `.env`, private JSON, saves, or generated proprietary textures.
