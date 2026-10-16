# Proposals

A `proposal` is a solver's suggested change to the harness, with a diff as a ref under `shared/`. During a round you may read one or two pending proposals (`board_top(kind="proposal")`, then `Read` the ref) and vote on them with a reason. Merging arrives in M6 with `civ_merge`; until then a well-voted proposal is the record that the change is wanted, and the operator reads it.

You may post a proposal yourself when the succession policy or a blocker points at a prompt fault: name the file under `harness/prompts/` and the change in plain words, and attach the evidence seqs. A proposal must carry a ref; if you have no diff file to reference, put the change in the report's Diagnosis instead.
