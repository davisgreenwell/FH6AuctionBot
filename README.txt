AUCTION BUYOUT BOT
==================

Two scripts:
  auction_bot_fast.py   <- use this one. Event-driven, reacts in single-digit ms.
  auction_bot.py        <- the original simpler version, kept as a fallback.

SETUP (Windows)
  1. pip install -r requirements.txt
        (dxcam is the fast capture path; if it won't install, the script
         automatically falls back to mss. opencv-python + numpy are required.)
  2. Keep the layout:
        auction_bot_fast.py
        templates\   (the 8 PNGs - don't rename them)
  3. Run the game in BORDERLESS or WINDOWED mode (not exclusive fullscreen),
     or captures come back black.
  4. python auction_bot_fast.py
  5. 4-second countdown -> alt-tab into the game, leave it focused.
  6. F12 stops it any time. Ctrl+C in the console also works.

THE FLOW (per recognized screen)
  no auctions / try again ....... Esc
  Search Auctions ............... Enter
  Auction Details ............... Enter, THEN WAIT for the Buy Out option to load
  Buy Out option (gavel icon) ... Down, Enter
  Buy Out (green confirm banner)  Enter
  Confirm ....................... Enter
  Buyout Successful ............. Enter
  Collect Car ................... Enter, prints "car successfully bought", quits
  anything unrecognized ......... after a grace period, Esc one menu at a time
                                  (debounced) until a known screen returns.

  The Auction Details menu takes ~1s to open after the first Enter, so the bot
  waits for the Buy Out option to actually appear before pressing Down,Enter
  instead of firing blind. It will not re-tap during that load.

WHY IT'S FAST
  - No fixed waits - it reacts the moment the screen changes.
  - dxcam (DXGI) capture returns a frame only when the screen changes.
  - Checks the EXPECTED next screen first and stops on a confident hit, so the
    normal case is ONE template match, not eight.
  - Caches each button's location; re-checking is a sub-millisecond ROI match.
  - The keypress is sent BEFORE anything is printed.
  - Locks the matching scale on the first recognized frame.

RESOLUTION
  Most templates were captured at 4K; the Buy Out option was captured at 1080p.
  The script knows each template's capture resolution (REF_H_BY_STATE) and
  normalizes everything to a common height, so 1080p / 1440p / 4K / ultrawide all
  work. If you ever replace a template, set its capture height there if it isn't 4K.

TUNING (top of auction_bot_fast.py)
  Buyout menu timing:
    REACT_TIMEOUT_OVERRIDE["auction_details"]  how long it waits before assuming
                    the first Enter dropped and re-pressing. Raise it if your menu
                    takes longer than ~1s to open. (Default 2.0s.)
    UNKNOWN_GRACE   how long an unrecognized/loading screen is tolerated before
                    backing out. Raise if it ever escapes during the menu load.
  Speed / responsiveness:
    KEY_HOLD, SEQ_GAP   raise slightly only if the game misses keypresses.
    REACT_TIMEOUT       default self-heal delay for the simple one-key screens.
    WORK_H              matching height. 540 = accurate default; 360 = faster,
                        lower margins. Don't go below ~400.
  Recognition:
    ACCEPT / STRONG raise ACCEPT if it acts on the wrong screen; lower if it
                    misses one. The small Buy Out option button is the most
                    likely to need ACCEPT nudged if a busy screen confuses it.
    DEBUG_SCORES    True to print every template's score per evaluated frame.
  Back-out:
    ESCAPE_DEBOUNCE min time between back-out escapes (the "don't go too far" knob).
  Targeting:
    DXCAM_OUTPUT    dxcam display index (0 = primary). 1 for a second monitor.
    MONITOR_INDEX   same idea for the mss fallback (1 = primary).
