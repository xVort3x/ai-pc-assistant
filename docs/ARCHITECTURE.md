# AI PC Assistant: Architecture and Planning Review

Status: core shell (no plugins). Target: Windows 11, Python 3.11+, PySide6 6.6+.

## 1. Purpose

A Spotlight/Raycast-style launcher that lives in the system tray. Press **F9** anywhere, a search bar appears, you type, results from plugins appear, Enter runs one. The shell does nothing clever by itself; it is the *host* that later plugins (file search, class Q&A, grammar fixer, recording tools, LLM chat) plug into.

## 2. User workflow

1. App starts (tray icon only, no window).
2. F9 → search window appears centred on the active monitor, focused, previous text selected.
3. Typing is debounced (120 ms); matching plugins search in background threads; results stream in, sorted by score.
4. Up/Down select, Enter activates the result (plugin decides what that means), Esc or clicking elsewhere hides the window.
5. Tray menu: Show, Reload plugins, Open plugins folder, Quit.
6. Keyword routing: `kw something` sends the query only to the plugin that owns `kw`.

## 3. System architecture

```
 Win32 RegisterHotKey ──► HotkeyManager ──┐
 Tray icon / menu ────────────────────────┤
 2nd launch (QLocalSocket) ───────────────┤
                                          ▼
                                   SearchWindow (UI, main thread)
                                     │ query_changed        ▲ results_ready / started / finished
                                     ▼                      │
                                  SearchService ────────────┘
                                     │ debounce, generation id, cancel token,
                                     │ keyword routing, ThreadPoolExecutor
                                     ▼
                                 PluginManager ──► Plugin instances
                                     │ discovery (plugin.json + entry module),
                                     │ isolation (a broken plugin never kills the app),
                                     ▼ PluginContext (logger, data dir, settings, notify)
                                 Settings (JSON in %APPDATA%\AIPCAssistant)
```

Key decisions and why:

| Decision | Why |
|---|---|
| `RegisterHotKey` via ctypes + `QAbstractNativeEventFilter` | Zero extra dependencies, reliable, system-wide, no keyboard hook (so no antivirus false positives). A `keyboard`/`pynput` hook is heavier and flagged by AV tools. |
| Plugins run in a thread pool, results streamed back by Qt signals | UI never blocks. A slow plugin only delays its own results. |
| Generation id + cancel token per query | Stale results from an old query are dropped; cooperative cancellation for long searches. |
| Plugin = folder with `plugin.json` + `main.py` exposing `create_plugin(ctx)` | Simple, inspectable, easy for DeepSeek to generate one at a time. No packaging or entry points to maintain. |
| Manifest is the source of truth for id/name/keyword | The manager can list, disable and route without importing code. |
| JSON settings, atomic writes, corrupt file tolerated | One developer, no DB needed yet. Easy to hand-edit. |
| Single instance via `QLocalServer` | Second launch just shows the window. Prevents double hotkey registration. |
| In-process plugins (not subprocesses) | Simplest thing that works. Isolation is exception-level only; see risks. |

## 4. File structure

```
ai_pc_assistant/
├─ main.py                     entry point (CLI flags: --show, --verbose)
├─ requirements.txt / requirements-dev.txt
├─ README.md
├─ docs/ARCHITECTURE.md
├─ assistant/
│  ├─ app.py                   wiring / lifecycle
│  ├─ config.py                paths + Settings + PluginSettings
│  ├─ logging_setup.py
│  ├─ core/
│  │  ├─ plugin_api.py         Plugin, SearchResult, SearchRequest, PluginContext
│  │  ├─ plugin_manager.py     discovery, load/unload/reload, error report
│  │  └─ search_service.py     debounce, routing, threading, streaming
│  ├─ system/
│  │  ├─ hotkey.py             parse_hotkey + Win32 registration
│  │  └─ single_instance.py
│  └─ ui/
│     ├─ theme.py              the only place colours/sizes/fonts are defined
│     ├─ main_window.py        search bar + result list
│     └─ tray.py               tray icon, menu, notifications
├─ plugins/                    bundled plugins (empty for now)
└─ tests/
```

User plugins live in `%APPDATA%\AIPCAssistant\plugins\`.

## 5. Plugin contract (v1)

```
plugins/my_plugin/plugin.json   {"id","name","version","api_version":1,"entry":"main.py","keyword":"f"}
plugins/my_plugin/main.py       def create_plugin(ctx) -> Plugin
```
`Plugin.search(request)` runs on a **worker thread**, returns `list[SearchResult]`, should poll `request.is_cancelled()`. `Plugin.activate(result)` runs on the **UI thread** and must be quick; start your own thread for long work. `api_version` lets us evolve the contract without breaking old plugins.

## 6. Risks (identified before coding)

| Risk | Mitigation now | Later |
|---|---|---|
| F9 already taken by another app | Registration result checked; tray notification; hotkey configurable in `config.json` | Settings UI |
| Foreground-window rules stop the window taking focus | Hotkey press grants foreground rights; `raise_()` + `activateWindow()` | Win32 `SetForegroundWindow` fallback if testing shows misses |
| Plugin hangs a worker thread | Stale results ignored; pool is bounded | Per-plugin timeout, subprocess plugins for untrusted code |
| Plugin crashes in `activate` on UI thread | try/except + log + tray notice | none needed |
| Plugin import-time errors | Isolated per plugin, listed in `manager.errors` | "Plugin problems" tray entry |
| Two instances fighting over the hotkey | Single-instance guard | none |
| DPI / multi-monitor placement | Uses `availableGeometry` of the screen under the cursor | test on mixed-DPI setup |
| Packaging (PyInstaller) path differences | `config.app_root()` handles frozen mode | build script |

## 7. Testing plan

Automated (run anywhere, headless): `pytest` with `QT_QPA_PLATFORM=offscreen`
- hotkey string parsing
- plugin discovery: valid, broken import, bad manifest, wrong api_version, disabled, duplicate id
- search service: global fan-out, keyword routing, stale-generation drop, plugin exception isolation, empty query
- settings: defaults, corrupt file recovery, plugin-scoped settings

Manual on Windows 11 (checklist):
1. `python main.py` shows tray icon, no window.
2. F9 shows window on the monitor with the mouse; F9 again hides it.
3. Esc hides; clicking another app hides.
4. Tray → Quit exits cleanly and releases F9 (press F9 afterwards: nothing happens).
5. Launch twice: second launch shows the window of the first.
6. Drop a plugin folder in, Tray → Reload plugins, results appear.
7. Hold F9: no repeated toggling (MOD_NOREPEAT).

## 8. Future expansion

Settings dialog (hotkey, plugins on/off) · result icons and actions menu · plugin result history/ranking · background indexer service (SQLite) shared by plugins · LLM provider abstraction in `core/` · plugin permissions · subprocess plugins · PyInstaller/Inno Setup installer · autostart toggle.

## 9. Implementation order and who builds it

| Step | Work | Owner | Why |
|---|---|---|---|
| 1 | Architecture, plugin contract, roadmap (this doc) | Copilot (review) / Claude (draft) | Contract is the most expensive thing to change later |
| 2 | Core shell: config, plugin API/manager, search service, hotkey, tray, window, app wiring | **Claude Sonnet** | Desktop app, tray, Windows integration, GUI structure |
| 3 | Test suite for the above | Claude Sonnet | Written alongside the code it protects |
| 4 | Manual Windows 11 test pass, logs/screenshots back to the team | Developer | Only real hardware can verify the hotkey and focus behaviour |
| 5 | First plugins: file search, app launcher, calculator | DeepSeek (rapid) | Small modules against a stable contract |
| 6 | Wrap existing tools as plugins (grammar fixer, ask_class, ask_all_classes) | DeepSeek, reviewed by Claude | Existing scripts, thin adapters |
| 7 | Indexer service, settings UI, installer | Claude Sonnet | Databases, GUI, packaging |
| 8 | Roadmap, prioritisation, monetisation | Copilot | Product work |
