# Xiangqi · Across the River

Local two-player Chinese chess. No dependencies, network requests, AI, or accounts.

## Open and play

Open `index.html` in a modern browser. It works directly with `file://` and offline.
For mobile, copy the file to your device and open it in a browser, or serve the folder:

```sh
cd xiangqi
python3 -m http.server 8080
```

Visit `http://localhost:8080`. A phone on the same network can visit `http://YOUR_COMPUTER_LAN_IP:8080` if the computer's firewall permits it.

Red starts at the bottom. Tap a piece, then a highlighted destination. Filled green dots are moves; rings are captures. The two players alternate on the same device. The status panel announces check, checkmate, and stalemate. Undo restores the previous position and turn; restart clears the game after confirmation. Refreshing the page starts a new game.

## Rules

Standard 9×10 setup and all seven piece types are implemented: palace boundaries, elephant eye and river restriction, horse leg blocking, cannon capture screens, soldier river crossing, flying general prohibition, and self-check prevention. Checkmate and stalemate both award the win to the opponent. The game ends before a general is captured. Repetition, perpetual-check adjudication, move clocks, and tournament draw rules are outside v1.

## Verify

With Node.js installed:

```sh
node test.cjs
```

The regression suite checks opening setup and move count, palace rules, horse and elephant blockers, cannon screens, soldiers, flying generals, self-check, checkmate, stalemate, and a deterministic 100-ply legal playout. No dependencies are needed for tests.

## Files

- `index.html`: complete self-contained application (HTML, CSS, rules engine, UI).
- `test.cjs`: automated rules tests.
- `README.md`: this guide.

The UI uses system fonts, responsive sizing, native buttons with square/piece labels, keyboard focus indicators, and live status announcements. Browser visual QA was unavailable in the build environment; rules tests and JavaScript syntax checks pass.
