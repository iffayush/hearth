## What this changes and why

<!-- One or two sentences. Link the issue if there is one. -->

## How it was tested

<!-- Commands you ran, and on which OS. For network changes, say what you ran it against. -->

## Checklist

- [ ] `python -m pytest` passes, and new behaviour has a test
- [ ] `python -m bandit -q -r hearth` and `python -m pip_audit --skip-editable` are clean
- [ ] Any ambiguity I resolved is in `docs/decisions.md`
- [ ] Anything that changes a stored format or the wire protocol is called out above, with what happens to existing groups
- [ ] A security fix includes a test that fails without it
- [ ] No keys, passphrases, invite links or real addresses appear in code, tests, logs or this PR
