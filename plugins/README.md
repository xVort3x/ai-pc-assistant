# Bundled plugins

Empty for now. Each plugin is a folder:

```
plugins/my_plugin/
  plugin.json   {"id": "my_plugin", "name": "My Plugin", "version": "1.0.0",
                 "api_version": 1, "entry": "main.py", "keyword": "my"}
  main.py       def create_plugin(ctx): return MyPlugin(ctx)
```

See `assistant/core/plugin_api.py` for the contract. Plugins you install yourself go in
`%APPDATA%\AIPCAssistant\plugins\` (tray menu > Open plugins folder).
