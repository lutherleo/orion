"""Orion's local web UI (`orion ui`, merge plan Phase 7): start, watch, stop and read scans from a browser.

Design of record: docs/superpowers/specs/2026-10-29-local-ui-design.md. The UI holds no scanning logic: every
scan it starts is the ordinary `orion scan` CLI in a child process, and every page reads only the files that
CLI writes under `.orion/runs/`.
"""
