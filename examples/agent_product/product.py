"""Deliberately imperfect sample product, used only for the offline agent demo."""
import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument('operation', choices=['add', 'multiply'])
parser.add_argument('left', type=int)
parser.add_argument('right', type=int)
args = parser.parse_args()
# Deliberate sample bug: multiplication incorrectly adds its operands.
print(json.dumps({'result': args.left + args.right}))
