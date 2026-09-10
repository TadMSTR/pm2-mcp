---
name: Feature request
about: Suggest a tool, field, or behaviour
title: ''
labels: enhancement
assignees: ''
---

## The problem

<!-- What are you trying to do that pm2-mcp makes hard or impossible? -->

## What you'd like

## What you've considered instead

<!--
Worth checking first: is this something `get_service` already returns and just isn't
documented? The raw `pm2 jlist` entry carries far more than the typed response exposes,
so "surface field X" is often a smaller change than a new tool.
-->

## Scope check

pm2-mcp deliberately does **not**:

- register new PM2 processes (`start_service` resumes an already-registered one)
- delete processes from the PM2 list
- authenticate callers — it is localhost-only by design (see `docs/threat-model.md`)

If your request needs one of those, say so explicitly — it's not an automatic no, but it
changes what the server is, so it needs its own discussion.
