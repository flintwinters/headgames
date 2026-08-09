# Headgames

Headgames is an isolated-battery, entirely analog EEG-to-audio proof of
concept. Its target is zero-window continuous-time sonification:

```text
wet active electrodes → differential acquisition → low-delay alpha weighting
→ direct bipolar oscillator control → LM386 → speaker
```

“Instantaneous” means no sampling, DSP, analysis window, peak hold, or envelope
release. Causal analog filtering still has measurable group delay and the
candidate selection explicitly minimizes it.

## Safety

This prototype is not a medical device. While electrodes are attached, use
only the keyed isolated 9 V battery input. Disconnect grounded scopes, bench
supplies, USB, chargers, powered audio, and every other mains-earth path.

MEAS and REF each require two independent 100 kΩ safety resistors electrically
ahead of the electrode-site LM358N buffer. BIAS requires two independent 1 MΩ
resistors. The active assembly has exactly five isolated conductors: buffered
MEAS, buffered REF, BIAS, battery VCC, and battery return.

## Verification

Use the root entrypoint:

```sh
python3 manage.py test
python3 manage.py simulate-sonification --candidate selected --electrode wet
python3 manage.py simulate-sonification-frontier --electrode wet --samples 2001 --seed 1212498244 --phase-steps 16
python3 manage.py synthesize-cable-isolation
python3 manage.py accept
```

`test` reproduces durable regressions; it is not acceptance. Wet electrodes
gate and dry electrodes are informational. Only `accept` may print `MODEL
ACCEPTANCE PASS`, and it currently fails closed because the native schematic
still implements the historical detector-centered circuit.

The nominal fixed-family frontier currently selects the broad existing ALPHA
weighting at 34.13 ms worst 8–12 Hz group delay and 0.1726 alpha/artifact
speaker-modulation ratio. One MFB section measures 82.41 ms and 1.2699; two
sections measure 134.51 ms and 9.1975 and remain report-only. The stateful U2D
and bounded LM386 model reports roughly 682 Hz carrier frequency plus duty,
sidebands, harmonics, current, clipping, latching, and node margins.

The TI LMx58 transient sweep selects the smallest passing stocked cable
isolation resistor, 8.2 kΩ, with 0.0% measured overshoot and 8.4 µs worst
settling across 150/250 pF loads. A separate loop-return or bench measurement
is still required for the 45° phase-margin gate.

The current `--samples` and `--phase-steps` frontier options declare the target
stress contract but the exhaustive physical-build/phase transient runner is
not yet implemented. Accordingly, neither those arguments nor nominal model
success constitutes hardware acceptance.

## Matched acquisition parts

Pair-match `R12/R22` (10 MΩ), `R15/R21` (reserved measured 474 kΩ),
`C12/C14` (100 nF), and `C11/C15` (1.5 nF equivalent). Build each 1.5 nF
network from one 1 nF C0G in parallel with two series 1 nF C0G parts and show
all parts in the schematic. A 1 nF + 470 pF pair is acceptable when stocked; a
lone 1 nF is not.
