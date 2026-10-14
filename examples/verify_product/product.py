"""Offline sample: user commands operate on one live counter instance."""
import json
import os
from pathlib import Path
import sys
import time

root = Path(os.environ['XDG_DATA_HOME']) / 'counter'
command = sys.argv[1]
if command == 'start':
    root.mkdir()
    (root / 'state.json').write_text(json.dumps({'pid': os.getpid(), 'value': 0}))
    while True:
        time.sleep(.1)
elif command == 'cleanup':
    if root.exists():
        (root / 'state.json').unlink(missing_ok=True)
        root.rmdir()
else:
    state = json.loads((root / 'state.json').read_text())
    os.kill(state['pid'], 0)
    if command == 'add':
        state['value'] += int(sys.argv[2])
        (root / 'state.json').write_text(json.dumps(state))
    elif command not in {'ready', 'read'}:
        raise ValueError('unknown command')
    print(json.dumps(state))
