#!/usr/bin/env python3
"""Single project entrypoint for circuit calculations and verification."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import typer
from rich.console import Console
from rich.table import Table

from circuit_sim import (
    EegPathComponents,
    logarithmic_sweep,
    magnitude_db,
    simulate_ac,
)


app = typer.Typer(no_args_is_help=True)
console = Console()
PROJECT_ROOT = Path(__file__).resolve().parent
SCHEMATIC = PROJECT_ROOT / "headgames.kicad_sch"


def schematic_data() -> tuple[
    dict[str, set[tuple[str, str]]], dict[str, str]
]:
    """Return the native schematic's electrical nets and component values."""
    netlist = PROJECT_ROOT / ".headgames-test-netlist.xml"
    try:
        subprocess.run(
            [
                "kicad-cli",
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                str(netlist),
                str(SCHEMATIC),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        root = ElementTree.parse(netlist).getroot()
        nets = {
            net.attrib["name"]: {
                (node.attrib["ref"], node.attrib["pin"])
                for node in net.findall("node")
            }
            for net in root.findall("./nets/net")
        }
        values = {
            component.attrib["ref"]: component.findtext("value", default="")
            for component in root.findall("./components/comp")
        }
        return nets, values
    finally:
        netlist.unlink(missing_ok=True)


def assert_audio_input_path(nets: dict[str, set[tuple[str, str]]]) -> None:
    """Require the carrier coupling network to reach only U2's signal input."""
    signal_net = next(net for net in nets.values() if ("U3", "3") in net)
    ground_net = next(net for net in nets.values() if ("U3", "4") in net)

    assert ("R5", "1") in signal_net, "U3 pin 3 is disconnected from R5"
    assert ("R8", "1") in signal_net, "input shunt is disconnected from U3 pin 3"
    assert ("U3", "2") in ground_net, "U3 inverting input must be grounded"
    assert ("C2", "2") in ground_net, "U3 bulk decoupling must return to ground"
    assert ("U3", "3") not in ground_net, "U3 signal input must not be grounded"


def assert_vref_capacitor_isolated(nets: dict[str, set[tuple[str, str]]]) -> None:
    """Require C1 to be isolated from the U1A follower output."""
    buffer_net = next(net for net in nets.values() if ("U2", "1") in net)
    vref_net = next(net for net in nets.values() if ("C9", "1") in net)

    assert ("U2", "2") in buffer_net, "U2A must remain a voltage follower"
    assert ("R2", "2") in buffer_net, "R2 must connect to the U2A output"
    assert ("R2", "1") in vref_net, "R2 must feed the reservoir side of VREF"
    assert ("U2", "1") not in vref_net, "C9 must not directly load the U2A output"


def resistance(value: str) -> float:
    """Parse the leading compact resistor value used in schematic fields."""
    token = value.split()[0]
    multipliers = {"k": 1_000.0, "M": 1_000_000.0}
    if token[-1] in multipliers:
        return float(token[:-1]) * multipliers[token[-1]]
    return float(token)


def capacitance(value: str) -> float:
    """Parse the leading compact capacitor value used in schematic fields."""
    token = value.split()[0]
    multipliers = {"p": 1e-12, "n": 1e-9, "u": 1e-6}
    return float(token[:-1]) * multipliers[token[-1]]


def assert_passives_have_values(values: dict[str, str]) -> None:
    """Require an explicit value for every resistor, trimmer, and capacitor."""
    missing = sorted(
        ref
        for ref, value in values.items()
        if ref.startswith(("R", "C")) and not value.strip()
    )
    assert not missing, f"passives without values: {', '.join(missing)}"


def assert_audio_drive_bounded(values: dict[str, str]) -> None:
    """Bound ideal LM386 output swing relative to carrier swing."""
    lm386_input_resistance = 50_000.0
    lm386_gain = 20.0
    series = resistance(values["R5"])
    shunt = resistance(values["R8"])
    effective_shunt = 1 / (1 / shunt + 1 / lm386_input_resistance)
    carrier_to_output = lm386_gain * effective_shunt / (series + effective_shunt)

    assert carrier_to_output <= 0.2, (
        "LM386 drive can demand excessive output swing: "
        f"{carrier_to_output:.3f} V/V from carrier to speaker output"
    )


def assert_audio_output_stabilized(
    nets: dict[str, set[tuple[str, str]]], values: dict[str, str]
) -> None:
    """Require the LM386 datasheet's series RC output stabilization branch."""
    output_net = next(net for net in nets.values() if ("U3", "5") in net)
    zobel_midpoint = next(net for net in nets.values() if ("C7", "2") in net)
    ground_net = next(net for net in nets.values() if ("U3", "4") in net)

    assert ("C7", "1") in output_net, "Zobel capacitor must start at U3 output"
    assert ("R10", "1") in zobel_midpoint, "Zobel C7 and R10 must be in series"
    assert ("R10", "2") in ground_net, "Zobel resistor must return to ground"
    assert resistance(values["R10"]) == 10.0, "Zobel resistor must be 10 ohms"
    assert values["C7"].split()[0] == "47n", "Zobel capacitor must be 47 nF"


def assert_eeg_signal_path(
    nets: dict[str, set[tuple[str, str]]], values: dict[str, str]
) -> None:
    """Require matched acquisition and explicit alpha/detector behavior."""
    assert values["U2"].startswith("LM324N"), (
        "the MVP must use the quad amplifier available in project inventory"
    )
    assert resistance(values["R12"]) == resistance(values["R22"])
    assert resistance(values["R15"]) == resistance(values["R21"])
    assert capacitance(values["C11"]) == capacitance(values["C15"])
    assert capacitance(values["C12"]) == capacitance(values["C14"])

    hp_fixed = resistance(values["R17"])
    hp_trim = resistance(values["RV1"])
    lp_fixed = resistance(values["R23"])
    lp_trim = resistance(values["RV2"])
    hp_range = (
        1 / (2 * math.pi * (hp_fixed + hp_trim) * capacitance(values["C13"])),
        1 / (2 * math.pi * hp_fixed * capacitance(values["C13"])),
    )
    lp_range = (
        1 / (2 * math.pi * (lp_fixed + lp_trim) * capacitance(values["C16"])),
        1 / (2 * math.pi * lp_fixed * capacitance(values["C16"])),
    )
    assert 7.8 <= hp_range[0] <= 8.0 and 8.7 <= hp_range[1] <= 8.9
    assert 10.7 <= lp_range[0] <= 10.8 and 12.3 <= lp_range[1] <= 12.5
    assert hp_range[1] < lp_range[0], "alpha passband corners can overlap"

    hp_trim_net = next(net for net in nets.values() if ("RV1", "2") in net)
    lp_trim_net = next(net for net in nets.values() if ("RV2", "2") in net)
    assert hp_trim_net == {("R17", "1"), ("RV1", "2")}
    assert lp_trim_net == {("R23", "1"), ("RV2", "2")}

    detector_release = resistance(values["R18"]) * capacitance(values["C17"])
    assert math.isclose(detector_release, 0.22), (
        f"detector release time constant is {detector_release:.3f} s"
    )


def eeg_path_model(
    values: dict[str, str], hp_trim_fraction: float = 0.5, lp_trim_fraction: float = 0.5
) -> EegPathComponents:
    """Build the simulator model from authoritative schematic values."""
    return EegPathComponents(
        # 20 kohm is a stated, conservative dry-electrode source assumption;
        # unlike every value below, it is not a schematic component.
        electrode_resistance=20_000.0,
        safety_resistance=resistance(values["R16"]) + resistance(values["R14"]),
        input_resistance=resistance(values["R15"]),
        input_capacitance=capacitance(values["C12"]),
        diff_feedback_resistance=resistance(values["R12"]),
        diff_feedback_capacitance=capacitance(values["C11"]),
        alpha_input_resistance=(
            resistance(values["R17"])
            + hp_trim_fraction * resistance(values["RV1"])
        ),
        alpha_input_capacitance=capacitance(values["C13"]),
        alpha_feedback_resistance=(
            resistance(values["R23"])
            + lp_trim_fraction * resistance(values["RV2"])
        ),
        alpha_feedback_capacitance=capacitance(values["C16"]),
    )


def assert_eeg_simulation(values: dict[str, str]) -> None:
    """Regression-check realistic small-signal behavior at all trim corners."""
    for hp_fraction in (0.0, 0.5, 1.0):
        for lp_fraction in (0.0, 0.5, 1.0):
            model = eeg_path_model(values, hp_fraction, lp_fraction)
            sweep = logarithmic_sweep(model)
            peak = max(sweep, key=lambda point: abs(point.total_gain))
            assert 7.2 <= peak.frequency_hz <= 7.9, (
                f"unexpected response peak: {peak.frequency_hz:.2f} Hz"
            )
            assert 2_300 <= abs(peak.total_gain) <= 2_850, (
                f"unexpected peak gain: {abs(peak.total_gain):.1f} V/V"
            )

    nominal = eeg_path_model(values)
    gain_10_hz = abs(simulate_ac(nominal, 10.0).total_gain)
    assert 2_300 <= gain_10_hz <= 2_500
    # 200 uV peak is the high end of the documented alpha fixture.  It must
    # remain comfortably inside the roughly 1.5 V positive LM324 headroom on 9 V.
    assert 200e-6 * gain_10_hz < 0.75


def print_eeg_simulation(values: dict[str, str]) -> None:
    """Print nominal AC response and EEG-scale signal predictions."""
    model = eeg_path_model(values)
    sweep = logarithmic_sweep(model)
    peak = max(sweep, key=lambda point: abs(point.total_gain))
    table = Table(title="EEG path AC simulation (trimmers at midpoint)")
    table.add_column("Input")
    table.add_column("Frequency", justify="right")
    table.add_column("Gain", justify="right")
    table.add_column("ALPHA peak", justify="right")
    for amplitude_uv, frequency_hz in ((10, 10), (20, 10), (50, 10), (100, 10), (50, 8), (50, 12)):
        result = simulate_ac(model, frequency_hz)
        gain = abs(result.total_gain)
        table.add_row(
            f"{amplitude_uv} uV peak",
            f"{frequency_hz:.0f} Hz",
            f"{gain:.0f} V/V ({magnitude_db(result.total_gain):.1f} dB)",
            f"{amplitude_uv * 1e-6 * gain:.3f} V",
        )
    console.print(table)
    console.print(
        f"Response peak: [bold]{peak.frequency_hz:.2f} Hz[/bold] at "
        f"{abs(peak.total_gain):.0f} V/V; assumed electrode source resistance: "
        "20 kohm per differential source."
    )
    console.print(
        "[yellow]Interpretation:[/yellow] the cascaded response peaks below 8 Hz; "
        "the RC corner labels do not make this a sharply selective 8-12 Hz filter."
    )


def assert_precision_detector(
    nets: dict[str, set[tuple[str, str]]], values: dict[str, str]
) -> None:
    """Require active diode compensation and a buffered envelope output."""
    alpha_net = next(net for net in nets.values() if ("U2", "8") in net)
    drive_net = next(net for net in nets.values() if ("U1", "1") in net)
    raw_envelope_net = next(net for net in nets.values() if ("D1", "1") in net)
    envelope_net = next(net for net in nets.values() if ("R6", "2") in net)
    vcc_net = next(net for net in nets.values() if ("U1", "8") in net)
    ground_net = next(net for net in nets.values() if ("U1", "4") in net)

    assert values["U1"].startswith("LM358N"), (
        "precision detector must use the inventory LM358N"
    )
    assert values["D1"].startswith("1N4148"), (
        "detector must use the common low-leakage small-signal silicon diode"
    )
    assert ("U1", "3") in alpha_net, "U1A non-inverting input must sense ALPHA"
    assert ("D1", "2") in drive_net, "D1 anode must be driven inside U3A feedback"
    assert {("U1", "2"), ("U1", "5"), ("R18", "1"), ("C17", "1")} <= (
        raw_envelope_net
    ), "U3A must sense the held envelope and U3B must buffer it"
    assert {("U1", "6"), ("U1", "7"), ("R6", "2")} <= envelope_net, (
        "U3B must be a voltage follower driving ENV"
    )
    assert ("C4", "1") in vcc_net, "U1 local decoupling must connect to VCC"
    assert ("C4", "2") in ground_net, "U1 local decoupling must return to ground"
    assert math.isclose(capacitance(values["C4"]), 100e-9)


def assert_isolated_battery_input(
    nets: dict[str, set[tuple[str, str]]], values: dict[str, str]
) -> None:
    """Require the keyed, explicitly rated battery-only power interface."""
    vcc_net = next(net for net in nets.values() if ("U2", "4") in net)
    ground_net = next(net for net in nets.values() if ("U2", "11") in net)
    key_net = next(net for net in nets.values() if ("J1", "2") in net)

    assert values["J1"] == "9V BATTERY IN"
    assert ("J1", "1") in vcc_net, "J1 pin 1 must supply positive 9 V"
    assert ("J1", "3") in ground_net, "J1 pin 3 must be battery return"
    assert key_net == {("J1", "2")}, "J1 pin 2 must remain an unused key"


def assert_redundant_electrode_limiting(
    nets: dict[str, set[tuple[str, str]]], values: dict[str, str]
) -> None:
    """Require two independent current-limiting resistors per electrode."""
    paths = (
        ("1", "R19", "R20", 50e-6),
        ("2", "R16", "R14", 50e-6),
        ("3", "R13", "R11", 5e-6),
    )
    for connector_pin, outer, inner, maximum_current in paths:
        connector_net = next(
            net for net in nets.values() if ("J2", connector_pin) in net
        )
        assert connector_net == {("J2", connector_pin), (outer, "2")}, (
            f"J2 pin {connector_pin} must first pass through {outer}"
        )
        circuit_net = next(net for net in nets.values() if (outer, "1") in net)
        assert (inner, "2") in circuit_net, (
            f"{outer} and {inner} must be independent series limiters"
        )
        fault_free_current = 9.0 / (
            resistance(values[outer]) + resistance(values[inner])
        )
        assert fault_free_current <= maximum_current, (
            f"J1 pin {connector_pin} current limit is "
            f"{fault_free_current * 1e6:.1f} uA"
        )


def assert_erc_clean() -> None:
    """Require KiCad's complete electrical-rules check to pass."""
    report = PROJECT_ROOT / ".headgames-test-erc.rpt"
    try:
        subprocess.run(
            [
                "kicad-cli",
                "sch",
                "erc",
                "--exit-code-violations",
                "--output",
                str(report),
                str(SCHEMATIC),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        report.unlink(missing_ok=True)


@app.callback()
def main() -> None:
    """Calculate and verify the documented circuit design."""


@app.command()
def test() -> None:
    """Run the project's repeatable engineering checks."""
    nets, values = schematic_data()
    assert_passives_have_values(values)
    assert_audio_input_path(nets)
    assert_vref_capacitor_isolated(nets)
    assert_audio_drive_bounded(values)
    assert_audio_output_stabilized(nets, values)
    assert_eeg_signal_path(nets, values)
    assert_eeg_simulation(values)
    assert_precision_detector(nets, values)
    assert_isolated_battery_input(nets, values)
    assert_redundant_electrode_limiting(nets, values)
    assert_erc_clean()
    console.print("[green]Schematic connectivity checks passed.[/green]")


@app.command("simulate-eeg")
def simulate_eeg() -> None:
    """Simulate the small-signal electrode-to-ALPHA response."""
    _, values = schematic_data()
    assert_eeg_simulation(values)
    print_eeg_simulation(values)


if __name__ == "__main__":
    app()
