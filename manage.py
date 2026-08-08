#!/usr/bin/env python3
"""Single project entrypoint for circuit calculations and verification."""

from __future__ import annotations

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
    console.print("[green]Schematic connectivity checks passed.[/green]")


if __name__ == "__main__":
    app()
