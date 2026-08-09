# Project guidance

## Motivation

Headgames seeks the smallest credible direct analog EEG sonification path:
scalp potential differences become audible continuously, without sampling,
DSP, analysis windows, peak hold, or envelope release. Favor low latency and
an understandable proof of concept; measure analog group delay rather than
calling the circuit delay-free. Expose limitations and avoid complexity that
does not improve the physiological experiment.

## Architecture and invariants

`headgames.kicad_sch` is the sole authoritative schematic. The intended worn
path is wet active electrodes, differential acquisition, selected low-delay
alpha weighting, direct bipolar control of the roughly 700 Hz U2D relaxation
oscillator, the gain-20 LM386 stage, and speaker current. The current native
schematic implements the same selected path, including the two LM358N active
buffers, independent 8.2 kΩ cable isolation, the five-wire assembly boundary,
explicit continuous MEAS/REF wiring through those buffers, and direct
ALPHA-to-U2D control through a 180 kΩ R6 floor plus the series 25 kΩ RV3
rheostat, limiting adjustment to 180–205 kΩ. Core signal-path continuity must not be hidden behind
same-sheet label jumps. Model acceptance remains closed because the
nominal noisy end-to-end experiment fails its alpha-modulation gate.

The broadband redesign is currently model-only. It proposes gentle nominal
1 Hz/30 Hz edges in the existing differential stage, a flat VREF-centered U2C
gain stage, and one Q≈8 active 60 Hz notch before U2C. The native schematic
still contains the historical alpha network and must not be retuned until a
qualified campaign selects physical values. Model code may describe both
circuits, but that does not make the proposal authoritative hardware.

MEAS and REF each pass through two independent 100 kΩ resistors before the
electrode-site LM358N buffer. BIAS uses two independent 1 MΩ resistors. The
active assembly boundary has exactly five conductors: buffered MEAS, buffered
REF, BIAS, isolated VCC, and isolated return. The entire worn system remains
battery-isolated. Remove grounded scopes, bench supplies, USB, chargers,
powered audio, and every mains-earth path while worn. This is not a medical
device.

Acquisition ratios remain assembly invariants. Pair-match `R12/R22` (10 MΩ),
`R15/R21` (reserved measured 474 kΩ pair), `C12/C14` (100 nF), and `C11/C15`
(1.5 nF equivalent). Each 1.5 nF network is one 1 nF C0G in parallel with two
series 1 nF C0G parts; show every physical part. A stocked 1 nF + 470 pF pair
is acceptable; a lone 1 nF is not equivalent.

The previous sonification-frontier outputs are invalid and must not guide the
circuit. They did not execute their advertised build count or phase sweep,
used pre-oscillator proxies instead of the declared speaker-current endpoint,
and did not propagate the complete simultaneous stimulus through the physical
path. Do not cite the reported Pareto knee, group delays, modulation ratios,
sidebands, or candidate selection as design evidence. The TI cable transient
is a limited block-level result, not end-to-end sonification validation.

Current limited block-level evidence is:

| Model | Result |
|---|---|
| Stateful oscillator prototype | Produces a nominal carrier near 682 Hz; 17 selected-path builds did not latch, but negative modeled node margin occurs under declared spreads |
| LM386 behavioral prototype | Uses gain 20, 50 kΩ input, 300 kHz bandwidth, conservative 250–325 mW clipping bounds, and 8 Ω ±10% load; 0/17 builds clipped, but TI supplies no LM386 device model so distortion/current-limit/recovery claims remain unresolved |
| TI cable transient | Actual 8.2 kΩ ±5% and 150/250 pF corners pass the nominal TI macro-model with 0.0% overshoot and 8.9 µs worst settling; phase margin remains unresolved because valid loop-break fixtures do not converge and TI supplies no process/temperature corners |
| End-to-end nominal wet/alpha run | Executes all 16 requested phases at the speaker-current endpoint, but fails: worst alpha/carrier modulation is 0.24% versus the 1% gate |
| Selected wet/alpha spread run | Deterministically executes 17 builds × 16 phases; all 17 fail alpha modulation, worst alpha/carrier is 0.09%, peak modeled output/load current is 128.1 mA, and minimum modeled node margin is −0.949 V |
| Alpha redesign comparison | An identical 17-build × 16-phase experiment for each of 3 weightings × 3 R6 values finds only the historical two-MFB cascade with 68 kΩ or 100 kΩ passes current behavioral gates; its worst modeled 8–12 Hz group delay is 139.4 ms, and this is not a hardware selection |
| Broadband redesign checkpoint | An identical 2-build × 16-phase endpoint experiment for 5 gain-feedback values × 3 R6 values reports every 1–30 Hz wanted tone and 35–100 Hz rejection tone separately. The realizable common-value twin-T model uses repeated 390 kΩ and 6.8 nF parts plus a 20 kΩ/620 kΩ Q divider. Several rows pass the current behavioral gates; worst passing-band delay above 4 Hz is 12.4 ms and modeled 59/60/61 Hz rejection is at least about 17/40/17 dB. This is not a selection: 0.1–0.5 Hz is AC-only, qualified notch/op-amp device corners are absent, and isolated bench evidence is absent. |

The redesign result is deliberately fail-closed. Its MFB networks currently
depend on a stale, non-authoritative BOM, require two additional amplifier
sections, and do not yet have complete leaf-level/device spread coverage.
Neither passing row may be implemented in KiCad until those facts are resolved.

The LM386 model is bounded by its official gain-20, 50 kΩ input, and 300 kHz
bandwidth characteristics. Its nonlinear output claims remain bench-gated.
Wet electrodes gate; dry-electrode reports are informational. The detector
and two-MFB implementations are historical evidence, not hardware candidates.

## Current big tasks

- Establish loop-return phase margin for the selected 8.2 kΩ cable isolation;
  the available TI macro-model cannot provide this answer, so new qualified
  device-corner models or an eventual loop-injection measurement are required.
- Expand the genuine end-to-end electrode-to-speaker-current experiment beyond
  the current 17-build deterministic spread checkpoint.
  Every requested seeded build and phase combination must exercise every
  relevant independent stocked part and the complete simultaneous stimulus.
- Select the weighting topology and synthesize `R6` only after that valid
  experiment produces a reproducible frontier. Do not implement a candidate
  in KiCad based on the invalid nominal knee.
- Expand the broadband checkpoint beyond two builds, add qualified notch/op-amp
  device corners, and
  exercise 0.1–0.5 Hz at the stateful endpoint before selecting its U2C gain,
  R6, or schematic values. The current command deliberately exits fail-closed.
- Extend characterization across VREF, supply, noise, slew, swing, clipping,
  onset/offset, and carrier-edge response. The attempted TI VREF transient
  fixture was removed because the vendor macro-model could not establish its
  operating point.
- Bench-validate the electrode-input-to-speaker-current path with an isolated
  phantom and nobody connected before any physiological test.

## Working method

### Simulation integrity

A simulation exists to produce evidence that can support a stated decision.
If it cannot do that, do not present it as validation. A plausible-looking
number from an incomplete experiment is actively harmful because it creates
false confidence.

- Every command must execute every option and contract it advertises. Never
  accept or print a sample count, phase count, tolerance range, stimulus, or
  gate that the implementation did not actually exercise.
- Measure acceptance at the declared physical endpoint. Do not silently
  replace speaker-current modulation with a filter-node proxy, complete-path
  stability with a block-level transient, or physical behavior with an ideal
  transfer function.
- Propagate the complete declared stimulus through the complete declared path,
  including simultaneous signals, state, nonlinearities, loading, clipping,
  recovery, supply/VREF conditions, and independently moved physical parts
  wherever those effects are in scope.
- Make omissions, idealizations, model boundaries, and bench-gated claims
  explicit in both code and output. Analytical and block-level models may be
  useful, but they must be labeled accurately and cannot claim end-to-end
  circuit validation.
- Fail closed when a model, dependency, cross-check, or requested coverage is
  missing. Do not emit a selection, acceptance verdict, Pareto frontier, or
  other decision-shaped output from incomplete evidence.
- Durable tests must prove requested case counts, deterministic replay,
  independent tolerance movement, phase coverage, stimulus coverage, endpoint
  measurement, rejection gates, and deliberate failure of incomplete runs.
- Apply identical experiments and gates to every compared candidate. Perform
  no ranking or mathematical knee selection until every candidate has
  completed the same valid experiment.
- Treat any discovered placeholder, ignored option, proxy mislabeled as an
  endpoint metric, or unexercised acceptance gate as a correctness defect.
  Invalidate its prior results immediately and remove or disable the interface
  until it becomes real.

Use stock KiCad symbols and validate the native schematic with `kicad-cli`.
Never edit it while its lock exists. Preserve user layout and UI artifacts and
avoid parallel schematic sources. Put durable checks behind the Typer/Rich
root `manage.py`; do not create ad-hoc tests. Run `manage.py lint` and keep
every Python module at or below 600 physical lines. Reuse existing modules, keep
safety annotations conspicuous, and commit each verified logical checkpoint
with a detailed message.
