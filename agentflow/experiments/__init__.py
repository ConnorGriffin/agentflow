"""Offline Intake replay experiments (Wayfinder #2, #228).

A measurement prototype, not a production policy change: it replays a fixed, pinned
corpus of real issues through three Intake configurations and blind-scores the results
under the spend-per-success contract (ADR 0040, #227) to answer whether standard-first
Intake with typed deep escalation cuts spend without degrading quality. Nothing here runs
inside the daemon or touches live GitHub — the replay drives the durable Intake contract
(:func:`agentflow.intake.intake_prompt` / :func:`agentflow.intake.parse_intake`) offline.
"""
