"""Orion's dynamic-trace layer: run the target, record what actually happens at runtime, and feed
observed nodes/edges into the SAME scan graph as the static build.

This package is a second producer into the graph, never a mutation of the static one. Everything it
writes is stamped ``origin='dynamic'`` and lives under new labels (:ObservedMethod, OBSERVED_CALL,
OBSERVED_DISPATCH) that the static build's clear never touches. See
``docs/superpowers/specs/2026-08-27-dynamic-trace-layer-design.md``.
"""
