# Validation before final recording

29 October 2026. This is preparation evidence, not a final campaign score.

Publication update: the maintainer completed the opening demonstration after further fixes with Antigravity/Gemini. The final source includes native event-window resolution through the idler, updated GUI/page bounds, clock anchoring and a desktop launcher. The available MP4 exports were checked locally: **120.000seconds /8,993,353bytes** (Discord copy), **119.933333seconds /23,510,404bytes** (higher-quality copy). Representative frames show the live Italian campaign and in-game desk. The maintainer reports no strategic input during the demonstrated opening; footage is cut and sped up2×. This is opening/setup evidence, not a full-war victory or a controlled cross-model benchmark. **304offline tests pass** on this source. Earlier integration failures below are retained as development history.

- Direct Mistral Studio `GET /models`: exact `mistral-large-4` available to the configured account.
- API-only tool-call integration: **passed**, one `ping(value="ok")`, **2.724 seconds**, **84 prompt /31 completion tokens**. No game access. Provider did not supply dollar cost; do not present it as zero.
- Two earlier API probes returned HTTP 400 because Studio's current Large 4 accepts low/medium/high rather than none. The client now maps none/minimal to low for this exact direct-Studio model, with an offline regression. Other profiles retain their own dialects.
- Studio Conversations rejects builtin connectors for Large 4 Preview. General web search now uses an optional operator-provided SearXNG JSON endpoint as a local tool; every inference still uses the selected model. No endpoint is configured/live-tested here; offline wiki and game-file tools remain available.
- Static game port: target1.19.4.1/build2265e6e7c1f527bf4b1f2c4b4ca3efd4371dc082. See the [port evidence](../ports/2026-10-29-1.19.4.1.md).
- Atomic writer lock: separate-process rejection tested; inherited ownership through actual `systemd-run --user --scope` tested without game/model calls.
- Native GUI: 304 elements / 38 markers; 16 EN/RU offline previews with zero detected overflows. This does not prove native click/scroll behavior.
- Offline suite: **298 tests passed** in the development tree and again in a fresh source extraction. The extraction regenerated **380 command-field /370 constructor /45 statistic tables**, built the UI and ran the suite without reusing development caches.
- Independent reviews: credential isolation, writer-lock handoff, usage timing and static port corrections reviewed; no remaining blocking findings. Tests include rejected/partial command behavior with the updated ABI addresses.
- Source archive: excludes keys, private config/logs, caches, executables and derived game DDS; embeds the exact modified bridge and per-file SHA256 hashes. Header/source version and build guard target1.19.4.1.


Live Italy integration (29 October): running build guard passed; pure reads found ITA at war with ETH,46divisions and two front sections. Known pause/timer calibration passed and left the game paused. All38GUI markers were discovered. The opening produced31successful Studio/exact-model call records (1,107,229input /81,749output tokens; monetary cost unavailable), then stopped safely because the political briefing referenced a file-only `SPA` tag. This is **not** a completed game-day or final campaign result.

The live pass exposed the native6144-byte localisation-copy limit: oversized plan/chat/feed/directive values lost their identity tail. Values are now6000bytes with explicit document parts; complete exact legacy6144-byte defaults can be recovered without weakening frozen full-buffer hash checks. The saved plan was restored to both native screen buffers in4parts, with exact byte readback. A targeted-decision candidate filter fixes the missing tag; live politics-picture readback passed. Actual hostile-border exercise admission now works before formal theatres exist; existing exercises in five contact armies were stopped through the sole Gateway and verified. **304offline tests passed**, including these regressions. Visual click/scroll acceptance remains separate from byte readback.

The linked recording is the submitted opening demonstration. Whole-war results, exhaustive mechanics coverage and automated native interaction acceptance are not claimed. Discord submission is performed by the maintainer; these preparation tools do not post it.
