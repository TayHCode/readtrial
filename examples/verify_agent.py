"""Offline protocol driver; plays a trusted plan, never calls a model.

A plan is a JSON list of the actions documented for verify mode. This script
asserts nothing about their meaning; readtrial records actual command output.
"""
import json
from pathlib import Path
import sys

context = json.loads(sys.stdin.read().split('UNTRUSTED TRIAL DATA:\n', 1)[1])
plan = json.loads(Path(sys.argv[1]).read_text())
if context.get('recipe') is not None:
    plan = [item for item in plan if 'recipe' not in item]
index = sum('agent_action' in row and 'recipe' not in row['agent_action'] for row in context['history'])
print(json.dumps(plan[index]))
