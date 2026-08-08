# Headgames

Open `headgames.kicad_sch` in KiCad. It is the sole authoritative schematic and
contains the complete externally wired MVP:
the three-electrode header and passive bias, matched AC-coupled LM324N
difference amplifier, alpha-band gain/filter, LM358N precision peak detector
with buffered envelope output, envelope-controlled carrier, and LM386 speaker
amplifier.

Power enters through keyed connector J2 from an isolated 9 V battery: pin 1 is
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

An immediately viewable PDF is generated as `build/BUILD_NOW.pdf` with:

```sh
inkscape BUILD_NOW.svg --export-type=pdf --export-filename=build/BUILD_NOW.pdf
```

The schematic references only stock KiCad libraries through `sym-lib-table`:
`Device`, `Switch`, `Amplifier_Operational`, `Amplifier_Audio`, and `power`.
There are no project-drawn substitute symbols.
