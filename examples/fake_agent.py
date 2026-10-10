"""Trusted protocol example; no model calls or command execution."""
import json
import sys

context = json.loads(sys.stdin.read().split("UNTRUSTED TRIAL DATA:\n", 1)[1])
if not context["history"]:
    print(json.dumps({"command": "python3 -m venv .venv"}))
else:
    attempt = context["history"][0]
    items = []
    if attempt["exit_code"]:
        items.append({"id": "venv", "severity": "BLOCKER", "type": "PLATFORM",
                      "machine": context["machine"], "command": attempt["command"],
                      "expected": "A virtual environment", "actual": attempt["output"][:1000],
                      "doc_line": "README.md:unknown"})
    print(json.dumps({"done": True, "findings": items}))
