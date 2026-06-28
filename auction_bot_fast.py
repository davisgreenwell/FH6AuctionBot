#!/usr/bin/env python3
"""
Auction House buyout helper - FAST build (v2).

Change in v2: the "Auction Details" menu takes ~1s to load after the first Enter,
so instead of blindly pressing Enter,Down,Enter we now:
    Auction Details   -> press Enter, then WAIT for the Buy Out option to appear
    Buy Out option    -> press Down, then Enter
    Buy Out (confirm) -> press Enter
This is fully event-driven, so it waits exactly as long as the load takes.

Speed techniques (unchanged): DXGI capture (dxcam) that only returns a frame when
the screen changes; 540p matching; check the expected screen first and bail on a
strong hit; cache each button's location for sub-millisecond re-checks; keypress
sent before any logging; lock the matching scale on the first recognized frame.

State -> action
    no_auctions        : Esc
    search_auctions    : Enter
    confirm            : Enter
    auction_details    : Enter            (then wait for buyout_option)
    buyout_option      : Down, Enter
    buy_out            : Enter            (the green confirmation banner)
    buyout_successful  : Enter
    collect_car        : Enter, print, quit
    <unrecognized>     : after a grace period, Esc one menu at a time (debounced)

Requires:  pip install opencv-python numpy dxcam mss   (mss is the dxcam fallback)
Windows only.  Kill switch: F12.
"""

import os
import sys
import time
import ctypes

import cv2
import numpy as np

# ===========================================================================
# CONFIG
# ===========================================================================
TEMPLATE_DIR    = "templates"
WORK_H          = 540          # classify at this height (accuracy/speed sweet spot)

# Templates were captured at 4K, EXCEPT buyout_option which was shot at 1080p.
# Per-template capture height keeps every template matched at the correct size.
DEFAULT_REF_H   = 2160
REF_H_BY_STATE  = {"buyout_option": 1080}

ACCEPT          = 0.66         # min score to act on a screen
STRONG          = 0.78         # score that ends the search early (confident hit)
CALIB_MULT      = (0.90, 0.95, 1.00, 1.05, 1.10)  # scale multipliers tried until one locks
ROI_PAD         = 18           # px slack around a cached location for the fast re-check

DXCAM_OUTPUT    = 0            # dxcam output index (0 = primary display)
MONITOR_INDEX   = 1            # mss monitor index (1 = primary) - mss fallback only

# timing (seconds)
KEY_HOLD        = 0.012        # key down->up hold
SEQ_GAP         = 0.022        # gap between Down and Enter in the option sequence
REACT_TIMEOUT   = 0.15         # default: re-send if a screen persists this long after acting
# Steps that are followed by a load get a longer re-send window so we don't double-tap
# while the next screen is still coming up:
REACT_TIMEOUT_OVERRIDE = {
    "auction_details": 2.0,    # the options menu takes ~1s to open after Enter
    "buyout_option":   1.2,    # the confirm banner takes a moment after Down,Enter
}
UNKNOWN_GRACE   = 1.50         # tolerate an unrecognized screen this long before backing out
ESCAPE_DEBOUNCE = 0.55         # min time between back-out escapes
IDLE_SLEEP      = 0.002        # tiny yield when nothing changed
START_DELAY     = 4.0          # countdown to alt-tab into the game

DEBUG_SCORES    = False        # print scores each evaluated frame (for tuning)
KILL_KEY_VK     = 0x7B         # F12

TEMPLATES = {
    "no_auctions":       "no_auctions.png",
    "search_auctions":   "search_auctions.png",
    "confirm":           "confirm.png",
    "auction_details":   "auction_details.png",
    "buyout_option":     "buyout_option.png",
    "buy_out":           "buy_out.png",
    "buyout_successful": "buyout_successful.png",
    "collect_car":       "collect_car.png",
}
ALL_STATES = list(TEMPLATES.keys())

# Likely next screen first, so the common path matches one template and stops.
# Every list still contains all states as a fallback.
NEXT_EXPECTED = {
    None:                ["search_auctions", "auction_details", "no_auctions", "confirm", "buyout_option", "buy_out", "buyout_successful", "collect_car"],
    "search_auctions":   ["auction_details", "no_auctions", "confirm", "buyout_option", "buy_out", "buyout_successful", "collect_car", "search_auctions"],
    "no_auctions":       ["search_auctions", "auction_details", "confirm", "buyout_option", "buy_out", "no_auctions", "buyout_successful", "collect_car"],
    "auction_details":   ["buyout_option", "buy_out", "confirm", "auction_details", "buyout_successful", "no_auctions", "search_auctions", "collect_car"],
    "buyout_option":     ["buy_out", "confirm", "buyout_successful", "buyout_option", "collect_car", "no_auctions", "auction_details", "search_auctions"],
    "confirm":           ["buyout_successful", "collect_car", "buy_out", "confirm", "no_auctions", "buyout_option", "auction_details", "search_auctions"],
    "buy_out":           ["buyout_successful", "confirm", "collect_car", "no_auctions", "buy_out", "buyout_option", "auction_details", "search_auctions"],
    "buyout_successful": ["collect_car", "confirm", "buyout_successful", "search_auctions", "auction_details", "no_auctions", "buyout_option", "buy_out"],
    "collect_car":       ["collect_car", "search_auctions", "no_auctions", "auction_details", "confirm", "buyout_option", "buy_out", "buyout_successful"],
}

# ===========================================================================
# WINDOWS INPUT (guarded so the module imports on non-Windows for testing)
# ===========================================================================
IS_WIN = sys.platform == "win32"

KEYEVENTF_SCANCODE    = 0x0008
KEYEVENTF_KEYUP       = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
SC_ENTER = 0x1C
SC_ESC   = 0x01
SC_DOWN  = 0x50  # extended key

if IS_WIN:
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

    def _send_key(scan, up=False, extended=False):
        flags = KEYEVENTF_SCANCODE
        if up:       flags |= KEYEVENTF_KEYUP
        if extended: flags |= KEYEVENTF_EXTENDEDKEY
        extra = ctypes.c_ulong(0)
        ii = _InputI(); ii.ki = _KeyBdInput(0, scan, flags, 0, ctypes.pointer(extra))
        inp = _Input(ctypes.c_ulong(1), ii)
        _SendInput(1, ctypes.pointer(inp), ctypes.sizeof(inp))

    def kill_requested():
        return bool(_GetAsyncKeyState(KILL_KEY_VK) & 0x8000)
else:
    def _send_key(scan, up=False, extended=False):  # inert off-Windows
        pass
    def kill_requested():
        return False

def _tap(scan, extended=False):
    _send_key(scan, up=False, extended=extended)
    if KEY_HOLD: time.sleep(KEY_HOLD)
    _send_key(scan, up=True, extended=extended)

def press_enter():  _tap(SC_ENTER)
def press_escape(): _tap(SC_ESC)

def fire(state):
    """Send the key(s) for a recognized screen. Called before any logging."""
    if state == "no_auctions":
        press_escape()
    elif state == "buyout_option":
        _tap(SC_DOWN, extended=True)
        time.sleep(SEQ_GAP)
        _tap(SC_ENTER)
    elif state in ("search_auctions", "confirm", "auction_details",
                   "buy_out", "buyout_successful", "collect_car"):
        press_enter()

# ===========================================================================
# DETECTOR  (priority order + strong-hit short-circuit + location cache + scale lock)
# ===========================================================================
class Detector:
    def __init__(self):
        self.locked = False
        self.loc_cache = {}                  # state -> (x, y, w, h) in WORK_H coords
        self._raw = {}
        self.base = {}                       # per-template base scale = WORK_H / capture_height
        for st, fn in TEMPLATES.items():
            p = os.path.join(TEMPLATE_DIR, fn)
            img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
            if img is None:
                sys.exit(f"Could not read template: {p}")
            self._raw[st] = img
            ref = REF_H_BY_STATE.get(st, DEFAULT_REF_H)
            self.base[st] = WORK_H / ref
        self._build(list(CALIB_MULT))

    def _build(self, mults):
        """variants[state] = list of (multiplier, resized_template_gray)."""
        self.variants = {}
        for st, img in self._raw.items():
            b = self.base[st]
            vs = []
            for m in mults:
                s = b * m
                w = max(6, round(img.shape[1] * s))
                h = max(6, round(img.shape[0] * s))
                interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_LINEAR
                vs.append((m, cv2.resize(img, (w, h), interpolation=interp)))
            self.variants[st] = vs

    def _match_state(self, gray, st):
        """Return (best_score, best_loc(x,y,w,h), best_multiplier) for one state."""
        gh, gw = gray.shape[:2]
        # Fast path: if locked and we know where this button was, re-check just there.
        if self.locked and st in self.loc_cache:
            m, tmpl = self.variants[st][0]
            th, tw = tmpl.shape[:2]
            cx, cy, cw, ch = self.loc_cache[st]
            x0, y0 = max(0, cx - ROI_PAD), max(0, cy - ROI_PAD)
            x1, y1 = min(gw, cx + cw + ROI_PAD), min(gh, cy + ch + ROI_PAD)
            roi = gray[y0:y1, x0:x1]
            if roi.shape[0] >= th and roi.shape[1] >= tw:
                r = cv2.matchTemplate(roi, tmpl, cv2.TM_CCOEFF_NORMED)
                _, sc, _, ml = cv2.minMaxLoc(r)
                if sc >= STRONG:
                    return sc, (x0 + ml[0], y0 + ml[1], tw, th), m
            # cache miss -> fall through to full search

        best_sc, best_loc, best_m = 0.0, None, None
        for m, tmpl in self.variants[st]:
            th, tw = tmpl.shape[:2]
            if th > gh or tw > gw:
                continue
            r = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
            _, sc, _, ml = cv2.minMaxLoc(r)
            if sc > best_sc:
                best_sc, best_loc, best_m = sc, (ml[0], ml[1], tw, th), m
        return best_sc, best_loc, best_m

    def classify(self, gray, last_state):
        """Return (state_or_None, score). Expected screens first; stop on a strong hit."""
        order = NEXT_EXPECTED.get(last_state, ALL_STATES)
        best_st, best_sc, best_loc, best_m = None, 0.0, None, None
        scores = {} if DEBUG_SCORES else None
        for st in order:
            sc, loc, m = self._match_state(gray, st)
            if scores is not None:
                scores[st] = sc
            if sc > best_sc:
                best_st, best_sc, best_loc, best_m = st, sc, loc, m
            if sc >= STRONG:
                break
        if scores is not None:
            print("    " + "  ".join(f"{k}:{v:.2f}" for k, v in sorted(scores.items(), key=lambda x: -x[1])))
        if best_sc >= ACCEPT and best_loc is not None:
            self.loc_cache[best_st] = best_loc
            if not self.locked:              # auto-calibrate: lock the winning scale multiplier
                self.locked = True
                self._build([best_m])
                self.loc_cache = {best_st: best_loc}
                print(f"  [calibrated] locked scale multiplier {best_m:.3f}")
            return best_st, best_sc
        return None, best_sc

# ===========================================================================
# DECISION LOGIC  (pure-ish, so it can be unit-tested)
# ===========================================================================
class BotState:
    __slots__ = ("last_acted", "last_act_t", "unknown_since", "last_escape_t")
    def __init__(self):
        self.last_acted = None
        self.last_act_t = 0.0
        self.unknown_since = None
        self.last_escape_t = 0.0

def react_timeout(state):
    return REACT_TIMEOUT_OVERRIDE.get(state, REACT_TIMEOUT)

def plan(state, now, bs):
    """Return 'FIRE' | 'ESCAPE' | 'WAIT'. Mutates bs.unknown_since only."""
    if state is None:
        if bs.unknown_since is None:
            bs.unknown_since = now
        if (now - bs.unknown_since) >= UNKNOWN_GRACE and (now - bs.last_escape_t) >= ESCAPE_DEBOUNCE:
            return "ESCAPE"
        return "WAIT"
    bs.unknown_since = None
    new_state = state != bs.last_acted
    persisted = (bs.last_acted is not None) and (now - bs.last_act_t) >= react_timeout(bs.last_acted)
    return "FIRE" if (new_state or persisted) else "WAIT"

# ===========================================================================
# CAPTURE  (dxcam primary, mss fallback)
# ===========================================================================
def make_capturer():
    try:
        import dxcam
        cam = dxcam.create(output_idx=DXCAM_OUTPUT, output_color="BGR")
        if cam is None:
            raise RuntimeError("dxcam.create returned None")
        for _ in range(120):                 # prime
            if cam.grab() is not None:
                break
            time.sleep(0.01)
        print("Capture backend: dxcam (DXGI)")
        return lambda: cam.grab()            # ndarray BGR, or None if no new frame
    except Exception as e:
        import mss
        sct = mss.mss()
        if MONITOR_INDEX >= len(sct.monitors):
            sys.exit(f"MONITOR_INDEX {MONITOR_INDEX} not available.")
        mon = sct.monitors[MONITOR_INDEX]
        print(f"Capture backend: mss  (dxcam unavailable: {e})")
        return lambda: np.asarray(sct.grab(mon))   # BGRA, always returns a frame

def to_gray_work(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY if frame.shape[2] == 4 else cv2.COLOR_BGR2GRAY)
    h = gray.shape[0]
    if h != WORK_H:
        sc = WORK_H / h
        interp = cv2.INTER_AREA if sc < 1.0 else cv2.INTER_LINEAR
        gray = cv2.resize(gray, (max(1, round(gray.shape[1] * sc)), WORK_H), interpolation=interp)
    return gray

# ===========================================================================
# MAIN LOOP
# ===========================================================================
def main():
    if not IS_WIN:
        sys.exit("This script must run on Windows (it drives the game via SendInput).")

    det = Detector()
    grab = make_capturer()
    print(f"Loaded {len(ALL_STATES)} templates. Classifying at {WORK_H}p, accept>={ACCEPT}.")
    print(f"Alt-tab into the game now. Starting in {START_DELAY:.0f}s.  (F12 to stop)")
    time.sleep(START_DELAY)
    print("Running.")

    bs = BotState()
    last_frame = None
    last_logged = "__init__"

    while True:
        if kill_requested():
            print("F12 - stopping.")
            return

        f = grab()
        fresh = f is not None
        if fresh:
            last_frame = f
        if last_frame is None:
            time.sleep(IDLE_SLEEP)
            continue

        now = time.perf_counter()
        established = bs.last_acted is not None
        selfheal_due = established and (now - bs.last_act_t) >= react_timeout(bs.last_acted)
        # Before the first action we always classify; afterwards, skip work when nothing
        # changed and no self-heal is due -> near-zero idle CPU.
        if not fresh and not selfheal_due and established:
            time.sleep(IDLE_SLEEP)
            continue

        gray = to_gray_work(last_frame)
        state, score = det.classify(gray, bs.last_acted)
        now = time.perf_counter()
        action = plan(state, now, bs)

        if action == "FIRE":
            fire(state)                       # <-- keypress first, no logging before it
            bs.last_acted, bs.last_act_t = state, now
            if state != last_logged:
                print(f"[{state}] {score:.2f}")
                last_logged = state
            if state == "collect_car":
                print("car successfully bought")
                return
        elif action == "ESCAPE":
            if last_logged != "unknown":
                print(f"[unknown] best {score:.2f} - backing out")
                last_logged = "unknown"
            press_escape()
            bs.last_escape_t = now
            bs.last_acted = None
        else:  # WAIT
            if state is None and last_logged != "unknown":
                print(f"[unknown] best {score:.2f} - watching")
                last_logged = "unknown"
            time.sleep(IDLE_SLEEP)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
