"""Read data/prompt_trace.jsonl in the terminal.

    py tools\\show_trace.py                 the last run
    py tools\\show_trace.py --all           every run in the file
    py tools\\show_trace.py --full          do not truncate long text
    py tools\\show_trace.py --masked        show what the endpoint received

No dependencies, no server, no UI: the whole point is that you can read the
prompts without setting anything up. TRACE=file writes the file; this prints it.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACE = os.environ.get("TRACE_FILE", os.path.join(HERE, "data", "prompt_trace.jsonl"))

ARGS = set(sys.argv[1:])
SHOW_ALL = "--all" in ARGS
FULL = "--full" in ARGS
MASKED = "--masked" in ARGS
WIDTH = 100

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, GREEN, YELLOW = "\033[36m", "\033[32m", "\033[33m"
if os.name == "nt":
    os.system("")          # let Windows terminals interpret the codes


def clip(text, width=WIDTH):
    text = " ".join(str(text or "").split())
    if FULL or len(text) <= width:
        return text
    return text[:width] + f"{DIM}…{RESET}"


def load():
    if not os.path.exists(TRACE):
        sys.exit(f"no trace at {TRACE}\n"
                 f"run the agent with TRACE=file first")
    with open(TRACE, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def runs(rows):
    """Split the file into runs: a prompt whose history is short starts one."""
    out, current = [], []
    for row in rows:
        if row.get("event") == "prompt" and len(row.get("messages") or []) <= 2:
            if current:
                out.append(current)
            current = []
        current.append(row)
    if current:
        out.append(current)
    return out


def show(rows):
    turn = 0
    for row in rows:
        event = row.get("event")

        if event == "prompt":
            turn += 1
            messages = row.get("messages") or []
            last = messages[-1] if messages else {}
            print(f"{BOLD}{CYAN}── turn {turn} ── {row.get('at','')}"
                  f"  ({len(messages)} messages){RESET}")
            print(f"  {DIM}latest {last.get('role','?')}:{RESET} {clip(last.get('content'))}")
            if MASKED and row.get("sent"):
                sent = row["sent"][-1]
                print(f"  {DIM}sent to endpoint:{RESET} {YELLOW}"
                      f"{clip(sent.get('content'))}{RESET}")

        elif event == "reply":
            secs = row.get("seconds")
            calls = row.get("tool_calls") or []
            head = f"  {GREEN}reply{RESET}"
            if secs is not None:
                head += f" {DIM}{secs}s{RESET}"
            print(head)
            if row.get("content"):
                print(f"    {clip(row['content'])}")
            for call in calls:
                args = json.dumps(call.get("args"), default=str)
                print(f"    {BOLD}→ {call.get('name')}{RESET} {clip(args, 80)}")
            print()

        elif event == "error":
            print(f"  \033[31merror:{RESET} {clip(row.get('error'))}\n")


def main():
    rows = load()
    grouped = runs(rows)
    chosen = grouped if SHOW_ALL else grouped[-1:]
    print(f"{DIM}{TRACE}  ·  {len(grouped)} run(s), showing "
          f"{'all' if SHOW_ALL else 'the last'}{RESET}\n")
    for i, run in enumerate(chosen, 1):
        if len(chosen) > 1:
            print(f"{BOLD}══ run {i}/{len(chosen)} ══{RESET}")
        show(run)
    if not MASKED:
        print(f"{DIM}(--masked shows what the endpoint actually received){RESET}")


if __name__ == "__main__":
    main()
