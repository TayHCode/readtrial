# Arithmetic CLI

This deliberately imperfect offline product has two short-lived commands. Each
prints a JSON object with a numeric `result`. No app needs to stay running.

- add: `python3 examples/agent_product/product.py add 2 3`
- multiply: `python3 examples/agent_product/product.py multiply 2 3`

Addition should return 5; multiplication should return 6. Multiplication contains
a deliberate defect so a verifier has a real mismatch to report.
