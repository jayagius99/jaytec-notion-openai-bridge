# JAYTEC Durable Memory — Interface and Reliability Preferences

## Interface direction

JAYTEC/Forge technical interfaces should be modern, restrained, premium-feeling
and highly legible rather than visually noisy.

Useful UI patterns:
- dark/black base;
- responsive status cards;
- live/interactive charts where useful;
- tables for exact state;
- expandable details;
- clear progress and gate visualization;
- visible worker/specialist/owner relationships;
- purposeful motion only.

A high-value Admin Hub panel is "Who is working right now?" showing:
driver -> canonical worker -> specialist calls -> objective -> current action ->
elapsed time -> fence/checkpoint -> latest evidence/artifact.

## Performance priorities

Reliability beats spectacle:
- stable frame pacing;
- low-latency controls;
- deterministic behavior;
- graceful degradation;
- robust error states;
- no screen tearing/stutter/visual glitches;
- avoid animation that harms responsiveness.

## Operational reliability

- fail closed at authority/security boundaries;
- preserve anti-duplication;
- bounded retry/backoff;
- explicit rollback/recovery;
- no silent provider or model substitution;
- exact live-state read before mutation;
- distinguish liveness from readiness;
- distinguish component readiness from whole-system readiness.
