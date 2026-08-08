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


def schematic_nets() -> dict[str, set[tuple[str, str]]]:
    """Return the native schematic's electrical nets as component-pin pairs."""
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
        return {
            net.attrib["name"]: {
                (node.attrib["ref"], node.attrib["pin"])
                for node in net.findall("node")
            }
            for net in root.findall("./nets/net")
        }
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


@app.callback()
def main() -> None:
    """Calculate and verify the documented circuit design."""


@app.command()
def test() -> None:
    """Run the project's repeatable engineering checks."""
    nets = schematic_nets()
    assert_audio_input_path(nets)
    console.print("[green]Schematic connectivity checks passed.[/green]")


if __name__ == "__main__":
    app()
