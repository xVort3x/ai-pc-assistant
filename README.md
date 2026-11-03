# AI PC Assistant (core shell)

Tray-resident launcher for Windows 11. Press **F9**, type, get results from plugins. This build contains the shell only; no plugins are included yet.

## Run

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py --show        # --show opens the window at startup; omit to start in the tray
```

Use `pythonw main.py` to run without a console window. Logs: `%APPDATA%\AIPCAssistant\assistant.log`. Settings: `%APPDATA%\AIPCAssistant\config.json` (hotkey, debounce, result count, disabled plugins).

## Keys

| Key | Action |
|---|---|
| F9 | show / hide the search window (configurable: `"hotkey": "Ctrl+Alt+Space"`) |
| Up / Down | move through results |
| Enter | run the selected result |
| Esc, or click elsewhere | hide |

## Develop

```powershell
pip install -r requirements-dev.txt
pytest
```

Architecture, risks, test plan and roadmap: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Plugin contract: `assistant/core/plugin_api.py`.
