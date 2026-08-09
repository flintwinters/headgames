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
python3 manage.py simulate-sonification --candidate alpha --electrode wet
python3 manage.py simulate-sonification-frontier --electrode wet --samples 2001 --seed 1212498244 --phase-steps 16
python3 manage.py synthesize-cable-isolation
python3 manage.py accept
```

`test` reproduces durable regressions; it is not acceptance. Wet electrodes
gate and dry electrodes are informational. Only `accept` may print `MODEL
ACCEPTANCE PASS`. The native schematic now matches the selected direct path,
but acceptance remains closed because the nominal noisy wet/alpha run fails
the alpha-modulation gate.

The stateful U2D and bounded LM386 model reports roughly 683 Hz carrier
frequency plus duty, sidebands, harmonics, speaker current, clipping, latching,
and node margins. The current nominal wet/alpha run executes all 16 requested
phases but fails its alpha-modulation gate; no candidate is selected.

The TI LMx58 transient sweep selects the smallest passing stocked cable
isolation resistor, 8.2 kΩ, with 0.0% measured overshoot and 8.4 µs worst
settling across 150/250 pF loads. A separate loop-return or bench measurement
is still required for the 45° phase-margin gate.

The frontier runner executes the requested physical builds and phases at the
speaker-current endpoint, but it has not produced a verified passing campaign.
The previously reported proxy frontier and candidate selection remain invalid.

## Matched acquisition parts

Pair-match `R12/R22` (10 MΩ), `R15/R21` (reserved measured 474 kΩ),
`C12/C14` (100 nF), and `C11/C15` (1.5 nF equivalent). Build each 1.5 nF
network from one 1 nF C0G in parallel with two series 1 nF C0G parts and show
all parts in the schematic. A 1 nF + 470 pF pair is acceptable when stocked; a
lone 1 nF is not.
