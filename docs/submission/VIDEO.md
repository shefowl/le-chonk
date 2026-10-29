# Recording plan — maximum 2 minutes

Record a real Italy 1936 Studio run after compatibility and integration smoke pass. Capture more footage than needed, then edit to **1:55**. Label jump cuts/time-lapse and keep the game date visible when showing outcomes. No scripted victory or reconstructed model statements. Do not show API keys/setup fields, private desktop notifications, or Studio billing screens.

| Time | Picture | Narration / caption |
|---|---|---|
| 0:00–0:10 | Italy and Ethiopian front; small title | “Le Chonk plays Hearts of Iron IV. Mistral Large 4, direct Studio API, every role.” |
| 0:10–0:25 | Staff tree; model/profile label or sanitized launch output | “One model runs the planner, supreme command, ministries and field marshals. Every role has its own information and tools.” |
| 0:25–0:45 | Strategic plan, scroll one full section; actual directives | “The model writes the plan, allocates armies and tells the staffs what it needs. The harness supplies facts and actions.” |
| 0:45–1:05 | Real focus/research/construction, production and air assignments | “Politics, economy and the air force work alongside the war. These are live orders through the game's command queue.” Show only actions actually observed. |
| 1:05–1:25 | Marshal's Ethiopian fronts/arrows; time-lapse of execution | “Field marshals draw operations. Game-native plan execution moves their divisions, with readback and alerts.” Add “time-lapse” when sped up. |
| 1:25–1:42 | Ask for report → spinner → actual response; give one human directive if useful | “The in-game desk lets you inspect decisions, ask questions, or take a branch back.” Disclose any human intervention. |
| 1:42–1:55 | Actual map outcome, aggregate call/token/time figures and repo link | State measured outcome honestly, including losses/failed orders if relevant. “One human maintainer; built with Claude Code and Codex, on Clausewitz-MCP.” |

Avoid claiming “autonomous win” unless the recorded run actually achieved it without undisclosed human orders. If the war is unfinished, show progress and the remaining problem. Do not call a multi-hour campaign a two-minute real-time run. Keep at least a couple seconds for the public repo URL; add subtitles for small game text.

## Evidence to retain

Matching starting save, final save, raw recording, sanitized profile/model/build/DLC metadata, `logs/final-latest.json`, corresponding run journal and usage aggregates. Record source commit hashes. Use `tools/run_report.py` after stopping the run; leave `results.json` absent until measured results exist.
