#!/usr/bin/env python3
"""
Auction House buyout helper (OpenCV template matching).

The reference images in ./templates were captured on a 4K (2160p) display.
The script normalizes whatever resolution it captures down to a common working
height before matching, so the SAME templates work on 1080p / 1440p / 4K /
ultrawide without re-capturing anything.

State -> action
    no_auctions        : Esc                         (back out, try again)
    search_auctions    : Enter                        (run the search)
    confirm            : Enter
    auction_details    : Enter, Down, Enter           (open menu -> Buy Out -> select)
    buy_out            : Enter                         (confirm the buyout)
    buyout_successful  : Enter
    collect_car        : Enter, print, quit
    <unrecognized>     : Esc (debounced) until a known screen is seen again

Requires:  pip install opencv-python numpy mss
Windows only (drives the game with the Win32 SendInput API).
Kill switch: press F12 at any time.
"""

import os
import sys
import time
import ctypes

import cv2
import numpy as np
import mss

# ===========================================================================
# CONFIG  (tweak these if needed)
# ===========================================================================
TEMPLATE_DIR    = "templates"   # folder with the 7 reference PNGs
TEMPLATE_REF_H  = 2160          # vertical resolution the templates were captured at
WORK_H          = 1080          # screen + templates are normalized to this height for matching
MATCH_THRESHOLD = 0.68          # min correlation score (0..1) to accept a match. Lower = looser.
SCALE_STEPS     = (0.90, 0.95, 1.00, 1.05, 1.10)  # search window around the base scale

MONITOR_INDEX   = 1             # mss monitor: 1 = primary display, 2 = second display, etc.
LOOP_DELAY      = 0.22          # pause between scan cycles (s)
ACTION_SETTLE   = 0.45          # pause after pressing a key so the UI can transition (s)
KEY_GAP         = 0.12          # gap between chained keys within a sequence (s)
UNKNOWN_PATIENCE = 6            # consecutive "unknown screen" reads before backing out one step.
                                # Also acts as the debounce: it resets after each escape, so you
                                # back out at most one menu every (PATIENCE * cycle) seconds.
                                # Raise it if the bot escapes out of a slow-loading search screen.
START_DELAY     = 4.0           # countdown before the loop starts, to alt-tab into the game (s)
DEBUG_SCORES    = False         # True = print every template's score each cycle (for tuning)

KILL_KEY_VK     = 0x7B          # F12. Press to stop the bot. (Win32 virtual-key code.)

# template file -> state name
TEMPLATES = {
    "no_auctions":       "no_auctions.png",
    "confirm":           "confirm.png",
    "auction_details":   "auction_details.png",
    "buy_out":           "buy_out.png",
    "buyout_successful": "buyout_successful.png",
    "collect_car":       "collect_car.png",
    "search_auctions":   "search_auctions.png",
}

# ===========================================================================
# WINDOWS INPUT  (SendInput w/ hardware scan codes -> registers inside DX games)
# ===========================================================================
if sys.platform != "win32":
    sys.exit("This script is Windows-only (it uses the Win32 SendInput API to drive the game).")

PUL = ctypes.POINTER(ctypes.c_ulong)

class _KeyBdInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]

class _HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_short),
                ("wParamH", ctypes.c_ushort)]

class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]

class _InputI(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput), ("hi", _HardwareInput)]

class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", _InputI)]

_SendInput        = ctypes.windll.user32.SendInput
_GetAsyncKeyState = ctypes.windll.user32.GetAsyncKeyState

KEYEVENTF_SCANCODE    = 0x0008
KEYEVENTF_KEYUP       = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

# hardware scan codes
SC_ENTER = 0x1C
SC_ESC   = 0x01
SC_DOWN  = 0x50   # arrow keys are "extended" keys

def _send_key(scan, up=False, extended=False):
    flags = KEYEVENTF_SCANCODE
    if up:       flags |= KEYEVENTF_KEYUP
    if extended: flags |= KEYEVENTF_EXTENDEDKEY
    extra = ctypes.c_ulong(0)
    ii = _InputI()
    ii.ki = _KeyBdInput(0, scan, flags, 0, ctypes.pointer(extra))
    inp = _Input(ctypes.c_ulong(1), ii)   # type 1 = keyboard
    _SendInput(1, ctypes.pointer(inp), ctypes.sizeof(inp))

def _tap(scan, extended=False, hold=0.04):
    _send_key(scan, up=False, extended=extended)
    time.sleep(hold)
    _send_key(scan, up=True, extended=extended)

def press_enter():  _tap(SC_ENTER)
def press_escape(): _tap(SC_ESC)
def press_down():   _tap(SC_DOWN, extended=True)

def kill_requested():
    return bool(_GetAsyncKeyState(KILL_KEY_VK) & 0x8000)

# ===========================================================================
# TEMPLATE MATCHING
# ===========================================================================
def load_templates():
    """Load each template in grayscale and pre-resize it to every working-res scale."""
    base_scale = WORK_H / TEMPLATE_REF_H
    loaded = {}
    for state, fname in TEMPLATES.items():
        path = os.path.join(TEMPLATE_DIR, fname)
        if not os.path.isfile(path):
            sys.exit(f"Missing template: {path}")
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            sys.exit(f"Could not read template image: {path}")
        variants = []
        for s in SCALE_STEPS:
            scale = base_scale * s
            w = max(8, int(round(img.shape[1] * scale)))
            h = max(8, int(round(img.shape[0] * scale)))
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
            variants.append(cv2.resize(img, (w, h), interpolation=interp))
        loaded[state] = variants
    return loaded

def grab_screen(sct, monitor):
    """Capture the monitor, convert to gray, normalize to WORK_H tall."""
    raw = np.asarray(sct.grab(monitor))                 # BGRA
    gray = cv2.cvtColor(raw, cv2.COLOR_BGRA2GRAY)
    h = gray.shape[0]
    if h != WORK_H:
        scale = WORK_H / h
        new_w = max(1, int(round(gray.shape[1] * scale)))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        gray = cv2.resize(gray, (new_w, WORK_H), interpolation=interp)
    return gray

def detect(screen_gray, templates):
    """Return (best_state_or_None, best_score). Picks the single highest-scoring template."""
    sh, sw = screen_gray.shape[:2]
    best_state, best_score = None, 0.0
    per_state = {}
    for state, variants in templates.items():
        state_best = 0.0
        for tmpl in variants:
            th, tw = tmpl.shape[:2]
            if th > sh or tw > sw:
                continue
            res = cv2.matchTemplate(screen_gray, tmpl, cv2.TM_CCOEFF_NORMED)
            score = cv2.minMaxLoc(res)[1]
            if score > state_best:
                state_best = score
        per_state[state] = state_best
        if state_best > best_score:
            best_score, best_state = state_best, state
    if DEBUG_SCORES:
        line = "  ".join(f"{k}:{v:.2f}" for k, v in sorted(per_state.items(), key=lambda x: -x[1]))
        print("    " + line)
    if best_score < MATCH_THRESHOLD:
        return None, best_score
    return best_state, best_score

# ===========================================================================
# MAIN LOOP
# ===========================================================================
def main():
    print("Loading templates...")
    templates = load_templates()
    print(f"Loaded {len(templates)} templates (normalized to {WORK_H}p, threshold {MATCH_THRESHOLD}).")
    print(f"Alt-tab into the game now. Starting in {START_DELAY:.0f}s.  (press F12 to stop)")
    time.sleep(START_DELAY)

    unknown_streak = 0
    last_logged    = "__init__"

    with mss.mss() as sct:
        if MONITOR_INDEX >= len(sct.monitors):
            sys.exit(f"MONITOR_INDEX {MONITOR_INDEX} not available (found {len(sct.monitors)-1} display(s)).")
        monitor = sct.monitors[MONITOR_INDEX]
        print(f"Running on monitor {MONITOR_INDEX}  ({monitor['width']}x{monitor['height']}).")

        while True:
            if kill_requested():
                print("F12 pressed - stopping.")
                return

            screen = grab_screen(sct, monitor)
            state, score = detect(screen, templates)

            # ---- unrecognized screen: back out one step, debounced via the streak ----
            if state is None:
                unknown_streak += 1
                if last_logged != "unknown":
                    print(f"[unknown screen] best score {score:.2f} - watching...")
                    last_logged = "unknown"
                if unknown_streak >= UNKNOWN_PATIENCE:
                    print("  -> backing out (Esc)")
                    press_escape()
                    unknown_streak = 0          # re-accumulate patience before the next Esc
                    time.sleep(ACTION_SETTLE)
                else:
                    time.sleep(LOOP_DELAY)
                continue

            # ---- recognized screen ----
            unknown_streak = 0
            if state != last_logged:
                print(f"[{state}]  score {score:.2f}")
                last_logged = state

            if state == "no_auctions":
                press_escape()

            elif state == "search_auctions":
                press_enter()

            elif state == "confirm":
                press_enter()

            elif state == "auction_details":
                press_enter()
                time.sleep(KEY_GAP)
                press_down()
                time.sleep(KEY_GAP)
                press_enter()

            elif state == "buy_out":
                press_enter()

            elif state == "buyout_successful":
                press_enter()

            elif state == "collect_car":
                press_enter()
                print("car successfully bought")
                return

            time.sleep(ACTION_SETTLE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
