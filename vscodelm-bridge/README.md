# LM Bridge

Serves the language models VS Code already has — GitHub Copilot's — as a local
**OpenAI-compatible endpoint**, so an agent can use them without an API key of
its own.

It exists because the tenant has Copilot but no model API. The netops agent's
alternative is `LLM_MODE=clipboard`, where a human pastes every prompt into
Copilot by hand. This replaces that with an HTTP call.

```
   netops agent            VS Code (this extension)         GitHub
  ChatOpenAI ──POST /v1/chat/completions──▶ vscode.lm ──▶ Copilot models
```

## One model, on purpose

This bridge answers with **gpt-4o-mini** and nothing else. A request naming a
different model is still answered by gpt-4o-mini, and the reply says so in its
`model` field.

That is a deliberate limit, not an oversight. An agent tuned against a small
model behaves differently on a large one, and a bridge that quietly swaps them
makes every result impossible to compare -- you would not know which model
produced the run you are looking at. Pinning also keeps the cost and the data
path predictable: one model, named in one place.

If Copilot is not offering gpt-4o-mini on your account, the bridge answers
`503` and names what it *is* offering rather than picking something else. To
change which model is pinned, edit `PINNED_MODEL` at the top of `bridge.js`.

## What you need

**GitHub Copilot Chat, installed and signed in.** `vscode.lm` has no models of
its own — it serves what other extensions register, and Copilot Chat is what
registers them. Without it the bridge starts and answers `503` with that
message. (`openai.chatgpt` does *not* register any: it contributes no
`languageModels`.)

## Install

```powershell
# link it into VS Code, then reload the window
New-Item -ItemType SymbolicLink `
  -Path "$env:USERPROFILE\.vscode\extensions\lm-bridge" `
  -Target "$PWD\vscodelm-bridge"    # run this from the netops-react folder
```

Or open this folder in VS Code and press **F5** to run it in an Extension
Development Host.

On startup it listens on **http://127.0.0.1:11434** and tells you how many
models it found. Commands: **LM Bridge: Status**, **List Models**, **Restart**.
Port is `lmBridge.port` in settings — 11434 is also Ollama's default.

## Point the agent at it

In the agent's environment:

```bash
LLM_MODE=api
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=gpt-4o-mini     # the only model this bridge serves
LLM_API_KEY=unused        # the bridge does not check it
```

Then run the agent as usual. No more clipboard pasting.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/chat/completions` | The one that matters. Tool calling supported. |
| `GET` | `/v1/models` | The one model it serves, with its token limits |
| `GET` | `/` | Readiness plus the model list |

```powershell
curl http://127.0.0.1:11434/v1/models
```

## The part that is easy to get wrong

`vscode.lm` does not have OpenAI's message shape, and tool calling is where
they diverge:

| OpenAI | vscode.lm |
|---|---|
| `system` role | no such role — sent as a User message |
| assistant `tool_calls[]` | `LanguageModelToolCallPart` **inside an Assistant message** |
| `tool` role result | `LanguageModelToolResultPart` **inside a *User* message** |
| `arguments` as a JSON string | `input` as a parsed object |

Putting a tool result in an Assistant message, or leaving arguments as a
string, makes the model answer as though the tool never ran — with no error to
tell you so. `test/test_bridge.js` pins each of those.

Because there is no system role, the agent's system prompt arrives as ordinary
user text. A model may weight it differently than a real system message.

## Tests

No VS Code needed — `test/fake-vscode.js` stubs the API with real classes, so
`instanceof` and the message shapes behave as they do in the editor.

```powershell
node test/test_bridge.js
```

25 checks: the tool-call round trip both ways, model listing and selection,
the no-provider case, and the error shapes.

To drive a *client* through the bridge, run the scripted stand-in — it replays
the agent's workflow without Copilot or a network:

```powershell
node test/serve_fake.js 11599
# then, in netops-react:
$env:LLM_MODE="api"; $env:LLM_BASE_URL="http://127.0.0.1:11599/v1"; .\run.ps1 -Mock
```

## Notes

- **Bound to 127.0.0.1 only.** Binding every interface would leave an
  unauthenticated door onto your Copilot seat, usable by anyone on the network.
- **No streaming.** Replies are returned whole. LangChain's `.invoke()` does
  not need streaming; a UI that wants tokens as they arrive would.
- **Copilot's terms still apply** to what goes through it, as does its rate
  limiting. The agent masks addresses and names before they leave the machine
  (`MASK_IPS`, `MASK_NAMES`), but that is the agent's doing, not the bridge's.
- Token counts come back as zeros: `vscode.lm` does not report usage.
