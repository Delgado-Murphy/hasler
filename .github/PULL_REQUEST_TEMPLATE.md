## What and why

<!-- One change per pull request. What does it do, and why? Link an issue if there is one. -->

## What an owner would notice

<!-- "Nothing" is a fine answer. If something, add a line to CHANGELOG.md. -->

## How it was tested

- [ ] `daemon`: `.venv/bin/python -m pytest -q -n auto`
- [ ] `update`: `../daemon/.venv/bin/python -m pytest -q -n auto tests` (if touched)
- [ ] Browser tests near the change: `cd daemon && node tests/e2e/<name>.cjs` (all of them if `index.html` changed)
- [ ] `tools/checkin`: `node --test` (if touched)
- [ ] On a radio (say which cabinet and version), or not needed

## Checklist

- [ ] The doc that describes this changed with the code ([contributing](https://github.com/Delgado-Murphy/hasler/blob/master/docs/contributing.md#style)).
- [ ] The radio still works without the internet, Home Assistant or a phone.
- [ ] Owner-facing text is plain, short English.
