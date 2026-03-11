# Plan: Event-Driven Architecture Rewrite

**Status:** Not started
**Decided:** Session 24 (March 11, 2026)
**Context:** Diary Session 24, round three

---

## Why

The playback code scatters state across widget.py — `_api_cached_info`, `_last_api_poll`, `_api_cache_time`, `_using_api_fallback`, plus the button icon itself. These are mutated from `_on_play_pause`, `_update_media_info`, `_on_web_check_done`, `_apply_media_info`, and `mouseReleaseEvent`. Every patch creates a new edge case: optimistic UI updates fight API polls, the button flickers between states, seek interpolation jumps. Session 24 proved this can't be fixed with more if-else — the architecture needs to change.

## The design

**Event-driven with unidirectional state flow.** Observer pattern + centralized state store. Components never call each other directly — they publish and subscribe through an event bus.

```
  ┌──────────────┐         ┌──────────────┐
  │ SMTC Backend │──emit──>│              │<──emit──┌──────────────┐
  │  (producer)  │         │  Event Bus   │         │ Web API      │
  └──────────────┘         │              │         │ Backend      │
                           │ Events:      │         └──────────────┘
                           │  track_changed│
                           │  playback_state_changed│
                           │  position_updated│
                           │  user_action  │
                           │  backend_switched│
                           │  error        │
  ┌──────────────┐         │              │         ┌──────────────┐
  │   Widget     │<─listen─│              │─listen─>│ State Store  │
  │  (renderer)  │         │              │         │ (reducer)    │
  └──────────────┘         └──────────────┘         └──────────────┘
        │                                                  │
        │ user clicks ──> publishes user_action            │
        │                                                  │
        └──────────── listens to state_changed <───────────┘
```

## Example flows

### User clicks pause
1. Widget publishes `user_action(PAUSE)` on the bus
2. State store receives it -> sets `is_playing = False` -> sets `optimistic_lock_until = now + 5s` -> emits `state_changed`
3. Widget receives `state_changed` -> renders pause icon immediately
4. Active backend receives `user_action(PAUSE)` -> calls Spotify API / SMTC
5. Next backend poll returns real state -> state store receives it -> checks optimistic lock -> lock still active -> ignores stale data
6. Lock expires -> next poll updates state store with confirmed state -> emits `state_changed` -> widget renders

### Track changes on Spotify
1. SMTC backend detects change (via event callback, NOT polling) -> emits `track_changed(info)`
2. State store receives it -> updates title/artist/art/duration -> emits `state_changed`
3. Widget receives `state_changed` -> renders new track

### User presses play with no desktop Spotify
1. Widget publishes `user_action(PLAY)` on the bus
2. Manager receives it -> SMTC has nothing -> asks Web API backend to check (background thread)
3. Web API backend finds active session -> emits `track_changed(info)` + `backend_switched(web_api)`
4. State store receives both -> updates state -> emits `state_changed`
5. Widget renders track info

## File structure

```
playback/
    __init__.py
    events.py           # Event bus (pub/sub) + event type definitions
    state.py            # PlaybackState store — single source of truth, optimistic locking
    backend_smtc.py     # SMTC backend — uses WinRT event callbacks, no polling
    backend_web.py      # Web API backend — 5s polling, rate limit handling, interpolation
    manager.py          # Picks active backend (SMTC priority > Web API), handles switching
widget.py               # Pure renderer — subscribes to state_changed, publishes user_action
```

## Implementation notes

### SMTC event callbacks
`GlobalSystemMediaTransportControlsSessionManager` supports `current_session_changed` and sessions support `media_properties_changed`, `playback_info_changed`, `timeline_properties_changed`. Use these instead of the current 1-second polling. This makes local playback detection instant and eliminates ~1 request/second of wasted work.

### Optimistic locking
After any user action, the state store ignores backend updates for N seconds (5s for play/pause, 2s for next/prev). This is the fix for button flickering — not scattered timestamp arithmetic in five different methods.

### Web API backend encapsulation
Rate limiting (`.rate_limit` file), 5-second polling, progress interpolation between polls, thumbnail caching — all of this lives inside `backend_web.py`. None of it leaks into widget.py.

### Event bus implementation
A simple Python class — a dict of `{event_name: [callbacks]}` with `emit(name, data)` and `on(name, callback)`. Use Qt signals underneath for thread safety. Don't need an external library.

### Backend switching
When SMTC detects a Spotify session, it takes priority. When SMTC loses the session, manager checks if Web API has an active session (only if user previously opted in via play button). Emit `backend_switched` so the state store knows to reset.

## What NOT to do

- Don't make this a plugin system with auto-discovery. Two backends is enough. YAGNI.
- Don't create abstract base classes with `@abstractmethod`. Same method names, Python duck typing.
- Don't touch search, tray, fullscreen detection, or positioning. Those are UI concerns that stay in widget.py unchanged.
- Don't do this preemptively. Only when playback logic needs to change next.
