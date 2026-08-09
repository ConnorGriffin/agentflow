# Codex-to-Claude child compatibility spike

Executed 2026-08-09 for [#509](https://github.com/ConnorGriffin/agentflow/issues/509).

## Command

```sh
codex exec -m gpt-5.6-sol --json --sandbox workspace-write --cd /path/to/agentflow \
  --ignore-user-config \
  'Run exactly: claude --version. Do not read, edit, create, delete, stage, or commit any repository file.'
```

## Result

The non-ephemeral Codex Sol parent executed `/bin/zsh -lc 'claude --version'` in the AgentFlow
worktree. The child exited `0` and reported `2.1.212 (Claude Code)`. The probe made no repository
changes. Its sanitized parent-launch envelope and completed child event are retained in
[the captured JSONL evidence](./evidence/codex-claude-child-probe-2026-08-09.jsonl).

## Executable contract

The repository-owned probe now runs Codex with `--json` and accepts success only after a completed
`command_execution` event proves that the configured Claude CLI ran `--version`. A nonzero Codex
launch or child exit remains nonzero; a zero Codex exit without usable completed-command evidence
fails closed.

## Limit

This establishes opposite-provider CLI delegation only. It does not prove native helper spawning,
helper usage attribution, or an end-to-end Build/Revise outcome.
