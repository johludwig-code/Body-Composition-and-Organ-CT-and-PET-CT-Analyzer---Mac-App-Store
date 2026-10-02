# ADR 0002: Where the code lives until it has its own repository

- Status: accepted, temporary
- Date: 2026-10-02

## Context

The owner asked for a fresh start that uses BOCARTA-MOOSE at most as a source.
No dedicated repository exists yet, and creating one is the owner's decision.

## Decision

The project lives in `BCOAnalyzer/` on the branch `claude/app-store-app-4eiy43`
of BOCARTA-MOOSE. Nothing inside it refers to a path outside it, and nothing
outside it refers to it. CI is in `BCOAnalyzer/.github/workflows/` and becomes
active the moment the folder is the root of its own repository.

## Consequences

Moving out keeps the history:

```bash
git subtree split --prefix=BCOAnalyzer -b bcoa-main
git push <new-repo-url> bcoa-main:main
```
