# Project guidance

## Motivation

Headgames exists to test the smallest credible form of direct analog EEG
neurofeedback: scalp potential differences should become audible feedback with
no sampled-data system in the feedback path.  Prefer an understandable,
battery-powered experiment whose limitations are explicit over apparent
precision or complexity that cannot yet be validated.

## Architecture

The MVP is a three-electrode, single-supply analog signal chain.  An LM324N
provides the virtual ground, AC-coupled differential acquisition, alpha-band
gain/filtering, and an audible carrier.  A passive detector derives alpha
amplitude and a diode modulates the carrier.  An LM386M drives the audio
transducer.  Every conductive connection to the wearer remains inside the
battery-powered enclosure.  The design documentation and the calculations in
`manage.py` are jointly authoritative; calculated claims in the documentation
must be represented by named circuit parameters in the verifier.

The LM324 front end is deliberately an inventory-first compromise, not a
precision instrumentation amplifier.  Its documented replacement boundary is
the electrode-input/differential block; later buffers or an instrumentation
amplifier must preserve the downstream signal reference and alpha-filter
interface.

## Current big tasks

- Establish and bench-validate the LM324N/LM386M alpha-sonification MVP without
  a person connected.
- Validate battery-only physiological acquisition, beginning with artifacts
  and proceeding to the eyes-open/eyes-closed alpha comparison.
- Record observed limitations before choosing whether electrode buffers or a
  proper instrumentation amplifier are the next justified refinement.

## Working method

Use the root `manage.py` Typer application as the obvious entry point for all
repeatable calculations and checks.  Add durable checks there rather than
using ad-hoc test snippets.  Commit each verified logical checkpoint with a
detailed message.  Keep safety boundaries conspicuous in both schematics and
procedures.
