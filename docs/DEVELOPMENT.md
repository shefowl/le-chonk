# Development and attribution

This entry has one human maintainer, **Aleksey** (the Git author), and approximately two weeks of test runs and debugging. Development used **Claude Code (Opus 5.5)**, **Codex (Sol 6.1)** and **Antigravity (Gemini 3.8 Flash)** for implementation, debugging, tests and review. The last UI, native event-window and clock fixes and the desktop launcher were completed with Antigravity. These coding assistants are development tools; the contest gameplay profile is **Mistral Large 4 Preview (`mistral-large-4`) through Mistral Studio for every in-game role**.

The project builds on **[smerss/Clausewitz-MCP](https://github.com/smerss/Clausewitz-MCP)**, MIT, copyright2026 smerss. The modified fork lives in a separate `bridge/` repository. It provides native-process access, RTTI/function recovery, command queue plumbing and the initial game API. The harness extends and ports that bridge, adds state/context and role tools, planning and scheduling, feedback/readback, memory safeguards, and the native command desk. The bridge is a substantial foundation, not a minor dependency. Both commits are recorded in each source package; retain `bridge/LICENSE`.

The implementation was developed through static analysis of the Linux executable, read-only inspection, command construction/eligibility/readback checks, offline regression tests and development campaigns as Germany, France, Japan, the UK and the USSR. Development runs used different models, including Claude Haiku subscription and OpenRouter profiles, to find missing information, missing tools and failures. They are not evidence that Mistral won those campaigns. The submitted Italy/Studio video demonstrates the opening only: no full-war victory is claimed. The footage is cut and sped up2×, with no human strategic orders during the demonstrated opening, as reported by the maintainer.

Safety work includes a single game worker, build-ID refusal, process-tree6 GiB/no-swap limits, bounded startup clock calibration, full-buffer hashes and frozen threads for GUI text writes, explicit partial/rejected outcomes and emergency pause signals. Earlier crashes and incorrect clock discovery are documented in the audit and crash notes; these mechanisms reduce risk, not prove the absence of native-integration bugs.

Third-party components:

- **NumPy, Requests, Pillow:** Python runtime libraries; see package metadata for their licenses. Direct Studio calls use Requests; no Mistral/OpenAI SDK is required.
- **Python/Tkinter, Linux/systemd, GNU binutils:** runtime, local secret-entry UI, memory limit and static-analysis tools.
- **Claude Agent SDK:** optional development/subscription adapter and its offline tests, not required for the Studio game client.
- **HOI4 wiki extracts:** `knowledge/wiki/`, community-authored CC BY-SA text. Each page retains source URL/revision/license. Retrieval/normalization uses `tools/wiki_pull.py`; its cache freshness and verification version are exposed to the planner. These files keep their own license, independently of source-code MIT.
- **SearXNG:** optional separately operated search service, not bundled or launched here. Its JSON search API is used only if configured; every inference stays on the selected model/provider.
- **Hearts of Iron IV:** proprietary game/artwork by Paradox Interactive. Users must own/install it. GUI artwork is derived locally from that installation; generated DDS textures and game executables are omitted from the public source package.

The harness does not hard-code Italy's winning campaign or choose targets on the model's behalf. It exposes mechanics, observations and automation available to a player, while the model chooses strategy and orders. `--history on` is an explicit reproducible rules constraint. Game-native frontline execution is used rather than hand-coded tactical micro replacing the model.
