# MapleTracker

EXP, meso, potion and profit tracker overlay for MapleStory Classic.

**Start:** double-click `Start MapleTracker.bat`. It closes by itself when MapleStory closes.
It only takes screenshots and is read-only: no memory reading, nothing injected, no input sent to the game.

| | |
|---|---|
| **Overlay** | Game-style outlined text straight over the game (transparent background). It shows the session state and map, EXP gained [EXP/h], an EXP-bar gauge with level % and time to level up, mesos gained [mesos/h], and pickups |
| **Alt+F6** / right-click the overlay | The tracker window, drawn like a game window. Drag it by its title strip; Esc or X closes it |
| **Alt+F7** | Start / pause / resume, from inside the game |
| Drag the overlay's text | Move it (the position is remembered). Size: `overlay_scale` / `window_scale` in `tracker_config.json` |

## Sessions, logs and characters

- **MapleTracker starts stopped.** Nothing counts until you press **START** (or Alt+F7), so walking around town doesn't count.
- **PAUSE** freezes both the numbers and the clock. EXP you gain while paused is never counted, even after you resume.
- **STOP & SAVE** ends the session. Type a name in the **MAP** box (for example `Kerning Subway <Line 1>`) and the session is saved to the **LOG** tab:
  - when you press STOP & SAVE,
  - after every hour of tracking (the session then carries on with a fresh hour at the same map),
  - when the game closes or you quit MapleTracker.

  Sessions without a map name, or shorter than 1 minute, aren't logged.
- **LOG tab:** sessions are grouped by map. Each map shows how many sessions it has, the total time, and the average EXP/h and mesos/h. Click a map to see its sessions: date and time, length, EXP (per hour and level %/hour), mesos (per hour, pickups). **X** deletes one session and **CLEAR ALL** deletes them all; both ask first. Logs are kept between runs.
- **Switching characters automatically:** MapleTracker reads your character's name off the status bar (under your class, next to LV) and switches to that character's profile when you log into another one.
  - It needs the same name read 3 times in a row, about 6 seconds.
  - A character it hasn't seen before gets a new profile, after 5 reads.
  - Capitals and letters OCR mixes up (I/l/1, O/0) don't matter, but "Maple" and "Maple2" stay separate characters.
  - Turn it off in SETTINGS ("Switch character automatically").
- **CHARACTER** (top right of the window) switches between characters by hand. Each character has its own logs in `profiles/<name>.json`. The same menu adds, renames and deletes characters. Switching character during a session stops it and saves it to the character you were on.
- **RESET EXP / MESOS / POTIONS / ALL** zero those numbers in the current session, without saving them to the log.

## Potions and profit

- **POTIONS tab:**
  - **ADD POTION** opens the common HP/MP items (Classic item IDs, icons and NPC shop prices from [NiaDB](https://meowdb.com)). **CUSTOM ITEM...** is for anything else: type its name and price. Click a price to change it.
  - On the right is a live picture of *your* quick slots, with your keys and your items. Drag each potion onto the slot it's on; drag it off a slot to remove it. When an icon is clearly visible in a slot, it's placed automatically.
  - Each character has its own potions.
- **How usage is counted:** it reads the item count on each potion's quick slot, and only *drops* in the count are counted. A drop or a purchase raises the count, and that just becomes the new starting point. So 80 → 75 → (loot) 76 → 70 counts 5 + 6 = 11 used.
- A big drop all at once (more than 12) is assumed to be moving, dropping or selling the stack, so it isn't counted.
- **PROFIT** = mesos gained − potion cost. It's green when you're ahead and red when potions cost more than you picked up. It shows on the overlay, the TRACKER tab and every log entry.
- **SETTINGS tab:** switch the EXP, Mesos and Potions trackers on or off independently; profit needs both Mesos and Potions. Also here: close with the game, and hide from screenshots.

The count digits are read on the game's own pixel grid. The game draws at 1366x768 and stretches that to the window, so each count corner is shrunk back exactly. There, every digit is the same crisp pixel-font shape wherever it sits (1xx, 5xx and 999 alike); set `native_size` in the config if your game uses another resolution. The digit templates in `count_glyphs.npz` are built by `tools/build_count_glyphs.py` from labelled recordings (`captures/qs*`, `captures/extra_labels.json`):

- `--check` reads every labelled count and replays the recordings through the potion counter.
- `--holdout` learns from part of the data and tests on the rest.

## How it reads the game

- **EXP** comes from the status bar number (`EXP. 62,312 [85.27%]`), matched against the digit templates in `statusbar_glyphs.npz`. That number is the game's own total, so kills, the yellow *Bonus EXP for PARTY* line and quest EXP all count, exactly once. On a level-up, the EXP left in the old level is added on; EXP lost on death isn't subtracted. A reading only counts when it repeats and agrees with the percentage, so a misread is ignored. Switching characters (with the character-select gap in between) starts fresh instead of counting a fake level-up.
- **Mesos** come from the white `You have gained mesos (+N)` messages at the bottom right. Text is picked out from any map by looking for bright pixels with the game's dark drop shadow, so faded and yellow lines drop out on their own. Lines are read with Windows' built-in OCR, followed as the stack scrolls up, and each one counts once its amount has read the same twice.
- **Character names** are read with the same OCR from the status bar, next to LV.
- It only scans while the game window has focus. Pickups made while you're alt-tabbed aren't seen; EXP still catches up, because the status bar holds the total.
- Its windows show up in your screenshots and recordings like any other window, so keep the overlay and tracker window off the status bar's EXP number, the bottom-right message area and your potion slots (the overlay warns you in red if one covers them). "Hide from screenshots" in SETTINGS lets them sit anywhere, but then they're also invisible to OBS, Discord and the Snipping Tool.

## Install

Windows 10 or 11, with [Python](https://www.python.org/) 3.10 or newer, then:

```
python -m pip install mss numpy opencv-python pillow winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Globalization winrt-Windows.Foundation winrt-Windows.Foundation.Collections
```

Windows OCR needs an English language pack (Settings > Time & language > Language).

## Tuning / troubleshooting

`tracker_config.json` (created on first run) sets screen positions in game units (screen px / (game height / 768)), measured on a 2560x1440 game window: `exp_text` (the EXP number, from the bottom-centre), `msg_area` (the pickup messages, from the bottom-right), `name_text` (the character name) and `quickslots` (the quick-slot grid).

- `python maple_tracker.py --debug` logs every EXP change, meso pickup and potion use to `tracker_log.txt`.
- `python tools/build_glyphs.py` rebuilds the status bar digits, and `python tools/build_count_glyphs.py` the quick-slot digits. Both need labelled screen recordings in `captures/`, which aren't included in this repository (they contain other players' names and chat).

## Credits

- Map, monster and item data (item IDs, icons, NPC prices): [NiaDB](https://meowdb.com), used with permission. MapleTracker caches what it looks up and asks NiaDB for at most one page every 10 seconds.
- MapleStory is a trademark of NEXON. MapleTracker is a fan-made tool and isn't affiliated with NEXON or NiaDB.

## License

[MIT](LICENSE)

