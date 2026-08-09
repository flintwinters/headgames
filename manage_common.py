#!/usr/bin/env python3
"""Single project entrypoint for circuit calculations and verification."""

from __future__ import annotations

import math
import hashlib
import os
import random
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from functools import lru_cache
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

import typer
from rich.console import Console
from rich.table import Table

from circuit_sim import (
    ActiveElectrodeChannel,
    AmplifierLimits,
    CascadedBandpass,
    DiodeModel,
    EegPathComponents,
    EnvelopeResult,
    SignalTone,
    active_electrode_output_noise_rms,
    electrode_profile,
    follower_cable_stability,
    logarithmic_sweep,
    magnitude_db,
    simulate_ac,
    simulate_active_electrode_inputs,
    simulate_electrode_inputs,
    simulate_nonideal_active_electrode_inputs,
    simulate_ideal_peak_detector,
    simulate_precision_peak_detector,
)
from physical_filter import (
    FilterStressResult,
    MfbStageParts,
    OpAmpModel,
    bounded_stage_sample,
    component_corner_cases,
    ideal_stage_transfer,
    integrated_output_noise_rms,
    recovery_bound_seconds,
    solve_cascade_ac,
    solve_stage_ac,
)
from inventory import (
    parse_compact_value,
    read_inventory,
    synthesize_mfb,
)
from sonification import (
    BROADBAND_REJECTION_HZ,
    BROADBAND_SLOW_HZ,
    BROADBAND_WANTED_HZ,
    CANDIDATES,
    BroadbandBuildResult,
    BroadbandFrequencyResult,
    BroadbandParts,
    ChannelParts,
    MfbParts,
    SonificationBuild,
    closed_form_group_delay,
    broadband_group_delay,
    broadband_transfer_gain,
    group_delay as sonification_group_delay,
    relaxation_frequency,
    simulate_build,
    simulate_broadband_build,
)


app = typer.Typer(no_args_is_help=True)
console = Console()
PROJECT_ROOT = Path(__file__).resolve().parent
SCHEMATIC = PROJECT_ROOT / "headgames.kicad_sch"
BOM = PROJECT_ROOT / "headgames_bom.csv"
TI_MODEL = PROJECT_ROOT / "models" / "ti" / "lmx58_lm2904.lib"
ALPHA_REDESIGN_R6_OHM = (100_000.0, 68_000.0, 47_000.0)
BROADBAND_GAIN_FEEDBACK_OHM = (390_000.0, 470_000.0, 560_000.0, 680_000.0, 820_000.0)
BROADBAND_R6_OHM = (47_000.0, 68_000.0, 100_000.0)
BROADBAND_GAIN_INPUT_OHM = 100_000.0

LM324_ACQUISITION = AmplifierLimits(
    "LM324N acquisition", 100_000.0, 1_000_000.0, 2e-3, 45e-9, 0.5e6,
    0.020, 2.0, 5e-3, 0.0, 2.0, 50e-6,
)
LM358_DETECTOR = AmplifierLimits(
    "LM358N detector", 100_000.0, 1_000_000.0, 2e-3, 45e-9, 0.1e6,
    0.020, 2.0, 5e-3, 0.0, 2.0, 50e-6,
)
LM324_TYPICAL_INPUT_OFFSET_CURRENT_A = 5e-9
FRONTIER_NOMINAL_SUPPLY_V = 9.0
FRONTIER_SUPPLY_BAND_V = 0.2
# Model the tolerance printed on readily available assembled parts. These are
# build-to-build component limits, not environmental or abuse excursions.
FRONTIER_RESISTOR_BAND = 0.05
FRONTIER_CAPACITOR_BAND = 0.10
FRONTIER_SAMPLES = 2_000
DETECTOR_DIODE = DiodeModel()
INVENTORY_AMPLIFIER_ICS = frozenset({"LM324N", "LM358N"})
ACTIVE_ELECTRODE_IC = "LM358N"
ACTIVE_ELECTRODE_CONDUCTORS = (
    "MEAS_BUFFERED", "REF_BUFFERED", "BIAS", "VCC_ISOLATED", "GND_ISOLATED",
)

# A block is aligned only when the same physical implementation is both drawn
# in the native schematic and exercised by the frontier model.  Keep the
# boundary explicit: mathematical substitutes and adjacent circuitry do not
# count as implementation equivalence.
FRONTIER_ALIGNMENT = (
    ("electrode-site active buffers", True, True),
    ("LM324 acquisition and ALPHA", True, True),
    ("direct bipolar carrier control", True, True),
    ("LM386 speaker-current output", True, True),
)


class VerificationError(RuntimeError):
    """A durable project acceptance condition was not satisfied."""


def require(condition: bool, message: str) -> None:
    """Raise explicitly so optimization can never erase a verification gate."""
    if not condition:
        raise VerificationError(message)


def require_frontier_alignment() -> None:
    """Reject acceptance unless KiCad and the physical model cover one circuit."""
    mismatches = [
        name for name, modeled, schematic in FRONTIER_ALIGNMENT
        if modeled != schematic
    ]
    require(not mismatches, "KiCad/model boundary mismatch: " + "; ".join(mismatches))


def print_frontier_alignment() -> None:
    """Report the implementation boundary without privileging either source."""
    table = Table(title="KiCad versus physical-frontier implementation")
    table.add_column("Circuit block")
    table.add_column("Physical model", justify="center")
    table.add_column("KiCad", justify="center")
    table.add_column("Finding")
    for name, modeled, schematic in FRONTIER_ALIGNMENT:
        finding = "same boundary" if modeled == schematic else (
            "model-only proposal" if modeled else "schematic-only downstream block"
        )
        table.add_row(name, "yes" if modeled else "no", "yes" if schematic else "no", finding)
    console.print(table)


def require_assertions_enabled() -> None:
    """Fail closed until legacy assertions have all migrated to ``require``."""
    require(__debug__, "python -O is forbidden: legacy verification assertions remain")


def schematic_data() -> tuple[
    dict[str, set[tuple[str, str]]], dict[str, str]
]:
    """Return the native schematic's electrical nets and component values."""
    netlist = PROJECT_ROOT / f".headgames-test-netlist-{os.getpid()}.xml"
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


def oscillator_control_resistance(
    values: dict[str, str], rheostat_fraction: float = 0.5
) -> float:
    """Return fixed R6 plus the selected fraction of series rheostat RV3."""
    require(0.0 <= rheostat_fraction <= 1.0,
            "RV3 rheostat fraction must be between zero and one")
    return resistance(values["R6"]) + rheostat_fraction * resistance(values["RV3"])


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
    """Prove the native schematic implements the topology solved in circuit_sim."""
    assert values["U2"].startswith("LM324N"), (
        "the MVP must use the quad amplifier available in project inventory"
    )
    assert resistance(values["R12"]) == resistance(values["R22"])
    assert resistance(values["R15"]) == resistance(values["R21"])
    assert resistance(values["R16"]) == resistance(values["R19"])
    assert resistance(values["R14"]) == resistance(values["R20"])
    assert capacitance(values["C11"]) == capacitance(values["C15"])
    assert capacitance(values["C12"]) == capacitance(values["C14"])

    def exact_net(pin: tuple[str, str], expected: set[tuple[str, str]]) -> None:
        actual = next((net for net in nets.values() if pin in net), None)
        require(actual == expected,
                f"{pin[0]} pin {pin[1]} topology mismatch: {sorted(actual or ())}")

    # Each electrode reaches its electrode-site LM358 follower only after two
    # independent safety resistors.  Each follower then drives an independent
    # 8.2 kohm cable-isolation resistor before the five-wire boundary.
    exact_net(("J2", "2"), {("J2", "2"), ("R16", "2")})
    exact_net(("R16", "1"), {("R16", "1"), ("R14", "2")})
    exact_net(("R14", "1"), {("R14", "1"), ("U1", "3")})
    exact_net(("U1", "1"), {("U1", "1"), ("U1", "2"), ("R24", "2")})
    exact_net(("R24", "1"), {("R24", "1"), ("C12", "2")})
    exact_net(("C12", "1"), {("C12", "1"), ("R15", "2")})
    exact_net(("U2", "5"), {
        ("U2", "5"), ("R15", "1"), ("R12", "1"), ("C11", "1")
    })

    # REF reaches U2B's inverting input through the symmetric network;
    # R22||C15 closes feedback from pin 7 at DIFF_OUT.
    exact_net(("J2", "1"), {("J2", "1"), ("R19", "2")})
    exact_net(("R19", "1"), {("R19", "1"), ("R20", "2")})
    exact_net(("R20", "1"), {("R20", "1"), ("U1", "5")})
    exact_net(("U1", "7"), {("U1", "7"), ("U1", "6"), ("R25", "2")})
    exact_net(("R25", "1"), {("R25", "1"), ("C14", "2")})
    exact_net(("C14", "1"), {("C14", "1"), ("R21", "2")})
    exact_net(("U2", "6"), {
        ("U2", "6"), ("R21", "1"), ("R22", "2"), ("C15", "2")
    })
    exact_net(("U2", "7"), {
        ("U2", "7"), ("R22", "1"), ("C15", "1"), ("C13", "2")
    })

    # DIFF_OUT enters the U2C inverting ALPHA stage through C13/R17/RV1;
    # C16 parallels the R23/RV2 feedback path from ALPHA.
    exact_net(("C13", "1"), {("C13", "1"), ("R17", "2")})
    exact_net(("R17", "1"), {("R17", "1"), ("RV1", "2")})
    exact_net(("U2", "9"), {
        ("U2", "9"), ("RV1", "1"), ("R23", "2"), ("C16", "2")
    })
    exact_net(("R23", "1"), {("R23", "1"), ("RV2", "2")})
    alpha_net = next(net for net in nets.values() if ("U2", "8") in net)
    require({("U2", "8"), ("RV2", "1"), ("C16", "1")} <= alpha_net,
            "ALPHA feedback does not match the simulated parallel network")
    require(("R6", "2") in alpha_net,
            "ALPHA must enter the oscillator coupling network through R6")
    exact_net(("R6", "1"), {("R6", "1"), ("RV3", "2"), ("RV3", "3")})
    oscillator_control_net = next(net for net in nets.values() if ("RV3", "1") in net)
    require({("RV3", "1"), ("U2", "12")} <= oscillator_control_net,
            "RV3 must complete the explicit series path to U2D")
    require(resistance(values["R6"]) == 180_000.0,
            "R6 must retain a 180 kohm oscillator-coupling floor")
    require(resistance(values["RV3"]) == 25_000.0,
            "RV3 must provide 25 kohm of adjustable series resistance")
    require(oscillator_control_resistance(values, 0.0) == 180_000.0,
            "oscillator coupling minimum must remain bounded at 180 kohm")
    require(oscillator_control_resistance(values, 1.0) == 205_000.0,
            "oscillator coupling maximum must remain 205 kohm")
    require(resistance(values["R24"]) == 8_200.0
            and resistance(values["R25"]) == 8_200.0,
            "both active-buffer cable outputs require 8.2 kohm isolation")
    vref_net = next(net for net in nets.values() if ("U2", "10") in net)
    require({("U2", "10"), ("R12", "2"), ("C11", "2")} <= vref_net,
            "MEAS shunt and U2C bias must return to VREF")
    all_nodes = set().union(*nets.values())
    require(("RV1", "3") not in all_nodes and ("RV2", "3") not in all_nodes,
            "trim unused ends must remain open for rheostat topology")

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
    nominal_sweep = logarithmic_sweep(nominal)
    nominal_peak = max(abs(point.total_gain) for point in nominal_sweep)
    passband = tuple(
        point.frequency_hz
        for point in nominal_sweep
        if abs(point.total_gain) >= nominal_peak / math.sqrt(2)
    )
    assert 3.6 <= passband[0] <= 3.8
    assert 14.5 <= passband[-1] <= 14.9
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
    peak_gain = abs(peak.total_gain)
    passband = tuple(
        point.frequency_hz
        for point in sweep
        if abs(point.total_gain) >= peak_gain / math.sqrt(2)
    )
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
        f"{peak_gain:.0f} V/V; -3 dB span: {passband[0]:.2f}-"
        f"{passband[-1]:.2f} Hz; assumed electrode source resistance: "
        "20 kohm per differential source."
    )
    console.print(
        "[yellow]Interpretation:[/yellow] the cascaded response peaks below 8 Hz; "
        "the RC corner labels do not make this a sharply selective 8-12 Hz filter."
    )


def artifact_fixture_tones(include_alpha: bool) -> tuple[SignalTone, ...]:
    """Return the authoritative simultaneous project-survival stimulus."""
    # Peak amplitudes are deliberately explicit. Differential signals are
    # split symmetrically between MEAS and REF; mains is common to both.
    tones = [
        SignalTone(2.0, 0.5e-3, -0.5e-3),  # 1 mV differential motion/drift
        SignalTone(30.0, 50e-6, -50e-6),  # 100 uV differential muscle-like
        SignalTone(60.0, 100e-3, 100e-3),  # 100 mV common-mode mains pickup
    ]
    if include_alpha:
        tones.append(SignalTone(10.0, 25e-6, -25e-6))  # 50 uV differential
    return tuple(tones)


def artifact_fixture_outputs(
    values: dict[str, str], include_alpha: bool
) -> tuple[tuple[float, complex], ...]:
    """Solve the explicit simultaneous artifact fixture at the ALPHA node."""
    model = eeg_path_model(values)
    return tuple(
        (
            tone.frequency_hz,
            simulate_electrode_inputs(
                model,
                tone.frequency_hz,
                tone.meas_peak_v,
                tone.ref_peak_v,
                meas_electrode_resistance=20_000.0,
                ref_electrode_resistance=100_000.0,
            ),
        )
        for tone in artifact_fixture_tones(include_alpha)
    )


def frontier_artifact_fixture_outputs(
    values: dict[str, str], include_alpha: bool, electrode: str = "wet"
) -> tuple[tuple[float, complex], ...]:
    """Solve active electrodes through the finite-A0/GBW LM324 acquisition."""
    model = eeg_path_model(values)
    meas_channel, ref_channel = active_electrode_channels(electrode)
    return tuple(
        (
            tone.frequency_hz,
            simulate_nonideal_active_electrode_inputs(
                model, tone.frequency_hz, tone.meas_peak_v, tone.ref_peak_v,
                meas_channel, ref_channel, LM324_ACQUISITION,
            ),
        )
        for tone in artifact_fixture_tones(include_alpha)
    )

