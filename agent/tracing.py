"""Local prompt tracing: see every prompt, reply, tool call and timing.

LangSmith without the cloud. Two levels, both entirely on this machine -- no
account, no telemetry, nothing leaving the box, which is the whole point given
what is in these prompts.

  TRACE=file      append every model call to data/prompt_trace.jsonl (no deps)
  TRACE=phoenix   the above, plus Arize Phoenix's UI at http://localhost:6006

WHAT YOU SEE, AND WHERE -- this matters here more than in most projects.

The agent masks addresses and names before they reach the model. A LangChain
callback fires OUTSIDE that boundary, so a trace shows the REAL values: useful
for debugging the agent, but it means the trace file holds exactly what the
masking exists to protect. It stays local; do not ship it anywhere.

To see what the endpoint actually received -- the masked side -- set
TRACE_MASKED=1. Each entry then carries both, so a masking bug is visible as a
real value appearing in the `sent` half.
"""
import json
import os
import sys
import threading
import time
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

_LOCK = threading.Lock()


def _walk(value, fn):
    """Apply fn to every string in a nested structure. Keys are left alone."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: _walk(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, fn) for v in value]
    return value

TRACE = os.environ.get("TRACE", "").strip().lower()
TRACE_MASKED = os.environ.get("TRACE_MASKED", "").strip().lower() in ("1", "true", "yes")
TRACE_FILE = os.environ.get(
    "TRACE_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "prompt_trace.jsonl"))


def enabled() -> bool:
    return TRACE in ("file", "phoenix", "1", "true", "yes")


class FileTracer(BaseCallbackHandler):
    """One JSON object per model call: what went out, what came back, how long.

    Deliberately a flat file rather than a database -- it is meant to be read
    with a text editor when a run goes wrong, and thrown away afterwards.
    """

    def __init__(self, path: str = TRACE_FILE):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._started: dict = {}

    # ---- helpers -------------------------------------------------------
    def _write(self, entry: dict):
        entry["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with _LOCK, open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as e:                       # never break a run to log
            print(f"[trace] could not write: {e}", file=sys.stderr)

    @staticmethod
    def _messages(payload) -> list:
        """LangChain hands messages as a list of lists for chat models."""
        out = []
        for group in payload or []:
            items = group if isinstance(group, list) else [group]
            for m in items:
                entry = {"role": getattr(m, "type", "?"),
                         "content": str(getattr(m, "content", ""))}
                calls = getattr(m, "tool_calls", None)
                if calls:
                    entry["tool_calls"] = [
                        {"name": c.get("name"), "args": c.get("args")} for c in calls]
                out.append(entry)
        return out

    # ---- callbacks -----------------------------------------------------
    def on_chat_model_start(self, serialized, messages, *, run_id=None, **kwargs):
        self._started[str(run_id)] = time.time()
        entry = {"event": "prompt", "run": str(run_id),
                 "messages": self._messages(messages)}
        if TRACE_MASKED:
            # the same prompt as the endpoint will see it -- the two halves
            # side by side are what makes a masking bug obvious.
            #
            # EVERY string, not just the content: tool call arguments carry
            # addresses too, and masking only the prose here made this view
            # report a leak that the real request does not have -- a monitor
            # that cries wolf is worse than none, and the same blind spot
            # would have hidden a genuine leak in the arguments.
            try:
                from agent.llm import ip_mask
                mask = ip_mask.session_mask()
                entry["sent"] = _walk(entry["messages"], mask.mask)
            except Exception as e:
                print(f"[trace] could not mask the sent copy: {e}", file=sys.stderr)
        self._write(entry)

    def on_llm_end(self, response, *, run_id=None, **kwargs):
        started = self._started.pop(str(run_id), None)
        text, calls = "", []
        for generation in (response.generations or []):
            for gen in generation:
                message = getattr(gen, "message", None)
                text += str(getattr(message, "content", "") or getattr(gen, "text", ""))
                for call in (getattr(message, "tool_calls", None) or []):
                    calls.append({"name": call.get("name"), "args": call.get("args")})
        self._write({"event": "reply", "run": str(run_id),
                     "seconds": round(time.time() - started, 2) if started else None,
                     "content": text, "tool_calls": calls})

    def on_llm_error(self, error, *, run_id=None, **kwargs):
        self._started.pop(str(run_id), None)
        self._write({"event": "error", "run": str(run_id), "error": str(error)})

    def on_tool_end(self, output, *, run_id=None, name=None, **kwargs):
        self._write({"event": "tool_result", "tool": name,
                     "output": str(output)[:4000]})


def _start_phoenix():
    """Launch the Phoenix UI in this process and instrument LangChain.

    Everything stays on localhost: Phoenix keeps its traces in memory (or in
    its own local store) and talks to nothing outside the machine.
    """
    try:
        import phoenix as px
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from phoenix.otel import register
    except ImportError:
        print("[trace] TRACE=phoenix needs:  uv pip install arize-phoenix "
              "openinference-instrumentation-langchain", file=sys.stderr)
        return
    try:
        session = px.launch_app()
        tracer_provider = register(project_name="netops-agent",
                                   endpoint="http://localhost:6006/v1/traces")
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
        print(f"[trace] Phoenix UI on {session.url}", file=sys.stderr)
    except Exception as e:                            # never block the agent
        print(f"[trace] Phoenix failed to start: {e}", file=sys.stderr)


def callbacks() -> list:
    """The callback handlers for this run. Empty when tracing is off."""
    if not enabled():
        return []
    if TRACE == "phoenix":
        _start_phoenix()
    tracer = FileTracer()
    print(f"[trace] writing prompts to {tracer.path}"
          + ("  (both real and masked)" if TRACE_MASKED else ""),
          file=sys.stderr)
    return [tracer]
