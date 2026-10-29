# Submission checklist and Discord draft

Requirements source: the organizer's Discord announcement supplied by the maintainer on 29 October 2026. [Challenge rules](https://mistr.al/le-chonk-challenge) could not be fetched during preparation; recheck them in the browser before publishing. Do not claim independent verification of additional rules.

- Exact gameplay model: **mistral-large-4**, **Mistral Studio API**, all roles. `mistral-large-latest` is not equivalent.
- Deadline in announcement: Unix 1793314740 = **29 October 2026 22:59 UTC / 23:59 Europe/Warsaw**.
- Post in Discord channel **1559933071527125155**. This document is a draft; no Discord message has been sent.
- Include demo video, **public GitHub repository with all code/full setup README**, and development attribution.
- Working project convention: video≤2minutes; aim 1:55. Announcement excerpt supplied here does not itself state the duration limit.
- Evaluation: complexity, performance, reproducibility. The former fun criterion is struck out in the supplied announcement.

## Before publication

- [x] Apply/review static 1.19.4.1 port; preserve build guard.
- [x] Pass live read-only/known-command smoke on 1.19.4.1 before the campaign.
- [x] Record the Italy 1936 opening with Studio/exact-model profile; recording supplied by the maintainer after further fixes with Gemini.
- [ ] Export genuine results; record game build, enabled DLC, source/bridge commits and any human intervention.
- [ ] Native UI acceptance: plan/chat scroll, feedback completion/error, secure setup launch, no clipping at recording resolution.
- [ ] Make source package; extract/rebuild/test it using README. Include exact bridge fork and its MIT license; exclude secrets, logs/saves, caches and game-derived DDS.
- [ ] Publish extracted source tree to GitHub at https://github.com/shefowl/le-chonk, embedding the exact bridge fork. Do not push the private development checkout's history.
- [ ] Replace all placeholders below with working public links and measured results. Verify video playback and duration.
- [ ] Post video + repo + development disclosure in the required channel. No automatic publication is performed by the preparation scripts.

## Ready-to-copy text

Use [DISCORD.md](DISCORD.md) for a single message under2000characters, with the video attached separately. The full expanded write-up is [POST.md](POST.md). Public repository: https://github.com/shefowl/le-chonk. Video downloads: https://github.com/shefowl/le-chonk/releases/tag/v0.1.0.

Video exports checked:119.93seconds higher-quality MP4 and120.00seconds Discord MP4 (under10MB). The submission describes an autonomous opening, not an Ethiopian-war victory. Development country examples use different models and remain explicitly separate from the final Mistral demonstration.
