# Headgames

Open `headgames.kicad_sch` in KiCad. It is the sole authoritative schematic and
contains the complete externally wired MVP:
the three-electrode header and passive bias, matched AC-coupled LM324
difference amplifier, alpha-band gain/filter, passive envelope detector,
envelope-controlled carrier, and LM386 speaker amplifier.

Anything connected to the electrode header must use an isolated wired DC
supply with no mains-earth or USB connection while worn. Remove all grounded
test equipment, non-isolated bench supplies, USB connections, chargers, and
mains-connected audio equipment before attaching electrodes.

An immediately viewable PDF is generated as `build/BUILD_NOW.pdf` with:

```sh
inkscape BUILD_NOW.svg --export-type=pdf --export-filename=build/BUILD_NOW.pdf
```

The schematic references only stock KiCad libraries through `sym-lib-table`:
`Device`, `Switch`, `Amplifier_Operational`, `Amplifier_Audio`, and `power`.
There are no project-drawn substitute symbols.
