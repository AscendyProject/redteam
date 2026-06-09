# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Report privately via GitHub's **"Report a vulnerability"** button on this repo's
[Security advisories page](https://github.com/AscendyProject/redteam/security/advisories/new)
(Security tab → Advisories). This opens a private channel with the maintainer.

Please include: what you found, how to reproduce it, and the impact you expect.
You'll get an acknowledgement as soon as possible, and we'll keep you updated on
the fix and disclosure timeline.

## Scope

redteam shells out to external model CLIs (`claude`, `codex`) and `python3`, and
its installer writes files into a consumer repo. Reports most relevant to this
project include: the installer overwriting or deleting files it shouldn't
(data-loss), the verification allowlist being bypassed so an LLM-authored
`outcome.md` can run arbitrary commands, a reviewer adapter gaining write
capability, or credentials leaking through adapter stderr.

## Supported versions

This is early software (0.x). Fixes land on `main` and in the next tagged
release; there is no long-term-support branch yet.
