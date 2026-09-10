---
name: Bug report
about: Something pm2-mcp does that it shouldn't, or doesn't do that it should
title: ''
labels: bug
assignees: ''
---

## What happened

<!-- The behaviour you saw. If a tool returned {"ok": false, ...}, paste the whole dict. -->

## What you expected

## Reproduction

<!--
Which tool, with which arguments. If it involves a specific PM2 service, its `exec_mode`
and `status` from `list_services` are usually the two fields that matter.
-->

1.
2.

## Environment

| | |
|---|---|
| pm2-mcp version | <!-- see CHANGELOG.md, or `get_status` --> |
| Python version | |
| PM2 version | <!-- `pm2 --version`, or the `pm2_version` field from `get_status` --> |
| OS | |

## Anything else

<!--
Please DON'T paste raw `pm2 jlist` output or a full environment dump — both routinely
contain credentials. Service names, statuses and the specific fields involved are enough.
-->
