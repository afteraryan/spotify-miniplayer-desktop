# The Fullscreen Detection Rabbit Hole

**Date:** February 19, 2026
**Project:** Spotify Taskbar Player (PySide6 + Windows 11)

---

## The Problem

My Spotify player widget sits directly on the Windows 11 taskbar — always on top, always visible. But when I go fullscreen on a YouTube video or a game, the player stays there, floating on top of the fullscreen content. Worse, it sometimes causes the fullscreen to "blip" — opening and immediately closing, like the two are fighting each other.

The goal: detect when a fullscreen app is running and hide the player. Sounds simple. It was not.

I'm not a developer — I'm a tinkerer. I build this stuff with the help of Claude (an AI coding assistant). This blog is the story of how we went back and forth three times before getting it right.

---

## The Starting Point: `showCmd`

The existing code used `GetWindowPlacement()` to check `showCmd`. If the window was maximized (`showCmd == 3`), it skipped it — assuming maximized windows aren't fullscreen. Then it checked if the window covered the full screen with `GetWindowRect()`.

```python
wp = WINDOWPLACEMENT()
wp.length = ctypes.sizeof(WINDOWPLACEMENT)
ctypes.windll.user32.GetWindowPlacement(fg, ctypes.byref(wp))
if wp.showCmd == 3:  # SW_SHOWMAXIMIZED
    return False  # Skip maximized windows, they're not fullscreen
```

I figured out the problem: when a browser goes fullscreen, Windows *still reports* `showCmd == 3`. So the code was skipping browser fullscreen entirely. I told Claude the diagnosis and suggested using the DWM API instead.

---

## Attempt #1: DWM Extended Frame Bounds (Claude's Idea)

Claude went with `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`. The theory: this API returns the *visible* window bounds, stripping away Windows 11's invisible resize borders. A maximized window's visible bounds should stop at the taskbar, while a fullscreen window's bounds would cover the entire screen.

```python
DWMWA_EXTENDED_FRAME_BOUNDS = 9
rect = wintypes.RECT()
ctypes.windll.dwmapi.DwmGetWindowAttribute(
    fg, DWMWA_EXTENDED_FRAME_BOUNDS,
    ctypes.byref(rect), ctypes.sizeof(rect),
)
```

It explained the fix confidently. I tested it.

**My verdict:** *"nah not working, when I click on a maximized window, it closes."*

The player was now hiding whenever *any* maximized window was focused. Clicking on a maximized File Explorer made it vanish. It got worse, not better.

Turns out, `DWMWA_EXTENDED_FRAME_BOUNDS` returns the same full-screen rect for both maximized and fullscreen windows on Windows 11. It strips the invisible borders from both, making them indistinguishable. The API Claude picked specifically *removes* the one piece of information we needed.

---

## Time to Research

At this point I asked Claude: *"You want to do some web research to figure this out?"*

It did a deep dive — Raymond Chen's blog, Windows Terminal source, Microsoft docs, the whole lot. It came back with a finding that flipped the entire approach:

> **Use `GetWindowRect` — the "broken" one with invisible borders.** A maximized window overshoots the monitor rect by ~7px on each side. A true fullscreen window matches the monitor rect *exactly*. The "flaw" is the feature.

Counterintuitive, but it makes sense once you see it.

---

## Attempt #2: Right Logic, Still Broken

Claude wrote the new detection with three checks:

1. `SHQueryUserNotificationState` — catches D3D exclusive fullscreen (games)
2. `GetWindowRect` exact match against monitor rect — the core insight
3. Window style check (`WS_CAPTION` / `WS_THICKFRAME`) — fullscreen apps strip these

I tested it.

**My verdict:** *"nah, not hiding on clicking on maximized window, but not hiding on fullscreen as well"*

It wasn't detecting *anything* now. Maximized windows: fine (player stayed). But fullscreen: also fine (player also stayed). The detection was completely dead.

---

## The Diagnostic Script

Claude wrote a diagnostic script I could run to see exactly what Windows reports for different window states. The first version had a 5-second timer — I didn't switch to fullscreen fast enough and it captured the same File Explorer window twice. So Claude rewrote it to poll continuously, printing only when the focused window changes. Much better.

I ran it, switched between a maximized window and a fullscreen YouTube video, and got this:

### Maximized File Explorer:
```
WindowRect:  (-7, -7, 1543, 823)
MonitorRect: (0, 0, 1536, 864)
CAPTION=yes  THICKFRAME=yes
>> FULLSCREEN? no                    ✓ Correct!
```

### Fullscreen YouTube (Brave, F11):
```
WindowRect:  (0, 0, 1536, 864)
MonitorRect: (0, 0, 1536, 864)
CAPTION=NO   THICKFRAME=NO
>> FULLSCREEN? YES                   ✓ Correct!
```

The logic worked perfectly in the diagnostic script. So why was it broken in the actual widget?

---

## The 64-bit Bug

Claude spotted it: the diagnostic script set proper return types for Win32 functions:

```python
user32.GetForegroundWindow.restype = wintypes.HWND
user32.MonitorFromWindow.restype = wintypes.HANDLE
```

The widget code didn't have this. On 64-bit Windows, `GetForegroundWindow()` and `MonitorFromWindow()` return 64-bit handles, but ctypes defaults to 32-bit `c_int`. The handles were being silently truncated, `GetMonitorInfoW` was getting garbage, and the detection always returned `False`.

The irony: Claude wrote the diagnostic script correctly but forgot to do the same thing in the actual widget code. The diagnostic's success masked the production bug.

---

## Attempt #3: It Works! But...

With the return types fixed, Claude also realized another problem: even when detection worked, the old code just changed z-order (`HWND_NOTOPMOST`) instead of actually hiding the widget. That's a suggestion, not a guarantee — especially for `Qt::Tool` windows.

The fix was simple: just `self.hide()` when fullscreen is detected, `self.show()` when it exits. No z-order tricks.

```python
if self._is_fullscreen_active():
    if not self._hidden_for_fullscreen:
        self._hidden_for_fullscreen = True
        self.hide()
else:
    if self._hidden_for_fullscreen:
        self._hidden_for_fullscreen = False
        self.show()
```

I tested it. **It worked.** Maximized windows: player stays. Fullscreen YouTube: player hides. Fullscreen exits: player comes back.

But there was a ~2 second delay before the player disappeared.

---

## Making It Instant

I asked Claude what would happen performance-wise if we made the detection faster. The answer: basically nothing.

The delay existed because clicking YouTube's fullscreen button doesn't change the foreground window — it's the same browser, just resized. The foreground hook doesn't fire, so the app relied on a 2-second fallback timer.

The detection function makes about 5 Win32 API calls that take microseconds each. Dropping the timer from 2000ms to 250ms means 4 checks per second, less than 0.001% CPU. Feels instant, costs nothing.

Done.

---

## Summary: What Works on Windows 11

| Approach | Works? | Why / Why Not |
|---|---|---|
| `showCmd == SW_SHOWMAXIMIZED` | No | Browsers report maximized even in fullscreen |
| `DWMWA_EXTENDED_FRAME_BOUNDS` | No | Returns same rect for maximized and fullscreen |
| `GetWindowRect` exact match | Yes | Maximized overshoots by ~7px, fullscreen matches exactly |
| Window style check (`WS_CAPTION`) | Yes | Fullscreen strips chrome, maximized keeps it |
| `SHQueryUserNotificationState` | Partial | Catches D3D/presentation, misses borderless fullscreen |
| Actually hiding (not just z-order) | Yes | `HWND_NOTOPMOST` isn't reliable for Tool windows |

**The winning combo:** `SHQueryUserNotificationState` + `GetWindowRect` exact match + `WS_CAPTION`/`WS_THICKFRAME` style check + `self.hide()`/`self.show()` + 250ms poll timer.

---

## What I Learned

1. **AI gets it wrong sometimes.** Claude confidently shipped two broken fixes before we got it right. The DWM approach sounded great in theory but was exactly backwards. Trusting but verifying — testing every change myself — is what kept us moving forward.
2. **Push back and test.** Every time I said "nah, not working" and described what I saw, it gave Claude the feedback it needed to change course. If I'd just accepted the first fix, I'd still have a broken player.
3. **Diagnostic scripts are everything.** The moment we stopped theorizing and printed actual values from my system, the problem became obvious. One script, one run, problem solved.
4. **`GetWindowRect`'s "bug" is a feature.** The invisible borders that make it "inaccurate" for measuring windows make it *perfect* for detecting fullscreen. Counterintuitive and beautiful.
5. **Simple beats clever.** `hide()` and `show()` vs z-order manipulation. The boring solution just works.
6. **Ask about performance, don't assume.** I almost accepted the 2-second delay because I thought faster polling would be expensive. It wasn't. One question, one line change, instant response.
