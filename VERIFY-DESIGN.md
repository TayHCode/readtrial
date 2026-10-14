# Verification design ideas

As already attributed in [README.md](README.md#license-and-ideas), the source is
Lauren Tan's **create-verification-skill**, in **Cursor pstack**, published in the
public **cursor/plugins** project at
[pstack/skills/create-verification-skill/SKILL.md](https://github.com/cursor/plugins/blob/main/pstack/skills/create-verification-skill/SKILL.md).
The recorded licence is **MIT**, at
[pstack/LICENSE](https://github.com/cursor/plugins/blob/main/pstack/LICENSE).
These precise locations are repeated from this repository's existing attribution;
they were not fetched or independently checked during this change. A verifier
with network access can use them to compare the source and the mapping below.
The ideas are implemented independently here; no original text, prompts, code
or file structure was copied or vendored.

| Idea | Implementation in readtrial |
| --- | --- |
| Exercise the finished product through its user interface | `readtrial/verify.py`: `BRIEF` asks for documented user commands, and `verify` sends README, requirements and recorded outputs to a separate agent. `LocalProduct.command` executes those commands in a fresh committed-source copy. |
| Check usable readiness | `readtrial/verify.py`: `recipe_value` requires a readiness command for an app; `verify` runs bounded readiness retries before requirement commands. |
| Retain one owned app between steps | `readtrial/verify.py`: `LocalProduct.launch` owns one process group; `LocalProduct.healthy` enforces `keep_running` between actions. Short-lived CLIs use a not-applicable recipe. |
| Preserve action and result proof | `readtrial/verify.py`: `verify` records exact commands, outputs, proposed expected exits, actual exits, matches, policy refusals, superseded attempts with reasons, proof step selection, observation and lifecycle with `write_json`, and hashes each proof file. `successful_step` and `proof_supported` enforce matching active attempts and later replacements. `readtrial/verify_result.py`: `validate_result` checks result coverage, proof hashes and agreement with those details. |
| Reuse startup instructions | `readtrial/verify.py`: `verify` accepts a saved recipe wrapper, records source changes, retries readiness, and saves `recipe.json`. `LocalProduct.recipe_problem` checks saved commands before launch; unusable recipes are recorded as invalid and replaced through the agent. Actual readiness failure remains an honest failure. |
| Report gaps | `readtrial/verify.py`: `action` requires a reason for unverified observations and a nonempty final `not_checked` list. `verify` records unfinished requirements and failed attempts honestly. |
| Clean up owned state | `readtrial/verify.py`: `LocalProduct.stop` terminates its own process group; `verify` runs recipe cleanup and removes private copy/state in `finally`, including on errors. |

The local stand-in in `examples/standin_agent.py` demonstrates the agent path
without a model. Its small vocabulary cannot establish real semantic judgement.
These locations make our design statement inspectable within this repository.
Comparing it with the original skill requires someone with access to the original;
the offline demo and this note do not establish that comparison.
