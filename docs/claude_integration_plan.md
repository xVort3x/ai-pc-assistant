Claude Integration Plan

Current Status

There are two plugin systems:



Claude Shell

Desktop UI

Search service

Plugin manager

Hotkey handling

Tray integration

Window management

DeepSeek Engine

Assistant engine

Router

Registry

Loader

Plugin system

Retrieval system

Decision:



Keep both systems unchanged.



Do not merge architectures.



Connect them with a bridge.



Core Decision

Use an adapter.



Do not rewrite:



assistant/

pcassistant/

Build a bridge between them.



Reason:



DeepSeek engine already has:



52 passing tests

Working plugins

Working retrieval

No benefit from rewriting.



Verified Findings

Assistant.ask()

Do not call on free text.



Reason:



If Gemini is configured, Assistant.ask() can send user text to the model.



This would:



increase API usage

increase latency

risk sending text unnecessarily

Assistant.execute()

Safe to use.



Used for:



file\_search

gp7\_search

homework\_search

downloads\_summarizer

grammar\_fixer

Prefix Detection

Use:



parse\_query(...).hint



Examples:



@files budget files: budget /gp7 notes



Returns the intended plugin.



Plain text returns None.



Claude Files To Keep Unchanged

Keep all files unchanged:



assistant/ main.py tests/



Only update:



requirements.txt



Add:



pypdf>=4.0



DeepSeek Files To Keep Unchanged

Keep unchanged:



pcassistant/



including:



assistant.py

registry.py

router.py

loader.py

config.py

storage.py

search.py

Keep unchanged plugins:



grammar\_fixer

gp7\_search

homework\_search

file\_search

downloads\_summarizer

Keep all DeepSeek tests.



Bridge Architecture

Create:



plugins/pcassistant\_bridge/



Containing:



plugin.json main.py



Create:



tests/test\_bridge.py



Startup Behavior

Bridge loads one:



pcassistant.assistant.Assistant



instance.



On unload:



assistant.close()



Free Text Guard

Inside search():



If:



parse\_query(...).hint == None



Return:



\[]



Never call Assistant.ask()



for plain text.



Supported Plugin Execution

Live Plugins

Can execute during search:



file\_search

gp7\_search

Reason:



Fast retrieval No AI required



Run-On-Enter Plugins

Only execute after Enter:



grammar\_fixer

homework\_search

downloads\_summarizer

Reason:



May call Gemini Can take longer



Row Mapping

Convert DeepSeek results into Claude rows.



Rules:



One row per action.



Title:



Action label



Subtitle:



Action target or text preview



Order:



Highest confidence first



Allowed Actions

Allow:



open

reveal

copy

Block:



run

If received:



Ignore or reject.



Never execute.



Activation Behavior

When a result is selected:



Run plugin in background thread.



Apply:



first copy action



or



copy plugin output text



Show notification containing:



PluginResult.title



Files To Merge

None.



Keep both architectures.



Files To Remove

None.



Do not remove:



assistant/ pcassistant/



Do not remove tests.



Final Structure

ai\_pc\_assistant/



├── main.py ├── requirements.txt ├── requirements-dev.txt ├── pyproject.toml ├── README.md │ ├── docs/ │ ├── ARCHITECTURE.md │ ├── PLUGIN\_SPEC.md │ └── claude\_integration\_plan.md │ ├── assistant/ │ ├── pcassistant/ │ ├── plugins/ │ ├── tests/ │ └── core files │ ├── plugins/ │ └── pcassistant\_bridge/ │ └── tests/



Build Order

Import DeepSeek code.

Run Claude tests.

Run DeepSeek tests.

Verify plugin discovery.

Configure Gemini.

Configure GP7 paths.

Configure homework paths.

Build bridge.

Run bridge tests.

Run shell.

Press F9.

Verify plugin functionality.

Dependencies

Python 3.13+



PySide6



pypdf>=4.0



pytest>=8



Gemini API key



MVP Scope

Included:



Desktop shell

Hotkey

Tray

Search

GP7 Search

Homework Search

File Search

Downloads Summarizer

Grammar Fixer

Excluded:



AI chat

Natural language routing

Detail panes

Packaging

Auto-start

Settings UI

Validation Status

Claude Tests: 25/25 PASS



DeepSeek Tests: 52/52 PASS



Plugins:



✅ downloads\_summarizer



✅ file\_search



✅ gp7\_search



✅ grammar\_fixer



✅ homework\_search



Gemini:



✅ configured



Config:



✅ working



GP7 Search:



✅ working



Homework Search:



✅ working



Remaining Work

Build:



plugins/pcassistant\_bridge/



Files:



plugin.json

main.py

test\_bridge.py

Then:



Run shell



Press F9



Verify DeepSeek plugins are available through Claude UI.

