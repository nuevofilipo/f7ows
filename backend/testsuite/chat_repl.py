"""Terminal chat REPL — drives the real /messages endpoint, prints the strategy,
citations, and the trace dir each turn so you can open the gory detail.

Meta-commands (don't hit the LLM unless noted):
  /help                      show this
  /intent specific|inbetween|global|auto
  /budget <int>              retrieval token budget (per-request override)
  /k      <int>              top-k for specific retrieval
  /knobs                     show current knob overrides
  /new                       start a fresh chat (new chat_id)
  /reply <ltree_path>        branch your NEXT message under that message path
  /mockexam <topic>          run the /mockexam command
  /deepsummary <topic>       run /deepsummary (async; polls until ready)
  /quit
Anything else is sent as a normal grounded message.
"""
from __future__ import annotations

from testsuite.client import Api
from testsuite.config import RUNS_DIR


def _latest_run() -> str:
    runs = [d for d in RUNS_DIR.glob("run*") if d.is_dir()] if RUNS_DIR.exists() else []
    if not runs:
        return "(no run dir)"
    return str(max(runs, key=lambda d: d.stat().st_mtime).relative_to(RUNS_DIR.parent))


def _print_reply(resp: dict) -> None:
    am = resp.get("assistant_message", {})
    meta = am.get("metadata") or {}
    strategy = meta.get("strategy", resp.get("strategy", "?"))
    cited = resp.get("cited") or meta.get("cited") or []
    print(f"\n  ┌ strategy={strategy}  tokens={meta.get('retrieval_tokens', '?')}  path={am.get('path')}")
    for c in cited:
        path = c["path"] if isinstance(c, dict) else c
        print(f"  │  cited {path}")
    print("  └ answer:\n")
    print(am.get("content", "").strip() or "(empty)")
    if "exam" in resp:
        print(f"\n  [mockexam parsed {len(resp['exam'])} question(s)]")
    print(f"\n  trace: {_latest_run()}\n")


def run_repl(api: Api, project_id: str) -> None:
    chat_id = api.new_chat_id()
    knobs: dict = {}          # intent / scope held across turns
    overrides: dict = {}      # settings patch (budget / k)
    reply_to: str | None = None
    print(__doc__)
    print(f"project={project_id}  chat={chat_id}\n")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue

        if line in ("/quit", "/exit"):
            return
        if line == "/help":
            print(__doc__); continue
        if line == "/knobs":
            print(f"  intent={knobs}  settings={overrides}  reply_to={reply_to}"); continue
        if line == "/new":
            chat_id = api.new_chat_id(); reply_to = None
            print(f"  new chat: {chat_id}"); continue

        if line.startswith("/intent "):
            knobs["intent"] = line.split(maxsplit=1)[1].strip(); continue
        if line.startswith("/budget "):
            overrides["retrieval_budget"] = int(line.split(maxsplit=1)[1]); continue
        if line.startswith("/k "):
            overrides["retrieval_k"] = int(line.split(maxsplit=1)[1]); continue
        if line.startswith("/reply "):
            reply_to = line.split(maxsplit=1)[1].strip()
            print(f"  next message branches under {reply_to}"); continue

        command = None
        content = line
        if line.startswith("/mockexam "):
            command, content = "mockexam", line.split(maxsplit=1)[1]
        elif line.startswith("/deepsummary "):
            command, content = "deepsummary", line.split(maxsplit=1)[1]
        elif line.startswith("/"):
            print("  unknown command — /help"); continue

        try:
            resp = api.send_message(
                project_id, chat_id, content, command=command, parent_path=reply_to,
                intent=knobs.get("intent"), settings=overrides or None,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ! request failed: {exc}"); continue
        reply_to = None

        # deepsummary is async: poll the placeholder message until it fills in.
        if command == "deepsummary":
            mid = resp["assistant_message"]["id"]
            print("  …deepsummary running (polling)…")
            final = api.wait_for_message(project_id, chat_id, mid)
            resp["assistant_message"] = final
        _print_reply(resp)
