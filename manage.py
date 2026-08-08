#!/usr/bin/env python3
"""Single project entrypoint for circuit calculations and verification."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import typer
from rich.console import Console


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
    signal_net = next(net for net in nets.values() if ("U2", "3") in net)
    ground_net = next(net for net in nets.values() if ("U2", "4") in net)

    assert ("R18", "1") in signal_net, "U2 pin 3 is disconnected from R18"
    assert ("R19", "1") in signal_net, "input shunt is disconnected from U2 pin 3"
    assert ("U2", "2") in ground_net, "U2 inverting input must be grounded"
    assert ("C14", "2") in ground_net, "U2 bulk decoupling must return to ground"
    assert ("U2", "3") not in ground_net, "U2 signal input must not be grounded"


def assert_vref_capacitor_isolated(nets: dict[str, set[tuple[str, str]]]) -> None:
    """Require C1 to be isolated from the U1A follower output."""
    buffer_net = next(net for net in nets.values() if ("U1", "1") in net)
    vref_net = next(net for net in nets.values() if ("C1", "1") in net)

    assert ("U1", "2") in buffer_net, "U1A must remain a voltage follower"
    assert ("R17", "2") in buffer_net, "R17 must connect to the U1A output"
    assert ("R17", "1") in vref_net, "R17 must feed the reservoir side of VREF"
    assert ("U1", "1") not in vref_net, "C1 must not directly load the U1A output"


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


def assert_audio_drive_bounded(values: dict[str, str]) -> None:
    """Bound ideal LM386 output swing relative to carrier swing."""
    lm386_input_resistance = 50_000.0
    lm386_gain = 20.0
    series = resistance(values["R18"])
    shunt = resistance(values["R19"])
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
    output_net = next(net for net in nets.values() if ("U2", "5") in net)
    zobel_midpoint = next(net for net in nets.values() if ("C16", "2") in net)
    ground_net = next(net for net in nets.values() if ("U2", "4") in net)

    assert ("C16", "1") in output_net, "Zobel capacitor must start at U2 output"
    assert ("R20", "1") in zobel_midpoint, "Zobel C16 and R20 must be in series"
    assert ("R20", "2") in ground_net, "Zobel resistor must return to ground"
    assert resistance(values["R20"]) == 10.0, "Zobel resistor must be 10 ohms"
    assert values["C16"].split()[0] == "50n", "Zobel capacitor must be 50 nF"


def assert_eeg_signal_path(values: dict[str, str]) -> None:
    """Require matched acquisition and explicit alpha/detector behavior."""
    assert values["U1"].startswith("LM324N"), (
        "the MVP must use the quad amplifier available in project inventory"
    )
    assert resistance(values["R4"]) == resistance(values["R8"])
    assert resistance(values["R6"]) == resistance(values["R9"])
    assert capacitance(values["C3"]) == capacitance(values["C4"])
    assert capacitance(values["C5"]) == capacitance(values["C6"])

    high_pass = 1 / (
        2 * math.pi * resistance(values["R10"]) * capacitance(values["C7"])
    )
    low_pass = 1 / (
        2 * math.pi * resistance(values["R11"]) * capacitance(values["C8"])
    )
    assert 8.0 <= high_pass <= 9.0, f"alpha high-pass is {high_pass:.2f} Hz"
    assert 12.0 <= low_pass <= 13.0, f"alpha low-pass is {low_pass:.2f} Hz"
    assert high_pass < low_pass, "alpha passband corners must not overlap"

    assert values["D1"].startswith("1N5711"), (
        "detector must specify the characterized low-level Schottky part"
    )
    detector_release = resistance(values["R12"]) * capacitance(values["C9"])
    assert math.isclose(detector_release, 0.22), (
        f"detector release time constant is {detector_release:.3f} s"
    )


def assert_isolated_battery_input(
    nets: dict[str, set[tuple[str, str]]], values: dict[str, str]
) -> None:
    """Require the keyed, explicitly rated battery-only power interface."""
    vcc_net = next(net for net in nets.values() if ("U1", "4") in net)
    ground_net = next(net for net in nets.values() if ("U1", "11") in net)
    key_net = next(net for net in nets.values() if ("J2", "2") in net)

    assert values["J2"] == "9V BATTERY IN"
    assert ("J2", "1") in vcc_net, "J2 pin 1 must supply positive 9 V"
    assert ("J2", "3") in ground_net, "J2 pin 3 must be battery return"
    assert key_net == {("J2", "2")}, "J2 pin 2 must remain an unused key"


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
    assert_audio_input_path(nets)
    assert_vref_capacitor_isolated(nets)
    assert_audio_drive_bounded(values)
    assert_audio_output_stabilized(nets, values)
    assert_eeg_signal_path(values)
    assert_isolated_battery_input(nets, values)
    assert_erc_clean()
    console.print("[green]Schematic connectivity checks passed.[/green]")


if __name__ == "__main__":
    app()
