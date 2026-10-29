🇫🇷 **Le Stratège. Le Diplomate. Le Ministre. L’Industriel. Le Général. Le Général de l’Air. L’Amiral. Le Maréchal.**

**Et au-dessus de tous… LE CHONK. 🐈**

*An entire nation. An entire general staff. One very large cat in command.*

**Hearts of Iron IV, run by Mistral Large 4 Preview — `mistral-large-4`, through the Mistral Studio API.** The same model can fill every seat in the cabinet.

The harness can run the country autonomously, or play alongside you. Don't enjoy managing the navy? Enable **L’Amiral**, give it your orders, and keep the rest of the country under your control. Every branch can be delegated or taken back through an in-game command desk.

**Code, setup and runbook:** https://github.com/shefowl/le-chonk

**Two-minute Italy demo:** https://github.com/shefowl/le-chonk/releases/tag/v0.1.0

**Q: What's implemented?**

National focuses, laws, advisors, decisions, construction, research, production, trade, diplomacy, espionage, division templates and recruitment, army organisation, fronts and battle plans, naval missions, air wings and missions, logistics, supply and HQ tools. The goal is to expose the player's toolkit across the whole game; there are still gaps and rough edges to find.

**Q: How good is it?**

Think of an enthusiastic human with fewer than 100 hours in HOI4, rather than a veteran with a perfect build. Give it a difficult country without a guide, and it can make very human mistakes. The harness provides information and tools; it doesn't secretly replace the model's strategy with a winning script.

Five development campaigns, using different development models, gave us a useful mix of successes and disasters. These aren't all Mistral Large 4 results:

- **Germany, 1939:** defeated Poland and France without much trouble.
- **France, 1936, historical:** held the Belgian border and the Maginot Line for roughly three months, without a prescribed survival plan.
- **Japan, 1936, historical:** got stuck at Beiping. Invading China turns out to require a plan :)
- **UK, 1936, historical:** beat German and Italian naval forces, made progress in Africa, and built a strong industrial base by 1940.
- **USSR, 1936, historical:** worried so much about a Japanese attack that it kept forces in the Far East while the front against Germany collapsed :)

**Q: How does it work?**

HOI4 has too many moving parts for one agent to keep in its head. So I gave Le Chonk a whole war cabinet:

1. **Le Stratège — Planner.** Writes the long-term plan: economy, research, production, political direction, force targets and future wars. Answers to the player; the supreme command implements its plan and can request a revision.
2. **LE CHONK — Supreme command.** Turns that plan into theatre objectives, force allocations and orders to the ministries and staffs. Handles strategic diplomacy and requests a new plan when circumstances change.
3. **Le Diplomate — Events & requests.** Handles incoming events and diplomatic requests between strategic turns, following the same plan and player directives. It keeps time-sensitive choices from waiting for the next cabinet meeting.
4. **Le Ministre — Politics.** Manages focuses, political power, laws, advisors and decisions, including country-specific mechanics. Reports to supreme command.
5. **L’Industriel — Industry & research.** Manages construction, research, production lines, trade and equipment requirements. Turns the cabinet's requirements into factories and actual kit.
6. **Le Général — Army staff.** Forms armies and army groups, appoints commanders, edits templates, recruits and equips divisions, and manages training. Prepares the force; theatre commanders fight it.
7. **Le Général de l’Air — Air staff.** Creates and deploys wings, assigns airfields and missions, and coordinates fighters, bombers and close air support with the campaign.
8. **L’Amiral — Admiralty.** Organises fleets and task forces, assigns missions, escorts convoys, contests sea zones and supports landings. Coordinates naval requirements with industry and the theatre commanders.
9. **Le Maréchal — Field marshal.** One agent per theatre: reads its front, assigns army sectors, draws attacks and fallback lines, handles operations and reports losses or requests reinforcements. Answers to supreme command.

Technically, it's a Python orchestration layer on top of a modified **[Clausewitz-MCP](https://github.com/smerss/Clausewitz-MCP)** bridge. It reads live game memory and submits known native commands through HOI4's own command queue. Agents receive role-specific briefings, maps and tools; API requests can run in parallel, while a single Gateway serialises game access. A native scripted GUI shows plans, reports and conversations and lets you give directives or change who controls each branch. BYOK supports Mistral Studio and custom compatible endpoints.

**Q: How was it made?**

One human developer, assisted by **Claude Code (Opus 5.5)**, **Codex (Sol 6.1)** and **Antigravity (Gemini 3.8 Flash)**, with about two weeks of test runs and debugging. The memory bridge is based on **smerss/Clausewitz-MCP**, a substantial open-source foundation, extended and ported for this project. Full setup instructions and attribution are in the repository.

The video shows the start of an **Italy** campaign: the model writes its plan, selects focuses, sets up industry, and forms fronts and battle plans against Ethiopia, without strategic orders from me. This is an opening demonstration, not a claim of a completed campaign. The footage is cut in the middle and sped up **2×** to fit two minutes.

I plan to keep supporting this after the challenge. The co-op side in particular feels useful: delegating the navy, air force or industry could make HOI4 much more approachable for people who are interested in the game but put off by the sheer number of mechanics.
