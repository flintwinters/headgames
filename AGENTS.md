# Project guidance

## Motivation

Headgames seeks the smallest credible direct analog EEG neurofeedback path:
scalp potential differences become audible without sampling or DSP. Favor a
fast, understandable proof of concept, expose limitations, and avoid complexity
that does not improve the core physiological experiment.

## Architecture and invariants

`headgames.kicad_sch` is the sole authoritative schematic. The isolated 9 V
MVP contains three electrodes (`MEAS`, `REF`, `BIAS`), matched AC-coupled
LM324N acquisition, adjustable alpha gain/filtering, an LM358N precision peak
detector with 1N4148 and 0.22 s release, analog carrier control, and an LM386
speaker stage. The analog feedback path must remain entirely analog; any future
logging stays electrically safe and outside it.

MEAS and REF each enter through two independent 100 kΩ series resistors; BIAS
uses two independent 1 MΩ resistors. These do not relax the isolated-battery
rule. While worn, remove grounded scopes, bench supplies, USB, chargers,
powered audio, and every mains-earth path. This is not a medical device.

Acquisition ratios are assembly invariants. Pair-match `R12/R22` (10 MΩ),
`R15/R21` (reserved measured 474 kΩ pair), `C12/C14` (100 nF), and `C11/C15`
(1.5 nF equivalent). Build each 1.5 nF feedback network from one 1 nF C0G in
parallel with two series 1 nF C0G parts; match the totals and show all physical
parts in the schematic. A 1 nF + 470 pF pair is acceptable when available; a
lone 1 nF is not equivalent.

The root `manage.py` is the sole verification entrypoint. Current architectural
evidence is:

| Model | Result |
|---|---|
| Existing path | 2,433 V/V at 10 Hz; broad 3.69–14.71 Hz −3 dB span |
| Artifact survival | Alpha changes mean `ENV` 3.8%, below the 25% target |
| Active electrodes | Mains improves 0.686→0.004 V, but differential motion remains; `ENV` changes 2.8% |
| Ideal two-biquad target | 9.798 Hz center, Q 1.576/section; ideal `ENV` changes 565% passive and 1,046% active; non-gating |
| Ideal coefficient perturbations | ±2% center, ±5% Q remain ≥502% passive and ≥908% active; non-gating |
| Physical MFB nonideal Python tier | 1,024 corners + 20,000 seeded builds retain ≥262.5% physical-detector `ENV` change but reach −2.346 V worst node margin; fail from LM324 bias/offset headroom |
| Required SPICE cross-check | Fail: ngspice 44.2 rejects TI Rev. C LMx58/LM2904 PSpice `IF()`/switch syntax; no agreement result exists |

Thus sharper selectivity remains the leading candidate, but the hardware gate
is closed. The proposed two-stage MFB network has strong modeled selectivity,
but finite LM324 bias/offset through the 10 MΩ acquisition network violates
headroom. The model now includes finite acquisition loop gain and a stateful
LM358/1N4148 detector; VREF, temperature, detailed acquisition transients, and
independent validation remain incomplete. The required TI-model SPICE check
also fails at simulator compatibility. Active
electrodes address cable/source imbalance but not motion; if pursued, both
safety resistors must precede every electrode-site buffer and the worn supply
must remain isolated. The LM324N remains an inventory-first compromise with a
clear future boundary for buffers or an instrumentation amplifier.

## Current big tasks

- Establish a compatible, source-locked independent simulation of the proposed
  MFB network and reconcile it with Python before considering a schematic edit.
- Resolve the LM324 acquisition bias/offset headroom failure or choose a
  credible input-stage boundary before further filter optimization.
- Extend VREF, temperature, trim spreads, deterministic phases, and detailed
  acquisition transient recovery, then independently validate the model.
- Implement the matched 474 kΩ pair and matched 1.5 nF networks after inventory
  confirmation.
- Reproduce the survival fixture with an isolated physical EEG phantom.
- Bench-validate power, VREF, carrier/audio, acquisition, filtering, and envelope
  with nobody connected; only then test isolated physiological pickup.
- Record observed limitations before adding buffers, instrumentation gain,
  independent logging, or other complexity.

## Working method

Use stock KiCad symbols and validate the native schematic with `kicad-cli`.
Preserve user layout changes and avoid parallel schematic sources. Put all
durable checks behind the Typer/Rich `manage.py`; do not create ad-hoc tests.
Reuse existing modules, keep safety annotations conspicuous, and commit every
verified logical checkpoint with a detailed message.
