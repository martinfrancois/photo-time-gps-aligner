# AGENTS

photo-time-gps-aligner is a Python command-line tool that corrects RAW camera clocks, geotags photos from GPX tracks or Google Timeline exports, and renames them in capture order, with verified recovery backups. [README.md](README.md) describes its commands, and [CONTRIBUTING.md](CONTRIBUTING.md) the rules for changes.

## Checks

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs these on Python 3.10 and 3.12, with ExifTool installed:

```sh
python3 -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
python3 -m compileall -q ./*.py examples
python3 -m unittest -v
```

It then runs the synthetic example from the README's "Synthetic example" section. Without ExifTool, the integration tests skip.

## Contributing for Someone Else

martinfrancois's rule for outside contributions, and for any agent working for an outside contributor: contributors shouldn't spend time on pull requests that collide with someone else's work or that he wouldn't accept. So a contribution takes one of three paths (see "Three ways to contribute" in CONTRIBUTING.md):

- Small fix (bug fix, typo, docs improvement or similar): open a pull request directly, no issue needed. If an issue for it exists, claim it first as below.
- Existing issue: check its assignees and linked pull requests, post `/take` on the issue, and start only once the assignment comes back. Never open a pull request for an issue that isn't assigned to the contributor you work for, and don't start on one that someone else holds or that an open pull request already addresses.
- New feature: open a thread in the Ideas category of GitHub Discussions. Wait until martinfrancois approves it and opens an issue, then claim that issue with `/take`. Never open a feature pull request without an approved issue assigned to your contributor.
