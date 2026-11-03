# AI PC Assistant — Plugin Core

A small, local-first assistant that routes a request to one of five plugins:

| Command | Plugin | What it does |
|---|---|---|
| `@grammar` | `grammar_fixer` | Corrects grammar, spelling, punctuation, tone |
| `@gp7` | `gp7_search` | Searches GP7 recordings/transcripts by timestamp |
| `@homework` | `homework_search` | Searches study notes; answers with citations |
| `@files` | `file_search` | Finds files/folders by name on Windows |
| `@downloads` | `downloads_summarizer` | Summarizes recent downloads |

The core is dependency-free on a stock Python 3.10+ install. `pypdf` is optional
for PDF text extraction.

---

## Quick start

```powershell
cd "C:\Users\kiank\OneDrive\Documents\Default Project"

python -m pcassistant --list
python -m pcassistant --health

python -m pcassistant "@files budget"
python -m pcassistant "@grammar: i has went too the store"
python -m pcassistant "@downloads"
python -m pcassistant "@gp7 cell membrane"
python -m pcassistant "@homework what is the difference between mitosis and meiosis"
```

Machine-readable output for another app:

```powershell
python -m pcassistant --json "@files budget"
```

Every response has the same envelope (`api: 1`):

```json
{
  "api": 1,
  "plugin": "file_search",
  "ok": true,
  "title": "Found 1 item",
  "text": "...",
  "citations": [],
  "actions": [{"kind": "open", "label": "Open budget.xlsx", "target": "C:\\...\\budget.xlsx"}],
  "confidence": 0.95,
  "mode": "search",
  "data": {},
  "error": "",
  "elapsed_ms": 174
}
```

`ok: false` means the plugin could not run (bad configuration, timeout, failed
load). An empty search is `ok: true` with `data.found = false` and a clear
message — "no results" is not an error.

---

## Configuration

Default location:

```
%LOCALAPPDATA%\PCAssistant\config.json
```

Override with `PCASSISTANT_CONFIG` or `--config`. Start from
[`config.example.json`](config.example.json).

Useful environment variables:

| Variable | Purpose |
|---|---|
| `PCASSISTANT_HOME` | Move config, cache, and logs |
| `PCASSISTANT_GEMINI_KEY` | API key without editing JSON |
| `PCASSISTANT_MODEL` | Override the primary model |
| `PCASSISTANT_DISABLE_AI` | Force offline/fallback modes |
| `PCASSISTANT_GP7_ROOT` | Recordings folder |
| `PCASSISTANT_DOWNLOADS` | Downloads folder |
| `PCASSISTANT_HOMEWORK_ROOTS` | `;`-separated study folders |

**Key reuse:** if `llm.api_key` is empty, the assistant reads the existing
Gemini key from the sibling `palette/config.json` (read-only). This avoids
asking for the same key twice. Nothing is written back to the palette config.

---

## Architecture

```
palette.ps1 / tray app / future UI
              │  JSON CLI (stable api=1)
              ▼
        pcassistant.cli
              │
          Assistant
        ┌─────┴──────────┐
      Router           Registry
        │                 │
   can_handle()      run() + timeout
        │                 │
        └──── plugins ────┘
              │
   config · llm · storage · extract · search · text
```

Design rules:

1. **Plugins are isolated.** A plugin exception becomes a structured failure,
   never a crashed assistant.
2. **Routing is explainable.** Explicit prefix first, then scored intent, with
   alternatives returned in `data.routing`.
3. **LLMs enhance, they do not gate.** File/transcript/note search is
   deterministic. Grammar has a mechanical fallback. Downloads has a metadata
   fallback.
4. **No invented answers.** Homework synthesis uses only retrieved excerpts and
   must cite them. If the notes do not cover the question, it says so and clears
   citations.
5. **Bounded work.** Every scan has a depth, entry, and time budget. Partial
   results are labelled partial.
6. **Caches are atomic.** A crash cannot leave a corrupt JSON cache.

### File structure

```
pcassistant/
  __init__.py          public models + version
  __main__.py          python -m entry point
  cli.py               JSON/human CLI, the bridge contract
  assistant.py         facade: ask(), execute(), health(), help
  router.py            prefix parsing, options, scored routing
  registry.py          plugin lifecycle, timeouts, health
  loader.py            built-in discovery + external .py plugins
  plugin.py            Plugin base class + PluginContext
  models.py            Query, PluginResult, Citation, Action, Health
  config.py            defaults, env overrides, path resolution
  llm.py               Gemini client: retries, fallback models, cache, redaction
  storage.py           atomic JSON, file signatures
  text.py              tokenizing, snippets, mechanical cleanup
  search.py            BM25 index, chunking, filename/path scoring, walker
  extract.py           TXT/MD/HTML/DOCX/PPTX/XLSX/PDF/subtitle extraction
  plugins/
    grammar_fixer.py
    gp7_search.py
    homework_search.py
    file_search.py
    downloads_summarizer.py
  tests/               51 unit tests, no network
  docs/PLUGIN_SPEC.md  contract for adding plugins
```

---

## Plugin designs

### 1. Grammar Fixer

**Purpose.** Turn rough text into correct text without changing meaning.

**User workflow.** `@grammar: teh cat sat on teh mat` → corrected text appears;
Enter copies it. `--tone=formal|concise|simple` adjusts register.

**Architecture.** Strip the trigger phrase → send only the passage to Gemini with
a strict copy-editor system prompt → clean fences → word-level diff for a change
count. If AI is disabled, absent, or fails, run conservative mechanical cleanup
(capitalisation, spacing, punctuation, terminal period) and clearly label it
`mechanical-fallback`.

**Failure behavior.** Oversized input is rejected before any network call.
Offline mode (`allow_network=false`) never sends text. API errors never leak the
key (redacted in the LLM client).

**Testing.** AI correction, mechanical fallback, offline mode, oversized input,
tone forwarding, trigger-phrase stripping.

**Expansion.** Local model provider, per-app tone presets, diff preview UI,
"apply to clipboard" action.

**Owner.** DeepSeek (prompt/system iteration), reviewed by Claude.

---

### 2. GP7 Search

**Purpose.** Find a moment in a recording by its transcript.

**User workflow.** `@gp7 cell membrane` → timestamped hits such as
`12:04 …the membrane controls…`, with actions to open the recording.

**Architecture.** Walk `paths.gp7_root`; parse `.srt`, `.vtt`, Whisper/JSON,
timestamped text, and plain text into `{start, label, text}` segments. Cache the
parsed index in `%LOCALAPPDATA%\PCAssistant\cache\gp7_index.json`, keyed by file
signature. Score segments by query coverage, phrase match, and filename match.
Link each transcript to a recording by matching stems.

**Failure behavior.** Missing root → actionable configuration message. No
transcripts → explicit "no transcripts found". No query match → `found: false`.
Index caps (1,500 files, 200k total segments) protect memory.

**Testing.** SRT timestamps, plain text, no-match, missing root, index reuse,
health reporting.

**Expansion.** Faster seek/open at timestamp via a player integration, speaker
diarization, semantic search over embeddings.

**Owner.** DeepSeek (parsers/search), Claude (index persistence review).

---

### 3. Homework Search

**Purpose.** Answer questions from the student's own notes, with proof.

**User workflow.** `@homework what is the difference between mitosis and meiosis`
→ a short answer with `[1]`, `[2]` citations, or the source passages when AI is
off.

**Architecture.** Index study folders (`.md`, `.txt`, `.pdf`, `.docx`, `.pptx`,
`.html`, `.json`) into a disk cache. Chunk at paragraph/heading boundaries. Rank
with BM25 plus title/section boosts. For question-shaped queries, send the top
excerpts to Gemini with a "use only these excerpts" system prompt, parse the
cited numbers, and return only the cited sources. Refusals clear citations.

**Failure behavior.** Missing folders → configuration message. No match → no
answer, no guessing. LLM failure → source-passage mode with a notice. Missing
`pypdf` → PDFs are skipped with a note, other formats still work.

**Testing.** Grounded answer + citations, search mode without AI, synthesis
failure degradation, no-match honesty, `synthesize=false`, refusal handling.

**Expansion.** Embedding retrieval, per-course filters (`in:biology`), flashcard
and practice-test sources, study-session history.

**Owner.** Claude Sonnet (retrieval/citation correctness).

---

### 4. File Search

**Purpose.** Find a file or folder by name fast enough to feel instant.

**User workflow.** `@files budget` → matching items with folder, type, size, and
modified time. `@files ext:pdf invoice`, `@files in:downloads recent:7d`,
`@files kind:folder taxes`.

**Architecture.** Bounded walk of Desktop, Documents, Downloads, and OneDrive
counterparts (or `paths.file_search_roots`). Index persisted to
`cache/file_index.json` with a TTL. Ranking: exact name > stem > prefix >
substring > all-token > acronym. Full-path matching is substring/token only — no
subsequence trap. Recent files get a small boost. Duplicate paths are removed.

**Failure behavior.** Time/entry budget exhaustion returns partial results with a
`partial scan` label. Unknown `in:` root lists available roots. No match is
explicit.

**Testing.** Name match, extension-only filter, folder-only filter, unknown root,
index persistence, no-match.

**Expansion.** Content search opt-in, Windows Search API backend, per-root
priority, "open containing folder" as a first-class UI action.

**Owner.** DeepSeek (index/search), Claude (cache and concurrency review).

---

### 5. Downloads Summarizer

**Purpose.** Tell the user what landed in Downloads without opening each file.

**User workflow.** `@downloads` → recent files, each with a one-line summary,
date, size, and an open action. `@downloads days:14`, `@downloads invoice`,
`@downloads all`.

**Architecture.** Scan direct children of Downloads, filter temp/partial files and
supported document types, sort by mtime. Extract text (TXT/MD/HTML/DOCX/PPTX/
XLSX/PDF when `pypdf` is installed). Summarize with Gemini using a strict
30-word system prompt. Cache summaries by `path + size + mtime + model` for 30
days. Without AI, produce a metadata + preview digest.

**Failure behavior.** Missing folder → clear error. No recent files → explicit
empty result. Extraction unsupported → metadata entry, not a fake summary.
Deadline near → skip remaining AI calls and mark partial. Cache writes are
atomic and capped at 500 entries.

**Testing.** AI digest, cache reuse without a second call, metadata fallback,
filename/day filters, empty folder, old-file exclusion.

**Expansion.** Recursive scan, ZIP contents listing, OCR for images, monthly
spend/duplicate detection, auto-delete suggestions (never automatic).

**Owner.** DeepSeek (extractors/caching), Claude (privacy review).

---

## Adding a plugin

Drop a file in `pcassistant/plugins/` or pass `--plugins-dir DIR`:

```python
from pcassistant.models import PluginResult
from pcassistant.plugin import Plugin


class ClipboardSearchPlugin(Plugin):
    id = "clipboard_search"
    name = "Clipboard Search"
    description = "Search recent clipboard history."
    aliases = ("clipboard", "clip")
    keywords = ("clipboard", "copied")
    priority = 90

    def can_handle(self, query, ctx):
        return 0.85 if "clipboard" in query.text.lower() else 0.0

    def handle(self, query, ctx):
        return PluginResult(
            plugin=self.id,
            title="Clipboard",
            text="...",
            confidence=0.7,
            mode="search",
        )

    def health(self, ctx):
        from pcassistant.models import Health

        return Health(True, "Ready.")
```

Rules:

- One `handle()`; raise only if you want the host's failure wrapper.
- Respect `ctx.deadline` in loops (`ctx.check_deadline()`).
- Return citations for any factual claim; return actions instead of opening files
  yourself.
- Keep `can_handle` tight; a plugin that claims everything starves the router.
- Use `ctx.cache_dir`, never a hard-coded temp path.
- Do not log user text or API keys.

Full contract: [`docs/PLUGIN_SPEC.md`](docs/PLUGIN_SPEC.md).

---

## Router behavior

| Input | Result |
|---|---|
| `@files budget` | explicit `file_search` |
| `/grammar: ...` | explicit `grammar_fixer` |
| `grammar: ...` | explicit `grammar_fixer` |
| `fix my grammar: ...` | scored `grammar_fixer` (0.94) |
| `summarize my recent downloads` | scored `downloads_summarizer` |
| `find the budget file` | scored `file_search` |
| `what is a monad` | no plugin → general AI answer (or help without a key) |

Options are `--key=value` (`--tone=formal`, `--ext=pdf`, `--limit=5`) or inline
filters (`ext:pdf`, `in:downloads`, `recent:7d`, `kind:folder`, `days:14`).

---

## Testing

```powershell
python -m unittest discover -s pcassistant/tests -t . -v
```

Current suite: **52 tests**, all offline. It covers contracts, routing, plugin
isolation, all five plugins, caching, rejection paths, and honest no-result
behavior.

Manual smoke checks:

```powershell
python -m pcassistant --list
python -m pcassistant --health
python -m pcassistant "@files ext:pdf"
python -m pcassistant --json "@downloads"
$env:PCASSISTANT_DISABLE_AI=1
python -m pcassistant "@grammar: i has went home"
```

Not automated: a live Gemini call, a real PDF with `pypdf`, and WinForms
rendering. Test those with a small real file after configuring the key.

---

## Integrating with the existing palette

The palette keeps its hotkey and UI. Add one command that shells out to the
stable JSON contract:

```powershell
function Invoke-PcAssistant([string]$Query) {
    $root = "C:\Users\kiank\OneDrive\Documents\Default Project"
    $json = & python -m pcassistant --json $Query 2>$null
    if (-not $json) { return $null }
    return ($json | ConvertFrom-Json)
}

$result = Invoke-PcAssistant "@files budget"
if ($result.ok) {
    $result.title
    $result.text
    foreach ($citation in $result.citations) { "  [{0}] {1}" -f $citation.locator, $citation.source }
}
```

Render `actions` by kind: `open` → `Start-Process`, `reveal` →
`explorer.exe /select,"<target>"`, `copy` → `Set-Clipboard`. Do not auto-execute
`run` actions.

Why a subprocess and not a DLL: process-per-query is trivial to recover, cannot
hang the palette's UI thread, and lets a plugin crash without taking down the
hotkey. Move to a local HTTP service only if measured latency becomes a problem.

---

## Privacy and security

- App/file/transcript/note searches stay on disk.
- Only these leave the machine: grammar passages, homework excerpts needed for
  synthesis, and download text needed for summaries. Each is opt-out via
  `plugins.*.enabled` / `allow_network` / `synthesize`.
- The API key is redacted from logs and error messages.
- Actions are data, not commands. The host whitelists `open`, `reveal`, `copy`.
- LLM output is never executed.
- Cache files are local JSON under `%LOCALAPPDATA%\PCAssistant\cache`.

---

## Known risks and trade-offs

| Risk | Current mitigation | Next step |
|---|---|---|
| Thread timeout cannot hard-kill a stuck plugin | Two-layer deadline; plugins check `ctx.deadline` | Subprocess isolation for untrusted plugins |
| First scan of a huge tree is slow | Depth/entry/time caps; disk cache; partial label | Background indexing + progress events |
| PDF text needs `pypdf` | Clear note; other formats still work | Bundle optional dependency in installer |
| Gemini quota/outage | Deterministic search; mechanical/metadata fallbacks; response cache | Local model provider |
| Retrieval misses paraphrases | BM25 + section/title boosts | Embedding index behind the same `SearchIndex` API |
| JSON index can grow | 20k file cap, 20k chunk cap, 200k segment cap, 500 summary cap | SQLite with migrations |
| Borrowed palette key surprises users | Read-only, documented, config override wins | Explicit "import key" action |

---

## Implementation order used

1. Contracts, config, LLM client, text/search/extract utilities.
2. Registry, loader, router, assistant facade, CLI.
3. `file_search` (validated the index and cache path).
4. `grammar_fixer` (validated the LLM and fallback path).
5. `gp7_search` (validated subtitle/JSON parsing and timestamps).
6. `homework_search` (validated chunking and grounded citation behaviour).
7. `downloads_summarizer` (validated extraction and summary caching).
8. Tests, docs, palette bridge example.

## Ownership

| Area | Owner |
|---|---|
| Contracts, registry, router, assistant | Claude Sonnet |
| File search / GP7 parsers | DeepSeek |
| Grammar prompts and plugin | DeepSeek |
| Homework retrieval and citations | Claude Sonnet |
| Downloads extraction and caching | DeepSeek |
| Integration, review, roadmap | Microsoft Copilot |
