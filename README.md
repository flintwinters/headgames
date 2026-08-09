# Headgames

Open `headgames.kicad_sch` in KiCad. It is the sole authoritative schematic and
contains the complete externally wired MVP:
the three-electrode header with redundant series current limiting and passive
bias, matched AC-coupled LM324N
difference amplifier, alpha-band gain/filter, LM358N precision peak detector
with buffered envelope output, envelope-controlled carrier, and LM386 speaker
amplifier.

Power enters through keyed connector J1 from an isolated 9 V battery: pin 1 is
positive, pin 3 is battery return, and pin 2 is deliberately unused as a key.
Do not replace this source with a mains-connected or USB-derived supply while
the electrodes are attached.

Anything connected to the electrode header must use an isolated wired DC
supply with no mains-earth or USB connection while worn. Remove all grounded
test equipment, non-isolated bench supplies, USB connections, chargers, and
mains-connected audio equipment before attaching electrodes.

Run the repeatable native-schematic calculations, connectivity assertions, and
KiCad electrical-rules check with:

```sh
python3 manage.py test
```

Run the frequency-domain circuit simulation with:

```sh
python3 manage.py simulate-eeg
python3 manage.py simulate-artifacts
python3 manage.py simulate-active-electrodes
```

The simulator extracts every circuit component value from the KiCad schematic
and solves the complex impedances of the matched differential acquisition and
active alpha-filter stages. It assumes a 20 kohm Thevenin source resistance at
each electrode and ideal op-amp closed-loop behavior. Representative fixtures
are 10-100 uV peak differential signals; these are engineering test inputs, not
a promise of what a particular electrode placement or person will produce.

At nominal trimmer midpoints, the present design predicts about 2,433 V/V at
10 Hz: 10, 20, 50, and 100 uV peak inputs become approximately 24, 49, 122,
and 243 mV peak at `ALPHA`. The complete cascaded response peaks near 7.53 Hz
and has a broad approximate -3 dB span of 3.69-14.71 Hz. Thus it has adequate
gain for EEG-scale bench fixtures, but it is not a sharp alpha-band selector.
Real electrode imbalance, motion artifacts, 50/60 Hz pickup, op-amp noise,
offset, bias current, slew/output limits, diode behavior, and component
tolerance are outside this first-order AC model and require isolated bench
measurement. Simulation does not authorize connecting a person to grounded
test equipment.

The artifact command applies a deliberately explicit project-survival fixture:
20 kΩ and 100 kΩ electrode sources, 1 mV peak differential motion at 2 Hz,
100 uV peak differential muscle-like activity at 30 Hz, and 100 mV peak
common-mode mains pickup at 60 Hz. It compares that simultaneous mixture with
and without 50 uV peak differential alpha at 10 Hz, then runs the result through
an ideal precision peak detector using the schematic's 0.22 s release constant.
The provisional criterion is at least a 25% change in mean `ENV` when alpha is
added. Nominal simulation fails: artifacts alone produce 0.919 V mean above
VREF and adding alpha raises that only to 0.954 V, a 3.8% change. These fixture
amplitudes are declared stress assumptions, not universal physiological bounds.
Change them deliberately as physical measurements become available.

The active-electrode command compares the passive cable against a candidate
unity buffer located after both series safety resistors at each electrode. The
declared generic buffer has 5 pF input capacitance, 1 MHz gain-bandwidth,
100 ohm output resistance, 10 pA input bias, and 25 nV/rtHz white voltage noise;
MEAS and REF cables are deliberately unequal at 150 pF and 250 pF. This is an
architectural sensitivity model, not a selected part or a parallel schematic.

The candidate reduces the 60 Hz common-mode contribution from 0.686 V to
0.004 V peak at `ALPHA`, despite 20 kΩ/100 kΩ electrode mismatch. It does not
reject differential electrode motion: the 2 Hz contribution increases from
0.837 V to 1.000 V as loading is removed. Artifacts-only mean `ENV` is 0.586 V
and adding 50 uV alpha raises it only to 0.603 V, a 2.8% change that still fails
the 25% target. Declared buffer plus electrode/safety-resistor white noise
integrates to about 1.33 mV RMS at `ALPHA` over 0.5-100 Hz; 1/f noise, current
noise, central LM324 noise, and interference are excluded.

Any physical active-electrode experiment must relocate both independent series
safety resistors to the electrode-side module, ahead of its buffer. Leaving
them at the central board would put powered circuitry between the electrode and
the protection. The complete worn buffer supply and every attached conductor
remain subject to the isolated-battery-only rule.

## Matched acquisition components

Common-mode rejection depends on matched impedance ratios, not merely nominal
part values. Select and keep these components as matched pairs:

- `R12` and `R22`: 10 MΩ
- `C11` and `C15`: nominally 1.5 nF
- `C12` and `C14`: 100 nF
- `R15` and `R21`: the measured 474 kΩ pair reserved for this build

Record individual measured values and pair parts by their difference on the
same meter range. The two 474 kΩ parts are a suitable common-value replacement
for the schematic's nominal 470 kΩ pair; using both changes nominal differential
gain by less than 1%.

There are currently no 1.5 nF capacitors in inventory. Do **not** substitute
1 nF alone for `C11` and `C15`: with the 10 MΩ resistors that would move their
low-pass corner from approximately 10.6 Hz to 15.9 Hz and further broaden the
artifact-sensitive response. The preferred inventory-first implementation at
each location uses three 1 nF C0G capacitors: one capacitor in parallel with a
series pair. Its equivalent capacitance is exactly 1.5 nF nominally
(`1 nF + (1 nF / 2)`). Select six parts so the two three-capacitor networks have
closely matched **combined capacitance**. A lower-part-count alternative, when
470 pF C0G parts are available, is 1 nF in parallel with 470 pF; its 1.47 nF
total gives an approximately 10.8 Hz corner. The authoritative schematic must
show every physical capacitor when either substitution is implemented.

An immediately viewable PDF is generated as `build/BUILD_NOW.pdf` with:

```sh
inkscape BUILD_NOW.svg --export-type=pdf --export-filename=build/BUILD_NOW.pdf
```

The schematic references only stock KiCad libraries through `sym-lib-table`:
`Device`, `Switch`, `Amplifier_Operational`, `Amplifier_Audio`, and `power`.
There are no project-drawn substitute symbols.
