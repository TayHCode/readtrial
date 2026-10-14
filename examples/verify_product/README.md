# Local counter

A small offline sample for verify mode. It keeps one counter process alive and
uses the private XDG data directory supplied by the runner. No network is used.

Start with `python3 examples/verify_product/product.py start`. Check readiness
with `python3 examples/verify_product/product.py ready`. Add a number with
`python3 examples/verify_product/product.py add 3`, and read it with
`python3 examples/verify_product/product.py read`. Each response includes the
same running process identity. The runner owns and terminates the start process;
`python3 examples/verify_product/product.py cleanup` removes its counter files.
