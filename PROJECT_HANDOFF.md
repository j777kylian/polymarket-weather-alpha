# Polymarket Weather Alpha Handoff

## Objective

Research whether point-in-time ECMWF weather forecasts contain predictive information not incorporated into Polymarket weather prices.

This is NOT a trading bot.

Historical prices are descriptive only.

## Current branch

pre-freebuff-workflow

## Current state

Phase35B V2 protocol implemented.

Completed:
- CLOB recovery
- correction overlay
- V2 readiness contract
- provenance model

Current unfinished work:
- V2-aware freeze adapter acceptance

## Current blocker

freeze adapter requires:
- no fabricated V2 readiness in tests
- production CLI wiring
- corrected audit provenance binding

## Do NOT

- execute dataset freeze
- start Phase 4
- calculate alpha
- calculate PnL
- modify historical artifacts

