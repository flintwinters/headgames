# Project guidance

## Motivation

Headgames tests the smallest credible form of direct analog EEG neurofeedback:
scalp potential differences become audible feedback without sampling or DSP in
the feedback loop. Optimize for a fast, understandable proof of concept using
available parts. Expose limitations and latency/filtering tradeoffs rather than
hiding them behind unnecessary complexity.

## Architecture and invariants

`headgames.kicad_sch` is the sole authoritative schematic. The externally
wired MVP uses a keyed 9 V isolated-battery input and three electrodes (`MEAS`,
`REF`, `BIAS`), an inventory-first LM324N for buffered mid-supply reference, AC-coupled
differential acquisition, bounded adjustable alpha-band gain with approximately
7.9-8.8 Hz high-pass and 10.7-12.4 Hz low-pass corner ranges, and envelope-controlled
audible oscillation, plus an LM386 for speaker output.
Each electrode enters through two independent series current-limiting
resistors: 100 kΩ + 100 kΩ on MEAS and REF, and 1 MΩ + 1 MΩ on BIAS.
These resistors do not relax the isolated-battery-only operating rule.
An LM358N precision peak detector converts filtered alpha amplitude into
`ENV`: U3A compensates the forward drop of D1 inside its feedback loop, R12/C9
hold the raw envelope relative to VREF, and U3B buffers that node before R16
controls U1D. D1 is a common 1N4148 small-signal silicon diode: the active
feedback loop compensates its forward drop, while its low reverse leakage keeps
the envelope release dominated by R12/C9. Do not substitute a leaky power
Schottky, which can materially alter the held envelope.

The analog feedback path must remain entirely analog. Future digital logging
must be electrically safe and outside that path. The LM324N is an
inventory-first compromise whose bipolar input bias, offset, and noise limit
the 10 MΩ acquisition network; preserve a clear replacement boundary for
future electrode-side buffers or an instrumentation amplifier, but do not make
an unavailable amplifier mandatory for the MVP.

`manage.py simulate-eeg` is the repeatable small-signal validation path. It
extracts component values from the authoritative schematic and models a 20 kΩ
electrode source per input. At midpoint trims it predicts about 2,433 V/V at
10 Hz and a broad 3.69-14.71 Hz -3 dB span, with a response maximum near
7.53 Hz. Treat this as evidence that the MVP has useful EEG-scale gain but weak
alpha selectivity, not as a substitute for noise, artifact, tolerance, or
isolated bench validation.

`manage.py simulate-artifacts` is the current project-survival test. Its
declared stress fixture combines 20 kΩ/100 kΩ electrode mismatch, 1 mV at 2 Hz
differential motion, 100 uV at 30 Hz differential muscle-like activity, and
100 mV at 60 Hz common mode. Adding 50 uV at 10 Hz changes the ideal detector's
mean envelope only 3.8%, below the provisional 25% distinguishability target.
The architecture therefore has an unresolved artifact-rejection problem;
validate the fixture amplitudes and response with an isolated physical phantom
before investing in downstream refinement.

`manage.py simulate-active-electrodes` evaluates unity buffers after both
electrode-side safety resistors without changing the authoritative schematic.
With declared 5 pF input, 100 Ω output, 1 MHz GBW, 10 pA bias, 25 nV/√Hz white
noise, and unequal 150/250 pF cables, buffering reduces the modeled 60 Hz
common-mode contribution from 0.686 V to 0.004 V at `ALPHA`. It cannot reject
the differential 2 Hz artifact, which reaches 1.000 V, so alpha changes mean
`ENV` only 2.8% and still fails the 25% target. Active electrodes solve a cable
and source-impedance problem, not the project-survival motion/selectivity
problem. A physical version must place both independent safety resistors ahead
of each electrode-site buffer and keep its entire worn supply isolated.

`manage.py simulate-sharper-filter` tests two unity-center-gain second-order
band-pass sections before the detector: 9.798 Hz center, Q=1.576 per section,
and ideal combined 8-12 Hz -3 dB limits. It passes the survival fixture: alpha
changes mean `ENV` by 565% with passive electrodes and 1,046% with active
electrodes; independent +/-2% center and +/-5% Q section corners remain above
502% and 908%. This establishes sharper selectivity as the leading fix, but the
model is not yet a buildable filter: synthesize physical passives and include
their tolerances, added noise, headroom, and overload recovery before changing
the authoritative schematic.

Acquisition impedance ratios are assembly invariants. Pair-match `R12/R22`
(10 MΩ), `C11/C15` (nominally 1.5 nF), `C12/C14` (100 nF), and `R15/R21`
(the reserved measured 474 kΩ pair). Inventory lacks 1.5 nF capacitors; the
preferred inventory-first substitute at each feedback location is one 1 nF C0G
capacitor in parallel with two series 1 nF C0G capacitors, giving exactly
1.5 nF nominally. Match the two three-capacitor totals and represent all six
physical capacitors in the authoritative schematic. A lone 1 nF substitution
is not equivalent: it moves the feedback low-pass corner from about 10.6 to
15.9 Hz. A 1 nF + 470 pF parallel pair is an acceptable lower-part-count
alternative when 470 pF C0G parts are available.

Anything conductively connected to electrodes must use an isolated wired DC
supply with no mains-earth or USB connection while worn. Remove grounded
scopes, non-isolated bench supplies, USB, chargers, powered audio, and other
mains-connected equipment before attaching electrodes. This prototype is not a
medical device.

## Current big tasks

- Implement the matched 474 kΩ input pair and matched three-capacitor 1.5 nF
  feedback networks in the authoritative schematic after inventory confirmation.
- Reproduce the artifact fixture with an isolated physical EEG phantom and
  determine whether alpha remains distinguishable under electrode imbalance.
- Synthesize the passing two-biquad filter into a one-dual-op-amp physical
  network, then model passive tolerances, noise, headroom, and overload recovery.
- Bench-validate power, VREF, carrier/audio, differential response, filtering,
  and envelope behavior with no person connected.
- Validate isolated-supply physiological pickup: artifacts first, then repeated
  eyes-open/eyes-closed alpha trials.
- Record observed limitations before adding input buffers, an instrumentation
  amplifier, sharper filters, or independent digital logging.

## Working method

Use installed KiCad library symbols and validate the native schematic with
`kicad-cli`. Preserve user layout changes and avoid parallel schematic sources.
Use the root Typer/Rich `manage.py` entrypoint for durable automated checks; do
not add ad-hoc test scripts. Reuse existing modules, keep safety annotations
conspicuous, and commit each verified logical checkpoint with a detailed
message.
