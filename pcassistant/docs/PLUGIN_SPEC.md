# Plugin Contract — v1

This is the stable interface for built-in and external plugins. Additive changes
are allowed; breaking changes require `api: 2`.

## Lifecycle

```
discover → construct(config) → can_handle(query, ctx)* → handle(query, ctx) → PluginResult
                                      │
                                      └─ health(ctx) may be called without executing
```

- Discovery imports modules; one broken plugin is reported in `--health` and the
  rest still load.
- Construction receives the global `Config`. Read plugin settings with
  `self.settings` (a dict) or `ctx.config`.
- `can_handle` is cheap and side-effect free. It is called for every enabled
  plugin on every non-explicit query.
- `handle` does the work. Raising is allowed; `Plugin.run` converts it to a
  failure result and logs the traceback.

## Manifest

```python
class MyPlugin(Plugin):
    id = "my_plugin"              # lower_snake_case, 2-41 chars, unique
    name = "My Plugin"            # display name
    description = "One sentence." # shown in help and --list
    aliases = ("mine",)           # @mine, /mine, mine:
    keywords = ("my thing",)      # default routing signal
    priority = 100                # higher wins near-ties
    enabled_by_default = True
    requires_network = False      # documentation flag; host does not block
```

Duplicate IDs are rejected. External plugins must be a single `.py` class that
subclasses `Plugin`; classes imported from elsewhere are ignored.

## Routing score

Return `0.0` to `1.0`.

- `< 0.30` is below the router threshold.
- `0.30–0.60` is a weak natural-language signal.
- `> 0.80` should mean a distinctive command pattern.
- Explicit prefixes (`@id`, `/id`, `id:`) bypass scoring entirely.

Do not claim broad vocabulary. If two plugins tie within `0.08`, `priority`
breaks the tie.

## Query

```python
@dataclass(frozen=True)
class Query:
    raw: str                  # exact user input
    text: str                 # prefix and --options removed
    hint: str | None          # explicit plugin id/alias
    options: dict[str, str]   # --tone=formal -> {"tone": "formal"}
    tokens: tuple[str, ...]   # stopword-filtered tokens
    attachments: tuple[str, ...]  # reserved
```

`--reindex` with no `=` becomes `{"reindex": "true"}`.

## Context

```python
@dataclass
class PluginContext:
    config: Config
    llm: LLMClient
    logger: logging.Logger
    cache_dir: Path
    deadline: float | None    # time.monotonic() value
    extras: dict

    def remaining(self) -> float | None: ...
    def check_deadline(self) -> None:  # raises TimeoutError
```

Long loops must call `ctx.check_deadline()` or compare `ctx.remaining()`. The
registry also enforces a wall-clock timeout, but a thread cannot be force-killed;
cooperative checks are what keep the process healthy.

## Result

```python
@dataclass
class PluginResult:
    plugin: str
    ok: bool = True
    title: str = ""
    text: str = ""
    citations: list[Citation] = []
    actions: list[Action] = []
    confidence: float = 0.0   # clamped to 0..1
    mode: str = "default"
    data: dict = {}
    error: str = ""
    elapsed_ms: int = 0
```

### `ok`

| Value | Meaning |
|---|---|
| `true` | The plugin ran. Empty results are still `true`. |
| `false` | Configuration error, timeout, rejection, or failure. |

Never use `ok: false` for "no matches". Set `data.found = false` instead.

### `confidence`

Calibrate honestly:

- `0.9+` exact filename / verified correction / grounded answer with strong retrieval
- `0.7–0.9` strong retrieval or AI-backed summary
- `0.3–0.6` weak retrieval, metadata fallback, mechanical cleanup
- `0.0` no result / help

### `mode`

Free-form but use existing values where possible: `ai`, `mechanical-fallback`,
`offline`, `search`, `grounded-answer`, `ai-digest`, `mixed-digest`,
`metadata-digest`, `help`, `default`. Hosts may render fallbacks differently.

### Citations

Every factual claim must point at something. Use `locator` for a timestamp,
section, page, or part number. `snippet` should be short and de-duplicated from
`text` where possible.

### Actions

| Kind | Target | Host behavior |
|---|---|---|
| `open` | file path or URL | `Start-Process` / browser |
| `reveal` | file or folder path | Explorer select |
| `copy` | text | clipboard |
| `run` | reserved | must be whitelisted; built-ins never emit it |

Plugins return actions; they do not perform UI work.

## Errors and fallbacks

- Catch `LLMUnavailable` / `LLMError` and degrade. Never show a raw stack trace.
- Prefer a labelled degraded result over a hard failure when the user can still
  get value.
- Reject oversized input before spending quota.
- Redact secrets. `LLMClient` already sanitises provider bodies and query strings.

## Caching

- Use `ctx.cache_dir`.
- Write with `pcassistant.storage.write_json_atomic`.
- Key by `path + size + mtime` (`file_signature`) and by model where summaries
  depend on the model.
- Cap entries; prune deterministically.
- Never cache a partial index as if it were complete.

## Health

`health(ctx)` must be cheap: check paths and read small cache metadata. Do not
scan or call the network. Return `Health(ok, detail)`.

## Configuration

Read defaults from `config.DEFAULTS` and access with:

```python
self.settings.get("my_setting", default)
ctx.config.get("paths.my_root")
ctx.config.bool("plugins.my_plugin.enabled", True)
```

Document every setting in `config.example.json` and the README. Invalid values
must fall back, not crash.

## Discovery

Built-ins: any module in `pcassistant/plugins/` defining a `Plugin` subclass.

External:

```powershell
python -m pcassistant --plugins-dir C:\plugins --list
```

Each file is imported under a unique module name. A duplicate ID is rejected
with a load report entry. External plugins share the process; treat them as
trusted code until subprocess isolation exists.

## Stable JSON

```
{"api": 1, "ok": ..., "plugin": ..., "title": ..., "text": ...,
 "citations": [...], "actions": [...], "confidence": ...,
 "mode": ..., "data": {...}, "error": ..., "elapsed_ms": ...}
```

`--list` returns `{"api": 1, "plugins": [...]}`. `--health` returns
`{"api": 1, "health": {...}}`.

Add fields freely; do not rename or remove existing ones without `api: 2`.

## Review checklist

- [ ] `id` unique, lower_snake_case
- [ ] `can_handle` tight; no broad word grabs
- [ ] `handle` respects the deadline
- [ ] empty/no-result path returns `ok: true` and a clear message
- [ ] AI failure has a labelled deterministic fallback
- [ ] citations on factual claims
- [ ] actions use only `open` / `reveal` / `copy`
- [ ] cache uses atomic writes and bounded entries
- [ ] no secrets or user text in logs
- [ ] tests for success, empty, and failure paths
- [ ] `health()` does not index
