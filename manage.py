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
            "ALPHA must drive U2D directly through R6")
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


@lru_cache(maxsize=4)
def cached_mfb_synthesis(value_items: tuple[tuple[str, str], ...]):
    """Avoid repeating the deterministic inventory search per physical build."""
    values = dict(value_items)
    return synthesize_mfb(read_inventory(BOM, values))


def nominal_sonification_build(values: dict[str, str], electrode: str = "wet"
                               ) -> SonificationBuild:
    """Materialize every part exercised by the end-to-end transient."""
    synthesis = cached_mfb_synthesis(tuple(sorted(values.items())))
    stage = MfbParts(synthesis.r1.nominal, synthesis.r2.nominal,
                     synthesis.r5.nominal, 100e-9, 100e-9)
    profile = electrode_profile(electrode)
    mismatch = 1.10 if electrode == "wet" else 1.22
    meas = ChannelParts(profile.series_resistance_ohm,
                        profile.charge_transfer_resistance_ohm,
                        profile.interface_capacitance_f, 5e-12, 1_000_000.0, 40e-9,
                        resistance(values["R16"]), resistance(values["R14"]),
                        resistance(values["R15"]), capacitance(values["C12"]),
                        resistance(values["R12"]), capacitance(values["C11"]),
                        8_200.0, 150e-12)
    ref = ChannelParts(profile.series_resistance_ohm*mismatch,
                       profile.charge_transfer_resistance_ohm*mismatch,
                       profile.interface_capacitance_f/mismatch, 5e-12, 1_000_000.0, 40e-9,
                       resistance(values["R19"]), resistance(values["R20"]),
                       resistance(values["R21"]), capacitance(values["C14"]),
                       resistance(values["R22"]), capacitance(values["C15"]),
                       8_200.0, 250e-12)
    return SonificationBuild(
        meas, ref, resistance(values["R17"])+0.5*resistance(values["RV1"]),
        capacitance(values["C13"]),
        resistance(values["R23"])+0.5*resistance(values["RV2"]),
        capacitance(values["C16"]), (stage, stage),
        resistance(values["R3"]), resistance(values["R4"]),
        resistance(values["R6"]), resistance(values["R9"]),
        capacitance(values["C10"]), resistance(values["R5"]),
        resistance(values["R8"]), capacitance(values["C5"]),
        capacitance(values["C6"]), resistance(values["R10"]),
        capacitance(values["C7"]), 9.0, 4.5,
        0.020, 2.0, 20.0, 50_000.0, 300_000.0, 0.250, 8.0,
    )


def sampled_sonification_build(values: dict[str, str], electrode: str,
                               rng: random.Random) -> SonificationBuild:
    """Move every declared physical leaf independently for one build."""
    nominal = nominal_sonification_build(values, electrode)
    def move(value: float, tolerance: float) -> float:
        return value*(1+rng.uniform(-tolerance, tolerance))
    matched_input_scale = 1+rng.uniform(-0.01, 0.01)
    matched_feedback_scale = 1+rng.uniform(-0.01, 0.01)
    def channel(item: ChannelParts) -> ChannelParts:
        return replace(
            item,
            electrode_series_ohm=move(item.electrode_series_ohm, 0.20),
            electrode_charge_transfer_ohm=move(item.electrode_charge_transfer_ohm, 0.20),
            electrode_interface_f=move(item.electrode_interface_f, 0.20),
            buffer_input_f=move(item.buffer_input_f, 0.50),
            buffer_bandwidth_hz=rng.uniform(0.5e6, 2.0e6),
            buffer_noise_v_rt_hz=move(item.buffer_noise_v_rt_hz, 0.25),
            safety_a_ohm=move(item.safety_a_ohm, 0.05),
            safety_b_ohm=move(item.safety_b_ohm, 0.05),
            input_ohm=item.input_ohm*matched_input_scale,
            input_f=move(item.input_f, 0.10),
            feedback_ohm=item.feedback_ohm*matched_feedback_scale,
            feedback_f=move(item.feedback_f, 0.10),
            cable_isolation_ohm=move(item.cable_isolation_ohm, 0.05),
            cable_f=move(item.cable_f, 0.20),
        )
    synthesis = cached_mfb_synthesis(tuple(sorted(values.items())))
    stages = []
    for _ in range(2):
        stages.append(MfbParts(
            synthesis.r1.sample(rng), synthesis.r2.sample(rng),
            synthesis.r5.sample(rng), move(100e-9, 0.10), move(100e-9, 0.10),
        ))
    supply = rng.uniform(8.8, 9.2)
    return replace(
        nominal, meas=channel(nominal.meas), ref=channel(nominal.ref),
        alpha_input_ohm=move(nominal.alpha_input_ohm, 0.05),
        alpha_input_f=move(nominal.alpha_input_f, 0.10),
        alpha_feedback_ohm=move(nominal.alpha_feedback_ohm, 0.05),
        alpha_feedback_f=move(nominal.alpha_feedback_f, 0.10),
        mfb=tuple(stages), r3_ohm=move(nominal.r3_ohm, 0.05),
        r4_ohm=move(nominal.r4_ohm, 0.05), r6_ohm=move(nominal.r6_ohm, 0.05),
        r9_ohm=move(nominal.r9_ohm, 0.01), c10_f=move(nominal.c10_f, 0.10),
        r5_audio_ohm=move(nominal.r5_audio_ohm, 0.05),
        r8_audio_ohm=move(nominal.r8_audio_ohm, 0.05),
        c5_audio_f=move(nominal.c5_audio_f, 0.10),
        c6_output_f=move(nominal.c6_output_f, 0.10),
        r10_zobel_ohm=move(nominal.r10_zobel_ohm, 0.05),
        c7_zobel_f=move(nominal.c7_zobel_f, 0.10),
        supply_v=supply, vref_v=supply/2+rng.uniform(-50e-3, 50e-3),
        oscillator_low_v=rng.uniform(0.005, 0.100),
        oscillator_high_headroom_v=rng.uniform(1.5, 2.5),
        lm386_input_ohm=move(nominal.lm386_input_ohm, 0.20),
        lm386_bandwidth_hz=move(nominal.lm386_bandwidth_hz, 0.20),
        lm386_output_power_w=rng.uniform(0.250, 0.325),
        speaker_ohm=move(nominal.speaker_ohm, 0.10),
    )


def print_sonification_result(candidate, result, build) -> None:
    item = result.worst
    delay = max(sonification_group_delay(build, candidate, frequency)
                for frequency in (8.0, 9.0, 10.0, 11.0, 12.0))
    table = Table(title=f"End-to-end speaker-current validation — {candidate.name}")
    table.add_column("Metric")
    table.add_column("Worst result", justify="right")
    table.add_row("Phases actually executed", str(result.phases_executed))
    table.add_row("Maximum 8-12 Hz group delay", f"{delay*1e3:.2f} ms")
    table.add_row("Alpha/artifact speaker-current ratio", f"{item.modulation_ratio:.4f}")
    table.add_row("Alpha modulation / carrier", f"{item.alpha_to_carrier:.2%}")
    table.add_row("Carrier / duty", f"{item.frequency_hz:.1f} Hz / {item.duty_cycle:.1%}")
    table.add_row("Burst onset t10 / t90", f"{item.onset_t10_s*1e3:.1f} / {item.onset_t90_s*1e3:.1f} ms")
    table.add_row("Offset to 10%", f"{item.offset_t10_s*1e3:.1f} ms")
    table.add_row("First changed carrier edge", f"{item.first_edge_latency_s*1e3:.1f} ms")
    table.add_row("Speaker RMS current", f"{item.speaker_rms_a*1e3:.2f} mA")
    table.add_row("LM386 peak output current", f"{item.peak_lm386_current_a*1e3:.2f} mA")
    table.add_row("Minimum node margin", f"{item.minimum_node_margin_v:.3f} V")
    table.add_row("Gate", result.first_failure or "PASS")
    console.print(table)


def verify_sonification_integrity(values: dict[str, str]) -> None:
    """Prove endpoint, phase, perturbation, and derivative contracts are real."""
    build = nominal_sonification_build(values, "wet")
    candidate = next(item for item in CANDIDATES if item.name == "alpha")
    result = simulate_build(build, candidate, 4, sample_rate_hz=40_000.0,
                            noise_seed=0x54455354, noise_enabled=False)
    refined = simulate_build(build, candidate, 4, sample_rate_hz=80_000.0,
                             noise_seed=0x54455354, noise_enabled=False)
    require(result.phases_executed == 4, "sonification phase count was not executed")
    require(result.worst.speaker_rms_a > 0,
            "sonification did not reach the speaker-current endpoint")
    require(result.worst.alpha_modulation_rms_a > 0,
            "alpha did not produce measured speaker-current modulation")
    require(abs(result.worst.frequency_hz-refined.worst.frequency_hz)
            / refined.worst.frequency_hz <= 0.025,
            f"carrier frequency is not converged: {result.worst.frequency_hz:.3f}/"
            f"{refined.worst.frequency_hz:.3f} Hz")
    require(abs(result.worst.duty_cycle-refined.worst.duty_cycle) <= 0.02,
            f"carrier duty is not converged: {result.worst.duty_cycle:.5f}/"
            f"{refined.worst.duty_cycle:.5f}")
    require(abs(result.worst.modulation_ratio-refined.worst.modulation_ratio)
            / max(refined.worst.modulation_ratio, 1e-15) <= 0.15,
            f"speaker modulation ratio is not converged: "
            f"{result.worst.modulation_ratio:.6f}/{refined.worst.modulation_ratio:.6f}")
    for frequency in (8.0, 10.0, 12.0):
        numerical = sonification_group_delay(build, candidate, frequency)
        exact = closed_form_group_delay(build, candidate, frequency)
        require(abs(numerical-exact) <= 1e-7,
                f"group-delay derivative mismatch at {frequency:g} Hz")
    left = sampled_sonification_build(values, "wet", random.Random(0x53454544))
    right = sampled_sonification_build(values, "wet", random.Random(0x53454544))
    require(left == right, "physical sonification build is not deterministic")
    require(left.meas.safety_a_ohm != left.meas.safety_b_ohm,
            "independent safety-resistor leaves moved together")
    require(left.meas.input_ohm/left.ref.input_ohm
            == build.meas.input_ohm/build.ref.input_ohm,
            "measured R15/R21 pair did not move together")
    require(left.meas.feedback_ohm/left.ref.feedback_ohm
            == build.meas.feedback_ohm/build.ref.feedback_ohm,
            "matched R12/R22 pair did not move together")
    require(left.meas.electrode_series_ohm != build.meas.electrode_series_ohm,
            "electrode spread was not exercised")
    require(left.meas.cable_f != build.meas.cable_f,
            "cable-capacitance spread was not exercised")
    require(left.lm386_output_power_w != build.lm386_output_power_w,
            "LM386 output-power bound was not exercised")
    require(left.speaker_ohm != build.speaker_ohm,
            "speaker-resistance spread was not exercised")
    require(left.mfb[0] != left.mfb[1],
            "independent MFB stages moved together")
    require(ALPHA_REDESIGN_R6_OHM == (100_000.0, 68_000.0, 47_000.0),
            "alpha-redesign R6 coverage changed")


def _sonification_frontier_case(arguments):
    """Picklable worker for one complete build/candidate/R6 experiment."""
    candidate, r6, base, phase_steps, seed, build_index = arguments
    r6_rng = random.Random(seed ^ (build_index*0x9E3779B1) ^ int(r6))
    build = replace(base, r6_ohm=r6*(1+r6_rng.uniform(-0.05, 0.05)))
    result = simulate_build(
        build, candidate, phase_steps,
        noise_seed=seed ^ (build_index*0x85EBCA6B),
    )
    delay = max(sonification_group_delay(build, candidate, 8+0.1*index)
                for index in range(41))
    return candidate.name, r6, build_index, result, delay


def _selected_spread_case(arguments):
    """Run one picklable selected-path build with deterministic endpoint noise."""
    build_index, build, candidate, phase_steps, seed = arguments
    result = simulate_build(
        build, candidate, phase_steps,
        noise_seed=seed ^ (build_index*0x85EBCA6B),
    )
    return build_index, result


def _alpha_redesign_case(arguments):
    """Run one candidate/R6/build combination for the redesign experiment."""
    candidate, r6, build_index, build, phase_steps, seed = arguments
    if build_index:
        rng = random.Random(seed ^ (build_index*0x9E3779B1) ^ int(r6))
        build = replace(build, r6_ohm=r6*(1+rng.uniform(-0.05, 0.05)))
    else:
        build = replace(build, r6_ohm=r6)
    result = simulate_build(
        build, candidate, phase_steps,
        noise_seed=seed ^ (build_index*0x85EBCA6B),
    )
    delay = max(sonification_group_delay(build, candidate, 8+0.1*index)
                for index in range(41))
    return candidate.name, r6, build_index, result, delay


def nominal_broadband_parts(feedback_ohm: float) -> BroadbandParts:
    """Materialize the proposed flat-gain and 60 Hz notch physical leaves."""
    return BroadbandParts(BROADBAND_GAIN_INPUT_OHM, feedback_ohm,
                          332_000.0, 332_000.0, 8.0e-9, 8.0e-9)


def broadband_build_from(build: SonificationBuild, nominal: SonificationBuild
                         ) -> SonificationBuild:
    """Retune the existing differential stage to gentle 1/30 Hz edges."""
    def channel(item: ChannelParts, reference: ChannelParts) -> ChannelParts:
        return replace(item,
                       input_f=330e-9*(item.input_f/reference.input_f),
                       feedback_f=530e-12*(item.feedback_f/reference.feedback_f))
    return replace(build, meas=channel(build.meas, nominal.meas),
                   ref=channel(build.ref, nominal.ref))


def sampled_broadband_parts(feedback_ohm: float, rng: random.Random
                            ) -> BroadbandParts:
    """Move every notch/gain physical leaf independently and deterministically."""
    nominal = nominal_broadband_parts(feedback_ohm)
    move = lambda value, tolerance: value*(1+rng.uniform(-tolerance, tolerance))
    return BroadbandParts(
        move(nominal.gain_input_ohm, 0.001),
        move(nominal.gain_feedback_ohm, 0.001),
        move(nominal.notch_r1_ohm, 0.001), move(nominal.notch_r2_ohm, 0.001),
        move(nominal.notch_c1_f, 0.001), move(nominal.notch_c2_f, 0.001),
        nominal.notch_q,
    )


def _broadband_redesign_case(arguments):
    """Picklable complete-path worker with an explicit campaign identity."""
    feedback, r6, build_index, build, nominal, phase_steps, seed = arguments
    rng = random.Random(seed ^ (build_index*0x9E3779B1) ^ int(feedback) ^ int(r6))
    parts = (nominal_broadband_parts(feedback) if build_index == 0
             else sampled_broadband_parts(feedback, rng))
    physical = broadband_build_from(build, nominal)
    physical = replace(physical, r6_ohm=(r6 if build_index == 0
                                        else r6*(1+rng.uniform(-0.05, 0.05))))
    result = simulate_broadband_build(physical, parts, phase_steps)
    return feedback, r6, build_index, parts, result


def require_complete_broadband_campaign(
    identities: list[tuple[float, float, int]], expected: set[tuple[float, float, int]],
    results: list[BroadbandBuildResult], phase_steps: int,
) -> None:
    """Reject missing/duplicate cases and implicit or duplicated phase coverage."""
    require(len(identities) == len(set(identities)), "duplicate broadband campaign identity")
    require(set(identities) == expected, "missing or unexpected broadband campaign identity")
    for identity, result in zip(identities, results, strict=True):
        require(result.phase_ids == tuple(range(phase_steps)),
                f"{identity}: incomplete or duplicated phase identities")
        reported = {item.frequency_hz for item in result.frequencies}
        required = set(BROADBAND_WANTED_HZ+BROADBAND_SLOW_HZ+BROADBAND_REJECTION_HZ)
        require(reported == required, f"{identity}: incomplete frequency reporting")


def verify_broadband_integrity(values: dict[str, str]) -> None:
    """Durably prove topology math, independent leaves, and fail-closed coverage."""
    nominal = broadband_build_from(nominal_sonification_build(values, "wet"),
                                   nominal_sonification_build(values, "wet"))
    parts = nominal_broadband_parts(470_000.0)
    require(broadband_transfer_gain(nominal, parts, 10.0) > 0,
            "broadband path has zero transfer")
    require(broadband_transfer_gain(nominal, parts, 60.0)
            < broadband_transfer_gain(nominal, parts, 59.0),
            "60 Hz notch is not centered")
    require(broadband_group_delay(nominal, parts, 4.0) <= 0.030,
            "nominal broadband 4 Hz delay exceeds the declared gate")
    left = sampled_broadband_parts(470_000.0, random.Random(0x42524F41))
    right = sampled_broadband_parts(470_000.0, random.Random(0x42524F41))
    require(left == right, "broadband physical sampling is not deterministic")
    require(left.notch_r1_ohm != left.notch_r2_ohm,
            "independent notch resistors moved together")
    require(left.notch_c1_f != left.notch_c2_f,
            "independent notch capacitors moved together")
    fake = BroadbandBuildResult(tuple(range(4)), tuple(), 700, 0.5, 1, 0, False, False, ())
    try:
        require_complete_broadband_campaign([(390e3, 47e3, 0), (390e3, 47e3, 0)],
                                            {(390e3, 47e3, 0)}, [fake, fake], 4)
    except VerificationError:
        pass
    else:
        raise VerificationError("duplicate campaign deliberately passed")


def active_electrode_channels(electrode: str | None = None
) -> tuple[ActiveElectrodeChannel, ActiveElectrodeChannel]:
    """Return the stocked LM358N dual buffer and unequal cable loads."""
    shared = {
        "amplifier_name": ACTIVE_ELECTRODE_IC,
        "input_capacitance": 5e-12,
        "gain_bandwidth_hz": 1_000_000.0,
        "output_resistance": 100.0,
        "input_bias_current": 45e-9,
        "white_voltage_noise": 40e-9,
    }
    profile = None if electrode is None else electrode_profile(electrode)
    source = 20_000.0 if profile is None else abs(profile.impedance(10.0))
    mismatch = 5.0 if electrode is None else (1.10 if electrode == "wet" else 1.22)
    meas_interface = {} if profile is None else {
        "electrode_series_resistance": profile.series_resistance_ohm,
        "charge_transfer_resistance": profile.charge_transfer_resistance_ohm,
        "interface_capacitance": profile.interface_capacitance_f,
    }
    ref_interface = {} if profile is None else {
        "electrode_series_resistance": profile.series_resistance_ohm * mismatch,
        "charge_transfer_resistance": profile.charge_transfer_resistance_ohm * mismatch,
        "interface_capacitance": profile.interface_capacitance_f / mismatch,
    }
    return (
        ActiveElectrodeChannel(
            electrode_resistance=source,
            cable_capacitance=150e-12,
            **meas_interface,
            **shared,
        ),
        ActiveElectrodeChannel(
            electrode_resistance=source * mismatch,
            cable_capacitance=250e-12,
            **ref_interface,
            **shared,
        ),
    )


def verify_electrode_profiles() -> None:
    """Check declared electrode impedances without claiming loop stability."""
    ranges = {"wet": (20_000.0, 26_000.0), "dry": (40_000.0, 52_000.0)}
    for name, (low, high) in ranges.items():
        profile = electrode_profile(name)
        impedances = tuple(abs(profile.impedance(frequency)) for frequency in (1.0, 10.0, 100.0))
        require(low <= impedances[1] <= high,
                f"{name} electrode impedance at 10 Hz is {impedances[1]:.0f} ohm")
        require(impedances[0] >= impedances[1] >= impedances[2],
                f"{name} electrode impedance is not monotonic")


def active_artifact_fixture_outputs(
    values: dict[str, str], include_alpha: bool
) -> tuple[tuple[float, complex], ...]:
    """Solve the survival fixture through candidate electrode-site buffers."""
    model = eeg_path_model(values)
    meas_channel, ref_channel = active_electrode_channels()
    return tuple(
        (
            tone.frequency_hz,
            simulate_active_electrode_inputs(
                model,
                tone.frequency_hz,
                tone.meas_peak_v,
                tone.ref_peak_v,
                meas_channel,
                ref_channel,
            ),
        )
        for tone in artifact_fixture_tones(include_alpha)
    )


def verify_artifact_baseline_regression(values: dict[str, str]) -> None:
    """Regression-check imbalance conversion and the contaminated envelope."""
    model = eeg_path_model(values)
    balanced_common_mode = simulate_electrode_inputs(
        model, 60.0, 0.1, 0.1, 20_000.0, 20_000.0
    )
    assert abs(balanced_common_mode) < 1e-12

    direct = simulate_ac(model, 10.0).total_gain
    nodal = simulate_electrode_inputs(
        model, 10.0, 0.5, -0.5, 20_000.0, 20_000.0
    )
    assert abs(direct - nodal) < 1e-9

    release = resistance(values["R18"]) * capacitance(values["C17"])
    without_alpha = simulate_ideal_peak_detector(
        artifact_fixture_outputs(values, include_alpha=False), release
    )
    with_alpha = simulate_ideal_peak_detector(
        artifact_fixture_outputs(values, include_alpha=True), release
    )
    relative_change = (with_alpha.mean_v - without_alpha.mean_v) / without_alpha.mean_v
    assert 0.0 <= relative_change < 0.05, (
        f"artifact fixture behavior changed unexpectedly: {relative_change:.1%}"
    )


def print_artifact_simulation(values: dict[str, str]) -> None:
    """Report whether alpha survives the simultaneous artifact fixture."""
    release = resistance(values["R18"]) * capacitance(values["C17"])
    without_outputs = artifact_fixture_outputs(values, include_alpha=False)
    with_outputs = artifact_fixture_outputs(values, include_alpha=True)
    without_alpha = simulate_ideal_peak_detector(without_outputs, release)
    with_alpha = simulate_ideal_peak_detector(with_outputs, release)
    relative_change = (with_alpha.mean_v - without_alpha.mean_v) / without_alpha.mean_v

    contribution_table = Table(title="ALPHA-node artifact fixture contributions")
    contribution_table.add_column("Fixture")
    contribution_table.add_column("Applied peak", justify="right")
    contribution_table.add_column("ALPHA peak", justify="right")
    labels = (
        ("Motion/drift, differential", "1 mV @ 2 Hz"),
        ("Muscle-like, differential", "100 uV @ 30 Hz"),
        ("Mains, common mode", "100 mV @ 60 Hz"),
        ("Eyes-closed alpha, differential", "50 uV @ 10 Hz"),
    )
    for (label, applied), (_, output) in zip(labels, with_outputs, strict=True):
        contribution_table.add_row(label, applied, f"{abs(output):.3f} V")
    console.print(contribution_table)

    envelope_table = Table(title="Ideal 0.22 s peak-detector result")
    envelope_table.add_column("Simultaneous fixture")
    envelope_table.add_column("ENV mean above VREF", justify="right")
    envelope_table.add_column("ENV range above VREF", justify="right")
    envelope_table.add_row(
        "Artifacts only",
        f"{without_alpha.mean_v:.3f} V",
        f"{without_alpha.minimum_v:.3f}-{without_alpha.maximum_v:.3f} V",
    )
    envelope_table.add_row(
        "Artifacts + 50 uV alpha",
        f"{with_alpha.mean_v:.3f} V",
        f"{with_alpha.minimum_v:.3f}-{with_alpha.maximum_v:.3f} V",
    )
    console.print(envelope_table)
    verdict = "PASS" if relative_change >= 0.25 else "FAIL"
    color = "green" if verdict == "PASS" else "red"
    console.print(
        f"[{color}]{verdict}[/{color}]: adding alpha changes mean ENV by "
        f"{relative_change:.1%}; the provisional distinguishability criterion is 25%."
    )
    console.print(
        "[green]Baseline regression: PASS[/green] — reproduced the documented "
        "known-failing response. This is not neurofeedback acceptance."
    )


def verify_active_electrode_baseline_regression(values: dict[str, str]) -> None:
    """Regression-check the candidate active electrode against the same fixture."""
    meas_channel, ref_channel = active_electrode_channels()
    require(meas_channel.amplifier_name == ref_channel.amplifier_name == "LM358N",
            "active buffer IC changed")
    require(meas_channel.amplifier_name in INVENTORY_AMPLIFIER_ICS,
            "active buffer must be selected from stocked amplifier ICs")
    require(ACTIVE_ELECTRODE_CONDUCTORS == (
        "MEAS_BUFFERED", "REF_BUFFERED", "BIAS", "VCC_ISOLATED", "GND_ISOLATED",
    ), "five-conductor active electrode boundary changed")
    passive = artifact_fixture_outputs(values, include_alpha=True)
    active = active_artifact_fixture_outputs(values, include_alpha=True)
    assert len(passive) == len(active) == 4
    passive_mains = abs(passive[2][1])
    active_mains = abs(active[2][1])
    assert active_mains < passive_mains / 100, (
        f"active electrode did not reject imbalance-converted mains: {active_mains:.6f} V"
    )

    model = eeg_path_model(values)
    channel = meas_channel
    balanced_common_mode = simulate_active_electrode_inputs(
        model, 60.0, 0.1, 0.1, channel, channel
    )
    assert abs(balanced_common_mode) < 1e-12

    release = resistance(values["R18"]) * capacitance(values["C17"])
    artifacts = simulate_ideal_peak_detector(
        active_artifact_fixture_outputs(values, include_alpha=False), release
    )
    with_alpha = simulate_ideal_peak_detector(active, release)
    relative_change = (with_alpha.mean_v - artifacts.mean_v) / artifacts.mean_v
    # Active buffering is expected to remove cable/common-mode conversion, but
    # it cannot remove differential electrode motion. Preserve that distinction.
    assert relative_change < 0.25
    white_noise_rms = active_electrode_output_noise_rms(
        model, meas_channel, ref_channel
    )
    assert white_noise_rms < abs(active[3][1]) / 10


def print_active_electrode_simulation(values: dict[str, str]) -> None:
    """Compare the passive cable and candidate active-electrode architecture."""
    release = resistance(values["R18"]) * capacitance(values["C17"])
    passive_outputs = artifact_fixture_outputs(values, include_alpha=True)
    active_outputs = active_artifact_fixture_outputs(values, include_alpha=True)
    active_artifacts = simulate_ideal_peak_detector(
        active_artifact_fixture_outputs(values, include_alpha=False), release
    )
    active_with_alpha = simulate_ideal_peak_detector(active_outputs, release)
    relative_change = (
        active_with_alpha.mean_v - active_artifacts.mean_v
    ) / active_artifacts.mean_v

    table = Table(title="Passive cable versus candidate active electrodes")
    table.add_column("Fixture")
    table.add_column("Passive ALPHA peak", justify="right")
    table.add_column("Active ALPHA peak", justify="right")
    table.add_column("Change", justify="right")
    labels = ("2 Hz motion", "30 Hz muscle-like", "60 Hz common mode", "10 Hz alpha")
    for label, (_, passive), (_, active) in zip(
        labels, passive_outputs, active_outputs, strict=True
    ):
        change = abs(active) / abs(passive) if passive else 0.0
        table.add_row(label, f"{abs(passive):.3f} V", f"{abs(active):.3f} V", f"{change:.3f}x")
    console.print(table)

    meas_channel, ref_channel = active_electrode_channels()
    bias_error = meas_channel.input_bias_current * abs(
        ref_channel.electrode_resistance - meas_channel.electrode_resistance
    )
    white_noise_rms = active_electrode_output_noise_rms(
        eeg_path_model(values), meas_channel, ref_channel
    )
    assumptions = Table(title="Declared active-electrode assumptions")
    assumptions.add_column("Parameter")
    assumptions.add_column("MEAS", justify="right")
    assumptions.add_column("REF", justify="right")
    assumptions.add_row(
        "Electrode source resistance",
        f"{meas_channel.electrode_resistance / 1e3:.0f} kohm",
        f"{ref_channel.electrode_resistance / 1e3:.0f} kohm",
    )
    assumptions.add_row(
        "Cable capacitance",
        f"{meas_channel.cable_capacitance * 1e12:.0f} pF",
        f"{ref_channel.cable_capacitance * 1e12:.0f} pF",
    )
    assumptions.add_row("Buffer IC", meas_channel.amplifier_name, ref_channel.amplifier_name)
    assumptions.add_row(
        "Buffer output resistance",
        f"{meas_channel.output_resistance:.0f} ohm",
        f"{ref_channel.output_resistance:.0f} ohm",
    )
    assumptions.add_row(
        "Buffer GBW",
        f"{meas_channel.gain_bandwidth_hz / 1e6:g} MHz",
        f"{ref_channel.gain_bandwidth_hz / 1e6:g} MHz",
    )
    assumptions.add_row(
        "Input capacitance",
        f"{meas_channel.input_capacitance * 1e12:g} pF",
        f"{ref_channel.input_capacitance * 1e12:g} pF",
    )
    assumptions.add_row(
        "Input bias current",
        f"{meas_channel.input_bias_current * 1e9:g} nA",
        f"{ref_channel.input_bias_current * 1e9:g} nA",
    )
    assumptions.add_row(
        "White voltage noise",
        f"{meas_channel.white_voltage_noise * 1e9:g} nV/rtHz",
        f"{ref_channel.white_voltage_noise * 1e9:g} nV/rtHz",
    )
    console.print(assumptions)

    verdict = "PASS" if relative_change >= 0.25 else "FAIL"
    color = "green" if verdict == "PASS" else "red"
    console.print(
        f"Bias-current error from the declared 80 kohm electrode mismatch: "
        f"{bias_error * 1e6:.2f} uV DC (subsequently AC-coupled)."
    )
    console.print(
        f"Integrated 0.5-100 Hz ALPHA noise from declared white buffer noise and "
        f"electrode/safety-resistor Johnson noise: {white_noise_rms * 1e3:.2f} mV RMS."
    )
    console.print(
        f"Active artifacts-only ENV mean: {active_artifacts.mean_v:.3f} V; with "
        f"alpha: {active_with_alpha.mean_v:.3f} V."
    )
    console.print(
        f"[{color}]{verdict}[/{color}]: active electrodes change mean ENV by "
        f"{relative_change:.1%} when alpha is added; target is 25%."
    )
    console.print(
        "[green]Baseline regression: PASS[/green] — reproduced the documented "
        "known-failing active-electrode comparison; acceptance remains failed."
    )
    console.print(
        "[yellow]Interpretation:[/yellow] buffering isolates the cable/common-mode "
        "mismatch mechanism, but cannot reject differential electrode motion."
    )


def planned_sharper_filter() -> CascadedBandpass:
    """Return the planned two-biquad, unity-center-gain 8-12 Hz filter."""
    return CascadedBandpass.from_cutoffs(8.0, 12.0, stages=2)


def verify_physical_filter_synthesis() -> None:
    """Cross-check the proposed MFB parts, nodal solver, and ideal oracle."""
    parts = MfbStageParts()
    require(math.isclose(parts.center_hz, 9.79827727297, rel_tol=1e-10),
            f"physical MFB center changed: {parts.center_hz:.9f} Hz")
    require(math.isclose(parts.q, 1.56989199083, rel_tol=1e-10),
            f"physical MFB Q changed: {parts.q:.9f}")
    require(math.isclose(parts.center_gain, 1.0, rel_tol=1e-12),
            f"physical MFB center gain changed: {parts.center_gain:.9f}")
    oracle_opamp = OpAmpModel(dc_open_loop_gain=1e12, gain_bandwidth_hz=1e18)
    for frequency in (0.5, 2.0, 8.0, parts.center_hz, 12.0, 30.0, 100.0):
        nodal = solve_stage_ac(parts, oracle_opamp, frequency).transfer
        closed_form = ideal_stage_transfer(parts, frequency)
        require(abs(nodal - closed_form) <= 1e-9,
                f"MFB nodal/oracle disagreement at {frequency:g} Hz")
    physical = solve_stage_ac(parts, OpAmpModel(), parts.center_hz)
    require(abs(abs(physical.transfer) - 1.0) < 0.001,
            "finite-GBW LM358 model changes nominal center gain unexpectedly")
    require(abs(physical.summing_node_v_per_v) > 0.0,
            "physical solver did not expose the internal summing node")
    require(abs(physical.output_current_a_per_v) > 0.0,
            "physical solver did not expose source/load current")
    require(sum(1 for _ in component_corner_cases()) == 1_024,
            "physical filter must enumerate exactly 1,024 independent endpoints")
    require(FRONTIER_RESISTOR_BAND == 0.05,
            "unmarked resistors must use the specified 5% tolerance")
    require(FRONTIER_CAPACITOR_BAND == 0.10,
            "unmarked capacitors must use the specified 10% tolerance")
    left = bounded_stage_sample(random.Random(0x48454144), 0.05, 0.10)
    right = bounded_stage_sample(random.Random(0x48454144), 0.05, 0.10)
    require(left == right, "bounded Monte Carlo seed is not reproducible")


def verify_inventory_synthesis(values: dict[str, str]) -> None:
    """Prove BOM parsing, KiCad precedence, and deterministic network search."""
    inventory = read_inventory(BOM, values)
    require(bool(inventory), "inventory BOM contains no passive values")
    require(math.isclose(parse_compact_value("1.5n"), 1.5e-9, rel_tol=1e-15),
            "compact capacitor parsing changed")
    # The stale CSV comments for C11/C15 and C7 must never override KiCad.
    capacitor_values = {item.value for item in inventory if item.kind == "C"}
    require(capacitance(values["C11"]) in capacitor_values,
            "native C11 value did not override the stale BOM comment")
    require(capacitance(values["C7"]) in capacitor_values,
            "native C7 value did not override the stale BOM comment")
    first = synthesize_mfb(inventory)
    second = synthesize_mfb(inventory)
    require(first == second, "inventory MFB synthesis is not deterministic")
    require(first.part_count <= 14,
            "MFB synthesis exceeded four resistors per element")
    for network in (first.r1, first.r2, first.r5):
        low = network.endpoint((-1,) * network.part_count)
        high = network.endpoint((1,) * network.part_count)
        require(low < network.nominal < high,
                "physical-part tolerance endpoints were not propagated")


def print_inventory_synthesis(values: dict[str, str]) -> None:
    inventory = read_inventory(BOM, values)
    candidate = synthesize_mfb(inventory)
    table = Table(title="Inventory-aware MFB synthesis (per stage)")
    table.add_column("Element")
    table.add_column("Physical network")
    table.add_column("Effective value", justify="right")
    for name, network in (("R1", candidate.r1), ("R2", candidate.r2), ("R5", candidate.r5)):
        table.add_row(name, network.canonical, f"{network.nominal/1e3:.6g} kΩ")
    table.add_row("C3/C4", "one stocked 100 nF part each", "100 nF")
    table.add_row("f0 / Q / gain", "", f"{candidate.center_hz:.5f} Hz / {candidate.q:.5f} / {candidate.gain:.5f}")
    table.add_row("Physical parts", "", str(candidate.part_count))
    console.print(table)


def inventory_mfb_parts(values: dict[str, str], rng: random.Random | None = None) -> MfbStageParts:
    """Materialize one stage from the sole inventory-backed synthesis."""
    candidate = synthesize_mfb(read_inventory(BOM, values))
    if rng is None:
        return MfbStageParts(candidate.r1.nominal, candidate.r2.nominal,
                             candidate.r5.nominal, 100e-9, 100e-9)
    return MfbStageParts(
        candidate.r1.sample(rng), candidate.r2.sample(rng), candidate.r5.sample(rng),
        100e-9 * (1 + rng.uniform(-0.10, 0.10)),
        100e-9 * (1 + rng.uniform(-0.10, 0.10)),
    )


def sampled_inventory_mfb_parts(candidate, rng: random.Random) -> MfbStageParts:
    """Sample a pre-synthesized candidate without repeating the search."""
    return MfbStageParts(
        candidate.r1.sample(rng), candidate.r2.sample(rng), candidate.r5.sample(rng),
        100e-9 * (1 + rng.uniform(-0.10, 0.10)),
        100e-9 * (1 + rng.uniform(-0.10, 0.10)),
    )


def print_physical_filter_synthesis() -> None:
    parts, opamp = MfbStageParts(), OpAmpModel()
    table = Table(title="Candidate physical MFB filter — non-schematic")
    table.add_column("Quantity")
    table.add_column("Stage 1")
    table.add_column("Stage 2")
    table.add_row("R1 / R2 / R5", "255k / 64.9k / 510k", "255k / 64.9k / 510k")
    table.add_row("C3 / C4", "100n / 100n", "100n / 100n")
    table.add_row("Nominal f0", f"{parts.center_hz:.6f} Hz", f"{parts.center_hz:.6f} Hz")
    table.add_row("Nominal Q", f"{parts.q:.6f}", f"{parts.q:.6f}")
    table.add_row("Center gain", "−1.000 V/V", "−1.000 V/V")
    solved = solve_cascade_ac(parts, parts, opamp, parts.center_hz)
    table.add_row("Finite-op-amp cascade", f"{abs(solved.stage1.transfer):.6f}", f"{abs(solved.transfer):.6f}")
    table.add_row("Output current / Vin", f"{abs(solved.stage1.output_current_a_per_v)*1e6:.3f} µA/V", f"{abs(solved.stage2.output_current_a_per_v)*1e6:.3f} µA/V")
    console.print(table)
    console.print("[yellow]Candidate only:[/yellow] this network is not in headgames.kicad_sch.")


def _physical_filtered_outputs(
    outputs: tuple[tuple[float, complex], ...], first: MfbStageParts, second: MfbStageParts,
    opamp: OpAmpModel,
) -> tuple[tuple[float, complex], ...]:
    return tuple((frequency, output * solve_cascade_ac(first, second, opamp, frequency).transfer)
                 for frequency, output in outputs)


def _stress_metrics(
    values: dict[str, str], first: MfbStageParts, second: MfbStageParts,
    opamp: OpAmpModel, supply_v: float, detector_release_s: float,
    electrode: str,
) -> tuple[float, float, float]:
    artifacts = frontier_artifact_fixture_outputs(values, False, electrode)
    with_alpha = frontier_artifact_fixture_outputs(values, True, electrode)
    filtered_artifacts = _physical_filtered_outputs(artifacts, first, second, opamp)
    filtered_alpha = _physical_filtered_outputs(with_alpha, first, second, opamp)
    artifact_env = simulate_ideal_peak_detector(filtered_artifacts, detector_release_s,
                                          duration_seconds=2.0, sample_rate_hz=240.0,
                                          measurement_seconds=1.0)
    alpha_env = simulate_ideal_peak_detector(filtered_alpha, detector_release_s,
                                       duration_seconds=2.0, sample_rate_hz=240.0,
                                       measurement_seconds=1.0)
    change = (alpha_env.mean_v - artifact_env.mean_v) / artifact_env.mean_v
    stage1_peak = sum(abs(output * solve_stage_ac(first, opamp, frequency).transfer)
                      for frequency, output in with_alpha)
    stage2_peak = sum(abs(output) for _, output in _physical_filtered_outputs(
        with_alpha, first, second, opamp))
    vref = supply_v / 2
    upper = supply_v - opamp.output_high_headroom_v
    acquisition = eeg_path_model(values)
    # The AC-coupled 474 kohm input arms are open at DC, so offset sees unity
    # DC noise gain. Matched input bias currents cancel through the equal 10 Mohm
    # paths; the residual is set by input offset current rather than full bias.
    acquisition_dc_error = (
        LM324_ACQUISITION.input_offset_v
        + LM324_TYPICAL_INPUT_OFFSET_CURRENT_A
        * acquisition.diff_feedback_resistance
    )
    margin = (
        min(vref - LM324_ACQUISITION.output_low_v,
            supply_v - LM324_ACQUISITION.output_high_headroom_v - vref)
        - max(sum(abs(output) for _, output in with_alpha), stage1_peak, stage2_peak)
        - acquisition_dc_error
    )
    current = max(
        abs(output) * abs(solve_stage_ac(first, opamp, frequency).output_current_a_per_v)
        for frequency, output in with_alpha
    )
    return change, margin, current


def run_filter_stress(
    values: dict[str, str], tier: str, samples: int, seed: int,
    electrode: str = "wet",
) -> FilterStressResult:
    """Run the active-electrode frontier in a tight nominal operating band."""
    require(tier == "build", "only the nominal operating frontier is supported")
    require(samples >= 0, "samples must be non-negative")
    require(electrode in ("wet", "dry"), "electrode must be wet or dry")
    opamp = OpAmpModel()
    release = resistance(values["R18"]) * capacitance(values["C17"])
    minimum_change = math.inf
    minimum_margin = math.inf
    maximum_current = 0.0
    worst = "none"
    first_failure = None
    cases = 0

    def consume(label: str, first: MfbStageParts, second: MfbStageParts, supply: float) -> None:
        nonlocal minimum_change, minimum_margin, maximum_current, worst, first_failure, cases
        change, margin, current = _stress_metrics(
            values, first, second, opamp, supply, release, electrode
        )
        cases += 1
        if change < minimum_change or margin < minimum_margin:
            worst = label
        minimum_change = min(minimum_change, change)
        minimum_margin = min(minimum_margin, margin)
        maximum_current = max(maximum_current, current)
        failure = (
            f"{label}: alpha change {change:.1%}" if change < 0.25 else
            f"{label}: node margin {margin:.3f} V" if margin < 0.250 else
            f"{label}: output current {current*1e3:.3f} mA" if current > opamp.output_current_a else None
        )
        if failure is not None and first_failure is None:
            first_failure = failure

    candidate = synthesize_mfb(read_inventory(BOM, values))
    nominal = MfbStageParts(candidate.r1.nominal, candidate.r2.nominal,
                            candidate.r5.nominal, 100e-9, 100e-9)
    consume("nominal", nominal, nominal, FRONTIER_NOMINAL_SUPPLY_V)
    rng = random.Random(seed)
    for index in range(samples):
        supply = rng.uniform(
            FRONTIER_NOMINAL_SUPPLY_V - FRONTIER_SUPPLY_BAND_V,
            FRONTIER_NOMINAL_SUPPLY_V + FRONTIER_SUPPLY_BAND_V,
        )
        first = sampled_inventory_mfb_parts(candidate, rng)
        second = sampled_inventory_mfb_parts(candidate, rng)
        consume(f"near-nominal:{index:05d}", first, second, supply)

    noise = integrated_output_noise_rms(nominal, nominal, opamp)
    alpha_peak = abs(frontier_artifact_fixture_outputs(values, True, electrode)[-1][1]) * abs(
        solve_cascade_ac(nominal, nominal, opamp, 10.0).transfer
    )
    recovery = recovery_bound_seconds(nominal, opamp, release)
    detector_artifacts = simulate_precision_peak_detector(
        _physical_filtered_outputs(
            frontier_artifact_fixture_outputs(values, False, electrode), nominal, nominal, opamp
        ),
        resistance(values["R18"]), capacitance(values["C17"]), 9.0, 4.5,
        LM358_DETECTOR, DETECTOR_DIODE, duration_seconds=3.0,
        sample_rate_hz=2_000.0, measurement_seconds=1.0,
    )
    detector_alpha = simulate_precision_peak_detector(
        _physical_filtered_outputs(
            frontier_artifact_fixture_outputs(values, True, electrode), nominal, nominal, opamp
        ),
        resistance(values["R18"]), capacitance(values["C17"]), 9.0, 4.5,
        LM358_DETECTOR, DETECTOR_DIODE, duration_seconds=3.0,
        sample_rate_hz=2_000.0, measurement_seconds=1.0,
    )
    physical_change = (
        detector_alpha.envelope.mean_v - detector_artifacts.envelope.mean_v
    ) / detector_artifacts.envelope.mean_v
    minimum_change = min(minimum_change, physical_change)
    minimum_margin = min(
        minimum_margin,
        detector_artifacts.minimum_output_margin_v,
        detector_alpha.minimum_output_margin_v,
        detector_artifacts.minimum_common_mode_margin_v,
        detector_alpha.minimum_common_mode_margin_v,
    )
    maximum_current = max(
        maximum_current,
        detector_artifacts.peak_output_current_a,
        detector_alpha.peak_output_current_a,
    )
    if (detector_artifacts.clipped_samples or detector_alpha.clipped_samples) and first_failure is None:
        first_failure = (
            "nominal detector: LM358 diode-drive output reaches its declared "
            "swing limit during normal rectifier operation"
        )
    if min(detector_artifacts.minimum_common_mode_margin_v,
           detector_alpha.minimum_common_mode_margin_v) < 0.250 and first_failure is None:
        first_failure = "nominal detector: common-mode margin is below 250 mV"
    if noise >= alpha_peak * 0.10 and first_failure is None:
        first_failure = f"noise {noise:.6g} V exceeds {alpha_peak*0.10:.6g} V"
    if recovery > 2.0 and first_failure is None:
        first_failure = f"recovery bound {recovery:.3f} s exceeds 2 s"
    return FilterStressResult(tier, cases, worst, minimum_change, minimum_margin,
                              maximum_current, noise, alpha_peak * 0.10, recovery,
                              first_failure)


def require_spice_models() -> None:
    """Fail closed when ngspice or either source-locked model is absent."""
    require(shutil.which("ngspice") is not None,
            "ngspice is required for circuit-level verification but was not found")
    expected = {
        PROJECT_ROOT / "models" / "ti" / "lmx58_lm2904.lib":
            "467a3e573420d1f5a21fab57b76be0e13073e854f609a73459a191958e314726",
        PROJECT_ROOT / "models" / "compat" / "lmx24_lmx58_nominal.lib":
            "8334c31c9a13f76d63232295e2d2ad73c5d0f99c17f30a5adc5ba68335ccb3d8",
    }
    for path, digest in expected.items():
        require(path.is_file(), f"required SPICE model is missing: {path.relative_to(PROJECT_ROOT)}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        require(actual == digest,
                f"SPICE model hash is not locked or mismatched: {path.name}")


def generate_filter_spice_deck() -> Path:
    """Generate the candidate-only cross-check deck under build/spice."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    output_dir.mkdir(parents=True, exist_ok=True)
    model = (
        PROJECT_ROOT / "models" / "compat" / "lmx24_lmx58_nominal.lib"
    ).resolve()
    deck = output_dir / "physical_filter_ac.cir"
    deck.write_text(f"""Headgames generated physical MFB AC cross-check
.include {model}
VCC vcc 0 9
VREF vref 0 4.5
VIN in vref dc 0 ac 1
R1A in x1 255k
R2A x1 vref 64.9k
C3A x1 n1 100n
C4A out1 x1 100n
R5A out1 n1 510k
XFA vref n1 vcc 0 out1 HG_LMX24_LMX58_NOMINAL
R1B out1 x2 255k
R2B x2 vref 64.9k
C3B x2 n2 100n
C4B out2 x2 100n
R5B out2 n2 510k
XFB vref n2 vcc 0 out2 HG_LMX24_LMX58_NOMINAL
.ac dec 1000 0.5 100
.print ac vm(out2) vp(out2)
.end
""", encoding="utf-8")
    return deck


def spice_nominal_model_dc_crosscheck() -> float:
    """Verify offset-current cancellation in a matched 10 Mohm DC fixture."""
    require_spice_models()
    output_dir = PROJECT_ROOT / "build" / "spice"
    output_dir.mkdir(parents=True, exist_ok=True)
    model = (
        PROJECT_ROOT / "models" / "compat" / "lmx24_lmx58_nominal.lib"
    ).resolve()
    deck = output_dir / "nominal_model_dc.cir"
    deck.write_text(f"""Headgames nominal op-amp DC contract
.include {model}
VCC vcc 0 9
VREF vref 0 4.5
RPLUS plus vref 10meg
RFB out minus 10meg
XU plus minus vcc 0 out HG_LMX24_LMX58_NOMINAL
.op
.print op v(plus) v(minus) v(out)
.end
""", encoding="utf-8")
    completed = subprocess.run(
        ["ngspice", "-b", str(deck)], cwd=deck.parent,
        check=False, capture_output=True, text=True,
    )
    require(completed.returncode == 0,
            f"ngspice DC contract failed: {completed.stderr[-500:]}")
    rows = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0].isdigit():
            try:
                rows.append(tuple(float(value) for value in fields[1:]))
            except ValueError:
                continue
    require(len(rows) == 1, "ngspice DC contract produced no unique operating point")
    plus, minus, output = rows[0]
    require(abs(abs(plus - minus) - LM324_ACQUISITION.input_offset_v) <= 100e-6,
            f"nominal model input offset is {plus-minus:.6g} V")
    dc_error = output - 4.5
    expected = (
        LM324_ACQUISITION.input_offset_v
        + LM324_TYPICAL_INPUT_OFFSET_CURRENT_A * 10_000_000.0
    )
    require(abs(abs(dc_error) - expected) <= 10e-3,
            f"nominal model DC error {dc_error:.6g} V differs from {expected:.6g} V")
    return dc_error


def spice_ti_dc_characterization() -> tuple[float, float]:
    """Instantiate TI's model through ngspice's PSpice compatibility frontend."""
    require_spice_models()
    output_dir = PROJECT_ROOT / "build" / "spice"
    output_dir.mkdir(parents=True, exist_ok=True)
    deck = output_dir / "ti_model_dc.cir"
    deck.write_text(f"""Headgames TI LM358 DC characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 4.5
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.op
.print op v(plus) v(out) i(VCC)
.end
""", encoding="utf-8")
    completed = subprocess.run(
        ["ngspice", "-D", "ngbehavior=ps", "-b", str(deck)], cwd=deck.parent,
                               check=False, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    require(completed.returncode == 0,
            f"TI model rejected by ngspice PSpice compatibility: {(completed.stderr or completed.stdout)[-800:]}")
    rows = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0].isdigit():
            try:
                rows.append(tuple(float(value) for value in fields[1:]))
            except ValueError:
                continue
    require(len(rows) == 1, "TI DC characterization produced no operating point")
    input_v, output_v, supply_a = rows[0]
    require(abs(output_v - input_v) <= 20e-3,
            f"TI follower DC error is {output_v-input_v:.6g} V")
    quiescent_a = abs(supply_a) - output_v / 10_000.0
    require(0.1e-3 <= quiescent_a <= 1e-3,
            f"TI quiescent supply current is {quiescent_a:.6g} A")
    return output_v - input_v, quiescent_a


def _run_ti_spice(deck: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["ngspice", "-D", "ngbehavior=ps", "-b", str(deck)], cwd=deck.parent,
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    require(completed.returncode == 0,
            f"TI PSpice compatibility run failed: {(completed.stderr or completed.stdout)[-800:]}")
    return completed


def _spice_table(stdout: str, columns: int) -> list[tuple[float, ...]]:
    rows: list[tuple[float, ...]] = []
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) == columns + 1 and fields[0].isdigit():
            try:
                rows.append(tuple(float(value) for value in fields[1:]))
            except ValueError:
                continue
    return rows


def spice_ti_ac_characterization() -> tuple[float, float, float]:
    """Measure the native TI model's follower gain, phase, and bandwidth."""
    require_spice_models()
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_follower_ac.cir"
    deck.write_text(f"""Headgames TI LM358 AC characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 dc 4.5 ac 1
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.ac dec 40 1 10meg
.print ac vm(out) vp(out)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 3)
    require(rows, "TI AC characterization produced no frequency points")
    low_frequency, low_gain, low_phase_rad = rows[0]
    require(0.98 <= low_gain <= 1.02, f"TI follower LF gain is {low_gain:.6g}")
    low_phase_deg = math.degrees(low_phase_rad)
    require(abs(low_phase_deg) <= 2.0, f"TI follower LF phase is {low_phase_deg:.3f} degrees")
    target = low_gain / math.sqrt(2)
    bandwidth = min(rows, key=lambda row: abs(row[1] - target))[0]
    require(0.5e6 <= bandwidth <= 2.0e6,
            f"TI follower -3 dB bandwidth is {bandwidth:.6g} Hz")
    return low_gain, low_phase_deg, bandwidth


def spice_ti_bias_characterization() -> float:
    """Measure input bias current from a source-resistor voltage drop."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_bias_dc.cir"
    deck.write_text(f"""Headgames TI LM358 input-bias characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VBIAS bias 0 4.5
RSOURCE bias plus 1meg
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.op
.print op v(bias) v(plus)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 2)
    require(len(rows) == 1, "TI bias characterization produced no operating point")
    bias_v, plus_v = rows[0]
    current = abs(bias_v - plus_v) / 1e6
    require(5e-9 <= current <= 100e-9, f"TI input bias current is {current:.6g} A")
    return current


def spice_ti_slew_characterization() -> float:
    """Measure large-signal follower slew between 10% and 90% levels."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_slew_tran.cir"
    deck.write_text(f"""Headgames TI LM358 slew characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 pulse(2 6 1m 10n 10n 4m 10m)
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.tran 1u 4m
.print tran v(out)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 2)
    post = [(time, voltage) for time, voltage in rows if time >= 1e-3]
    require(post, "TI slew characterization produced no transient samples")
    t10 = next((time for time, voltage in post if voltage >= 2.4), None)
    t90 = next((time for time, voltage in post if voltage >= 5.6), None)
    require(t10 is not None and t90 is not None and t90 > t10,
            "TI slew characterization did not cross 10% and 90%")
    slew = 3.2 / (t90 - t10)
    require(0.1e6 <= slew <= 1.0e6, f"TI positive slew rate is {slew:.6g} V/s")
    return slew


def spice_ti_swing_characterization() -> tuple[float, float]:
    """Check loaded follower tracking across its declared common-mode range."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_swing_dc.cir"
    deck.write_text(f"""Headgames TI LM358 loaded output-swing characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 0.1
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.dc VIN 0.1 7.0 0.1
.print dc v(plus) v(out)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 3)
    require(rows, "TI swing characterization produced no sweep points")
    _, low_in, low_out = rows[0]
    _, high_in, high_out = rows[-1]
    require(0 <= low_out <= high_out <= 9.0, "TI loaded output escaped its rails")
    require(abs(low_out-low_in) <= 50e-3, f"TI low output tracking error is {low_out-low_in:.6g} V")
    require(abs(high_out-high_in) <= 100e-3, f"TI high output tracking error is {high_out-high_in:.6g} V")
    return low_out, high_out


def spice_ti_noise_characterization() -> float:
    """Integrate TI-model unity-follower output noise over 0.5–100 Hz."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_noise.cir"
    deck.write_text(f"""Headgames TI LM358 noise characterization
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 dc 4.5 ac 1
XU plus out vcc 0 out LMX58_LM2904
RL out 0 10k
.noise v(out) VIN dec 40 0.5 100
.print noise onoise_spectrum
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 2)
    require(len(rows) >= 2, "TI noise characterization produced no spectrum")
    variance = 0.0
    for (left_f, left_n), (right_f, right_n) in zip(rows, rows[1:]):
        variance += (right_f-left_f) * (left_n*left_n + right_n*right_n) / 2
    noise = math.sqrt(max(0.0, variance))
    require(0 < noise <= 100e-6, f"TI integrated 0.5-100 Hz noise is {noise:.6g} V RMS")
    return noise


def spice_ti_cable_transient(cable_pf: float = 250.0,
                             isolation_ohm: float = 100.0) -> tuple[float, float]:
    """Measure step overshoot and 2% settling through the physical 100 ohm isolator."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / f"ti_cable_{int(cable_pf)}pf_{isolation_ohm:g}ohm.cir"
    deck.write_text(f"""Headgames TI LM358 isolated cable transient
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VIN plus 0 pulse(4.4 4.6 1m 1u 1u 4m 10m)
XU plus raw vcc 0 raw LMX58_LM2904
RISO raw cable {isolation_ohm:g}
CCABLE cable 0 {cable_pf:g}p
RLOAD cable vref 474k
VLOAD vref 0 4.5
.tran 2u 3m
.print tran v(cable)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 2)
    require(rows, "TI cable transient produced no samples")
    post = [(time, voltage) for time, voltage in rows if time >= 1e-3]
    final_v = sum(voltage for _, voltage in post[-50:]) / min(50, len(post))
    step = final_v - 4.4
    require(step > 0.15, f"TI cable transient final step is only {step:.6g} V")
    overshoot = max(0.0, (max(voltage for _, voltage in post) - final_v) / step)
    band = abs(step) * 0.02
    settling = post[-1][0] - 1e-3
    for index, (time, voltage) in enumerate(post):
        if all(abs(later - final_v) <= band for _, later in post[index:]):
            settling = time - 1e-3
            break
    return overshoot, settling


def select_cable_isolation(values: dict[str, str]) -> tuple[float, float, float]:
    """Choose the smallest stocked resistor passing both TI cable loads."""
    stocked = sorted({item.value for item in read_inventory(BOM, values)
                      if item.kind == "R" and item.value >= 100.0})
    for resistor in stocked:
        results = tuple(spice_ti_cable_transient(cable, resistor)
                        for cable in (150.0, 250.0))
        if (max(result[0] for result in results) <= 0.20
                and max(result[1] for result in results) <= 1e-3):
            return resistor, max(result[0] for result in results), max(
                result[1] for result in results)
    raise VerificationError("no stocked cable-isolation resistor passes the TI model")


def spice_ti_rectifier_transient() -> tuple[float, float]:
    """Exercise the TI model in the precision rectifier's nonlinear loop."""
    output_dir = PROJECT_ROOT / "build" / "spice"
    deck = output_dir / "ti_rectifier_tran.cir"
    deck.write_text(f"""Headgames TI LM358 precision-rectifier transient
.include {TI_MODEL.resolve()}
VCC vcc 0 9
VREF vref 0 4.5
BINPUT alpha vref V=ternary_fcn(time>2 && time<3,0.2*sin(2*pi*10*(time-2)),0)
XRECT alpha hold vcc 0 drive LMX58_LM2904
D1 drive hold D4148
RDC drive hold 1g
RREL hold vref 220k
CHOLD hold vref 1u
.model D4148 D(Is=4n Rs=2 Cjo=4p N=1.9)
.ic v(hold)=4.5 v(drive)=4.5
.tran 500u 5.2 0 200u uic
.print tran v(hold)
.end
""", encoding="utf-8")
    rows = _spice_table(_run_ti_spice(deck).stdout, 2)
    require(rows, "TI rectifier transient produced no samples")
    baseline_values = [voltage-4.5 for time, voltage in rows if 1.5 <= time <= 1.9]
    require(baseline_values, "TI rectifier transient lacks a baseline")
    baseline = sum(baseline_values) / len(baseline_values)
    peak = max(voltage-4.5 for time, voltage in rows if 2.5 <= time <= 3.0)
    target = baseline + 0.1 * (peak-baseline)
    recovery = 2.0
    for time, voltage in rows:
        if time >= 3.0 and voltage-4.5 <= target:
            recovery = time-3.0
            break
    require(0.10 <= peak <= 0.30, f"TI rectifier held peak is {peak:.6g} V")
    require(recovery < 2.0, f"TI rectifier recovery is {recovery:.3f} s")
    return peak, recovery


def spice_filter_ac_crosscheck() -> tuple[float, float, float]:
    """Return nominal-model DC error and worst Python/SPICE AC errors."""
    require_spice_models()
    spice_ti_dc_characterization()
    dc_error = spice_nominal_model_dc_crosscheck()
    deck = generate_filter_spice_deck()
    completed = subprocess.run(["ngspice", "-b", str(deck)], cwd=deck.parent,
                               check=False, capture_output=True, text=True)
    require(completed.returncode == 0,
            f"ngspice failed: {(completed.stderr or completed.stdout)[-500:]}")
    points: list[tuple[float, float, float]] = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[0].isdigit():
            try:
                # ngspice 44 prints vp() in radians in batch tabular output.
                points.append((
                    float(fields[1]),
                    float(fields[2]),
                    math.degrees(float(fields[3])),
                ))
            except ValueError:
                continue
    require(points, "ngspice AC output contained no parseable points")
    parts, opamp = MfbStageParts(), OpAmpModel()
    worst_db = worst_phase = 0.0
    for target in (2.0, 8.0, 10.0, 12.0, 30.0, 60.0):
        frequency, magnitude, phase = min(points, key=lambda point: abs(point[0] - target))
        python_value = solve_cascade_ac(parts, parts, opamp, frequency).transfer
        worst_db = max(worst_db, abs(20 * math.log10(magnitude / abs(python_value))))
        phase_error = (phase - math.degrees(math.atan2(python_value.imag, python_value.real)) + 180) % 360 - 180
        worst_phase = max(worst_phase, abs(phase_error))
    require(worst_db <= 0.1, f"Python/SPICE AC magnitude differs by {worst_db:.3f} dB")
    require(worst_phase <= 1.0, f"Python/SPICE AC phase differs by {worst_phase:.3f} degrees")
    return dc_error, worst_db, worst_phase


def verify_filter_stress(values: dict[str, str], samples: int = FRONTIER_SAMPLES,
                         seed: int = 0x48454144) -> FilterStressResult:
    result = run_filter_stress(values, "build", samples, seed)
    require(result.cases == 1 + samples,
            f"frontier evaluated {result.cases}, expected {1 + samples}")
    require(result.first_failure is None,
            f"physical build-envelope failure: {result.first_failure}")
    spice_filter_ac_crosscheck()
    return result


def print_filter_stress(result: FilterStressResult) -> None:
    title = "Active-electrode physical MFB — nominal operating band"
    table = Table(title=title)
    table.add_column("Metric")
    table.add_column("Worst result", justify="right")
    table.add_row("Cases", f"{result.cases:,}")
    table.add_row("Worst coordinate", result.worst_coordinate)
    table.add_row("Minimum ENV alpha change", f"{result.minimum_alpha_change:.1%}")
    table.add_row("Minimum node margin", f"{result.minimum_node_margin_v:.3f} V")
    table.add_row("Maximum output current", f"{result.maximum_output_current_a*1e3:.3f} mA")
    table.add_row("Integrated 0.5–100 Hz noise", f"{result.noise_rms_v*1e6:.2f} µV RMS")
    table.add_row("Noise limit", f"{result.noise_limit_v*1e3:.3f} mV RMS")
    table.add_row("Pop recovery bound", f"{result.recovery_s:.3f} s")
    table.add_row("First failure", result.first_failure or "none in Python tier")
    console.print(table)


def filtered_outputs(
    outputs: tuple[tuple[float, complex], ...],
    bandpass: CascadedBandpass,
    center_scales: tuple[float, ...] | None = None,
    q_scales: tuple[float, ...] | None = None,
) -> tuple[tuple[float, complex], ...]:
    """Apply the planned post-ALPHA filter to solved spectral contributions."""
    return tuple(
        (
            frequency,
            output * bandpass.transfer(frequency, center_scales, q_scales),
        )
        for frequency, output in outputs
    )


def envelope_alpha_change(
    artifacts: tuple[tuple[float, complex], ...],
    with_alpha: tuple[tuple[float, complex], ...],
    release_seconds: float,
) -> tuple[float, EnvelopeResult, EnvelopeResult]:
    """Return relative mean-envelope change and both detector results."""
    artifact_envelope = simulate_ideal_peak_detector(artifacts, release_seconds)
    alpha_envelope = simulate_ideal_peak_detector(with_alpha, release_seconds)
    relative_change = (
        alpha_envelope.mean_v - artifact_envelope.mean_v
    ) / artifact_envelope.mean_v
    return relative_change, artifact_envelope, alpha_envelope


def assert_sharper_filter_simulation(values: dict[str, str]) -> None:
    """Regression-check the ideal synthesis target without gating hardware."""
    bandpass = planned_sharper_filter()
    assert math.isclose(abs(bandpass.transfer(bandpass.center_frequency_hz)), 1.0)
    assert math.isclose(magnitude_db(bandpass.transfer(8.0)), -3.0103, abs_tol=0.01)
    assert math.isclose(magnitude_db(bandpass.transfer(12.0)), -3.0103, abs_tol=0.01)
    assert 1.5 <= bandpass.section_q <= 1.7

    release = resistance(values["R18"]) * capacitance(values["C17"])
    architectures = (
        (artifact_fixture_outputs(values, False), artifact_fixture_outputs(values, True)),
        (
            active_artifact_fixture_outputs(values, False),
            active_artifact_fixture_outputs(values, True),
        ),
    )
    for artifacts, with_alpha in architectures:
        # The added filter cannot recover an upstream stage that has clipped.
        # Bound the sum of simultaneous ALPHA peaks against conservative headroom.
        assert sum(abs(output) for _, output in with_alpha) < 3.0
        relative_change, _, _ = envelope_alpha_change(
            filtered_outputs(artifacts, bandpass),
            filtered_outputs(with_alpha, bandpass),
            release,
        )
        assert relative_change >= 0.25, (
            f"planned filter does not distinguish alpha: {relative_change:.1%}"
        )
        for center_signs in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
            for q_signs in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                center_scales = tuple(1 + 0.02 * sign for sign in center_signs)
                q_scales = tuple(1 + 0.05 * sign for sign in q_signs)
                corner_change, _, _ = envelope_alpha_change(
                    filtered_outputs(artifacts, bandpass, center_scales, q_scales),
                    filtered_outputs(with_alpha, bandpass, center_scales, q_scales),
                    release,
                )
                assert corner_change >= 0.25, (
                    f"filter coefficient corner fails: {corner_change:.1%}"
                )


def print_sharper_filter_simulation(values: dict[str, str]) -> None:
    """Report the planned filter's effect on passive and active fixtures."""
    bandpass = planned_sharper_filter()
    release = resistance(values["R18"]) * capacitance(values["C17"])
    table = Table(title="Planned two-biquad 8-12 Hz filter")
    table.add_column("Architecture")
    table.add_column("2 Hz", justify="right")
    table.add_column("30 Hz", justify="right")
    table.add_column("60 Hz", justify="right")
    table.add_column("10 Hz alpha", justify="right")
    table.add_column("ENV alpha change", justify="right")

    architecture_outputs = (
        (
            "Passive electrodes",
            artifact_fixture_outputs(values, False),
            artifact_fixture_outputs(values, True),
        ),
        (
            "Active electrodes",
            active_artifact_fixture_outputs(values, False),
            active_artifact_fixture_outputs(values, True),
        ),
    )
    for label, artifacts, with_alpha in architecture_outputs:
        filtered_artifacts = filtered_outputs(artifacts, bandpass)
        filtered_with_alpha = filtered_outputs(with_alpha, bandpass)
        relative_change, artifact_envelope, alpha_envelope = envelope_alpha_change(
            filtered_artifacts, filtered_with_alpha, release
        )
        corner_changes = []
        for center_signs in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
            for q_signs in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                center_scales = tuple(1 + 0.02 * sign for sign in center_signs)
                q_scales = tuple(1 + 0.05 * sign for sign in q_signs)
                corner_change, _, _ = envelope_alpha_change(
                    filtered_outputs(artifacts, bandpass, center_scales, q_scales),
                    filtered_outputs(with_alpha, bandpass, center_scales, q_scales),
                    release,
                )
                corner_changes.append(corner_change)
        peaks = [abs(output) for _, output in filtered_with_alpha]
        table.add_row(
            label,
            f"{peaks[0]:.3f} V",
            f"{peaks[1]:.3f} V",
            f"{peaks[2]:.6f} V",
            f"{peaks[3]:.3f} V",
            f"{relative_change:.0%} (corner {min(corner_changes):.0%})",
        )
        table.add_row(
            "  mean ENV",
            "",
            "",
            f"artifacts {artifact_envelope.mean_v:.3f} V",
            f"+ alpha {alpha_envelope.mean_v:.3f} V",
            "TARGET MET" if relative_change >= 0.25 else "TARGET MISSED",
        )
    console.print(table)
    console.print("[bold yellow]IDEAL TARGET — NON-GATING[/bold yellow]")
    console.print(
        f"Synthesized center: {bandpass.center_frequency_hz:.3f} Hz; "
        f"two identical sections at Q={bandpass.section_q:.3f}; unity gain at center."
    )
    console.print(
        "[yellow]Scope:[/yellow] ideal biquads plus independent +/-2% center and "
        "+/-5% Q coefficient corners; physical component mapping, op-amp limits, "
        "added noise, and overload recovery are not yet included."
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
    report = PROJECT_ROOT / f".headgames-test-erc-{os.getpid()}.rpt"
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


def assert_core_signal_path_drawn_explicitly() -> None:
    """Reject label jumps that make the safety-critical EEG path look open."""
    schematic = SCHEMATIC.read_text()
    hidden_path_labels = {
        name for name in ("MEAS_SITE", "REF_SITE", "MEAS_BUFFERED", "REF_BUFFERED")
        if f'(label "{name}"' in schematic
    }
    require(not hidden_path_labels,
            "core EEG path uses hidden label jumps: "
            + ", ".join(sorted(hidden_path_labels)))


def assert_no_overlapping_wire_segments() -> None:
    """Reject collinear wire objects whose interiors overlap."""
    schematic = SCHEMATIC.read_text()
    segments = [
        tuple(float(value) for value in match)
        for match in re.findall(
            r"\(wire\s+\(pts\s+\(xy\s+([\d.]+)\s+([\d.]+)\)\s+"
            r"\(xy\s+([\d.]+)\s+([\d.]+)\)",
            schematic,
        )
    ]
    overlaps: list[tuple[tuple[float, ...], tuple[float, ...]]] = []
    for index, first in enumerate(segments):
        x1, y1, x2, y2 = first
        for second in segments[index + 1:]:
            x3, y3, x4, y4 = second
            horizontal = y1 == y2 == y3 == y4
            vertical = x1 == x2 == x3 == x4
            if horizontal:
                overlap = min(max(x1, x2), max(x3, x4)) - max(min(x1, x2), min(x3, x4))
            elif vertical:
                overlap = min(max(y1, y2), max(y3, y4)) - max(min(y1, y2), min(y3, y4))
            else:
                continue
            if overlap > 0:
                overlaps.append((first, second))
    require(not overlaps, f"overlapping schematic wire segments: {overlaps}")


@app.callback()
def main() -> None:
    """Calculate and verify the documented circuit design."""


@app.command()
def test() -> None:
    """Reproduce documented regressions; this is not circuit acceptance."""
    require_assertions_enabled()
    nets, values = schematic_data()
    assert_passives_have_values(values)
    assert_audio_input_path(nets)
    assert_vref_capacitor_isolated(nets)
    assert_audio_drive_bounded(values)
    assert_audio_output_stabilized(nets, values)
    assert_eeg_signal_path(nets, values)
    assert_eeg_simulation(values)
    verify_electrode_profiles()
    verify_physical_filter_synthesis()
    verify_inventory_synthesis(values)
    verify_sonification_integrity(values)
    verify_broadband_integrity(values)
    threshold_low = (4.5 + 0.020 + 4.5) / 3
    threshold_high = (4.5 + 7.0 + 4.5) / 3
    expected_carrier = relaxation_frequency(100_000.0, 10e-9, 0.020, 7.0,
                                            threshold_low, threshold_high)
    require(500 <= expected_carrier <= 1_000,
            f"analytical carrier frequency is {expected_carrier:.1f} Hz")
    require(math.isfinite(expected_carrier), "analytical oscillator frequency is not finite")
    assert_isolated_battery_input(nets, values)
    assert_redundant_electrode_limiting(nets, values)
    assert_core_signal_path_drawn_explicitly()
    assert_no_overlapping_wire_segments()
    assert_erc_clean()
    require_spice_models()
    require_frontier_alignment()
    console.print(
        "[green]Regression suite passed.[/green] This reproduces documented "
        "results; it does not establish neurofeedback or hardware acceptance."
    )


@app.command()
def accept() -> None:
    """Require the selected electrode-to-speaker model and native topology."""
    nets, values = schematic_data()
    assert_eeg_signal_path(nets, values)
    require_frontier_alignment()
    candidate = next(item for item in CANDIDATES if item.name == "alpha")
    result = simulate_build(nominal_sonification_build(values, "wet"), candidate, 16)
    require(result.first_failure is None,
            f"nominal end-to-end sonification gate: {result.first_failure}")
    console.print("[bold green]MODEL ACCEPTANCE PASS: every declared model gate passed.[/bold green]")


@app.command("simulate-sonification")
def simulate_sonification(
    candidate: str = typer.Option(..., help="broadband|alpha|mfb1|mfb2"),
    electrode: str = typer.Option("wet", help="wet gating or dry informational profile"),
    phase_steps: int = typer.Option(16, min=16),
) -> None:
    """Run one nominal complete electrode-to-speaker-current experiment."""
    require(electrode in ("wet", "dry"), "electrode must be wet or dry")
    _, values = schematic_data()
    selected = next((item for item in CANDIDATES if item.name == candidate), None)
    require(selected is not None, "unknown candidate")
    build = nominal_sonification_build(values, electrode)
    result = simulate_build(build, selected, phase_steps)
    print_sonification_result(selected, result, build)
    require(result.phases_executed == phase_steps, "not every requested phase executed")
    require(result.first_failure is None, f"nominal sonification gate: {result.first_failure}")
    if electrode == "dry":
        console.print("[yellow]Dry-electrode verdict: INFORMATIONAL ONLY.[/yellow]")


@app.command("validate-selected-spreads")
def validate_selected_spreads(
    samples: int = typer.Option(32, min=1),
    seed: int = typer.Option(0x48454144, min=0),
    phase_steps: int = typer.Option(16, min=16),
    workers: int = typer.Option(4, min=1, max=32),
) -> None:
    """Run the selected wet path across explicit behavioral physical spreads."""
    _, values = schematic_data()
    candidate = next(item for item in CANDIDATES if item.name == "alpha")
    rng = random.Random(seed)
    builds = [nominal_sonification_build(values, "wet")]
    builds.extend(sampled_sonification_build(values, "wet", rng)
                  for _ in range(samples))
    tasks = [
        (index, build, candidate, phase_steps, seed)
        for index, build in enumerate(builds)
    ]
    results = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        for build_index, result in executor.map(
                _selected_spread_case, tasks, chunksize=1):
            require(result.phases_executed == phase_steps,
                    f"build {build_index}: phase coverage mismatch")
            results.append((build_index, result))
    expected = (samples+1)*phase_steps
    executed = sum(result.phases_executed for _, result in results)
    require(executed == expected,
            f"executed {executed} selected-path phases, expected {expected}")
    worst_alpha = min(result.worst.alpha_to_carrier for _, result in results)
    maximum_current = max(result.worst.peak_lm386_current_a for _, result in results)
    minimum_margin = min(result.worst.minimum_node_margin_v for _, result in results)
    failures = [(index, result.first_failure) for index, result in results
                if result.first_failure is not None]
    clipped_builds = sum(result.worst.clipped for _, result in results)
    latched_builds = sum(result.worst.latched for _, result in results)
    console.print(
        f"Executed exactly {samples+1} builds × {phase_steps} phases = {expected} "
        "complete wet-electrode speaker-current cases."
    )
    console.print(
        f"Worst alpha/carrier {worst_alpha:.2%}; peak modeled LM386/load current "
        f"{maximum_current*1e3:.1f} mA; minimum modeled node margin "
        f"{minimum_margin:.3f} V."
    )
    console.print(
        f"Behavioral clipping in {clipped_builds}/{len(results)} builds; "
        f"oscillator latching in {latched_builds}/{len(results)} builds."
    )
    require(not failures,
            f"selected spread campaign failed {len(failures)}/{len(results)} builds; "
            f"first is build {failures[0][0]}: {failures[0][1]}")


@app.command("evaluate-alpha-redesign")
def evaluate_alpha_redesign(
    samples: int = typer.Option(4, min=1),
    seed: int = typer.Option(0x414C5048, min=0),
    phase_steps: int = typer.Option(16, min=16),
    workers: int = typer.Option(4, min=1, max=32),
) -> None:
    """Compare 1/2/3-stage alpha weighting with identical R6 experiments."""
    _, values = schematic_data()
    candidates = tuple(item for item in CANDIDATES
                       if item.name in ("alpha", "mfb1", "mfb2"))
    r6_values = ALPHA_REDESIGN_R6_OHM
    rng = random.Random(seed)
    builds = [nominal_sonification_build(values, "wet")]
    builds.extend(sampled_sonification_build(values, "wet", rng)
                  for _ in range(samples))
    tasks = [
        (candidate, r6, build_index, build, phase_steps, seed)
        for candidate in candidates
        for r6 in r6_values
        for build_index, build in enumerate(builds)
    ]
    aggregates = {
        (candidate.name, r6): {
            "alpha": math.inf, "ratio": math.inf, "margin": math.inf,
            "current": 0.0, "delay": 0.0, "failures": [],
        }
        for candidate in candidates for r6 in r6_values
    }
    executed = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        for name, r6, build_index, result, delay in executor.map(
                _alpha_redesign_case, tasks, chunksize=1):
            require(result.phases_executed == phase_steps,
                    f"{name}/{r6:g}/build {build_index}: phase coverage mismatch")
            executed += result.phases_executed
            item = aggregates[(name, r6)]
            item["alpha"] = min(item["alpha"], result.worst.alpha_to_carrier)
            item["ratio"] = min(item["ratio"], result.worst.modulation_ratio)
            item["margin"] = min(item["margin"], result.worst.minimum_node_margin_v)
            item["current"] = max(item["current"], result.worst.peak_lm386_current_a)
            item["delay"] = max(item["delay"], delay)
            if result.first_failure:
                item["failures"].append((build_index, result.first_failure))
    expected = len(tasks)*phase_steps
    require(executed == expected,
            f"executed {executed} redesign phases, expected {expected}")
    table = Table(title="Alpha redesign — complete wet speaker-current experiment")
    table.add_column("Weighting / R6")
    table.add_column("Builds × phases", justify="right")
    table.add_column("α/carrier", justify="right")
    table.add_column("α/artifact", justify="right")
    table.add_column("Margin", justify="right")
    table.add_column("Delay", justify="right")
    table.add_column("Gate")
    feasible = []
    for candidate in candidates:
        for r6 in r6_values:
            item = aggregates[(candidate.name, r6)]
            failure = item["failures"][0][1] if item["failures"] else None
            table.add_row(
                f"{candidate.name} / {r6/1e3:g}k",
                f"{samples+1} × {phase_steps}", f"{item['alpha']:.2%}",
                f"{item['ratio']:.4f}", f"{item['margin']:.3f} V",
                f"{item['delay']*1e3:.1f} ms", failure or "PASS",
            )
            if not item["failures"]:
                feasible.append((candidate, r6, item))
    console.print(table)
    console.print(f"Executed exactly {expected} complete redesign phase cases.")
    require(feasible, "no alpha-weighting/R6 redesign passes every build and phase")
    passing = ", ".join(f"{candidate.name}/R6={r6/1e3:g} kΩ"
                        for candidate, r6, _ in feasible)
    console.print(f"[bold green]MODELED GATE PASS:[/bold green] {passing}")
    console.print(
        "[yellow]NO HARDWARE SELECTION:[/yellow] MFB inventory provenance, "
        "amplifier allocation, leaf-level spreads, and device-level nonlinear "
        "evidence remain incomplete."
    )


@app.command("evaluate-broadband-redesign")
def evaluate_broadband_redesign(
    samples: int = typer.Option(1, min=1),
    seed: int = typer.Option(0x42524F41, min=0),
    phase_steps: int = typer.Option(16, min=16),
    workers: int = typer.Option(4, min=1, max=32),
) -> None:
    """Compare every flat-gain/R6 pair using one identical wet-path campaign."""
    _, values = schematic_data()
    nominal = nominal_sonification_build(values, "wet")
    rng = random.Random(seed)
    builds = [nominal]
    builds.extend(sampled_sonification_build(values, "wet", rng) for _ in range(samples))
    tasks = [
        (feedback, r6, build_index, build, nominal, phase_steps, seed)
        for feedback in BROADBAND_GAIN_FEEDBACK_OHM
        for r6 in BROADBAND_R6_OHM
        for build_index, build in enumerate(builds)
    ]
    expected = {(feedback, r6, build_index)
                for feedback in BROADBAND_GAIN_FEEDBACK_OHM
                for r6 in BROADBAND_R6_OHM
                for build_index in range(samples+1)}
    rows: dict[tuple[float, float], list[BroadbandBuildResult]] = {
        (feedback, r6): [] for feedback in BROADBAND_GAIN_FEEDBACK_OHM
        for r6 in BROADBAND_R6_OHM
    }
    identities: list[tuple[float, float, int]] = []
    results: list[BroadbandBuildResult] = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        for feedback, r6, build_index, _parts, result in executor.map(
                _broadband_redesign_case, tasks, chunksize=1):
            identity = (feedback, r6, build_index)
            identities.append(identity)
            results.append(result)
            rows[(feedback, r6)].append(result)
    require_complete_broadband_campaign(identities, expected, results, phase_steps)
    table = Table(title="Broadband redesign — wet electrode to speaker current")
    table.add_column("Gain feedback / R6")
    table.add_column("Coverage", justify="right")
    table.add_column("Worst wanted", justify="right")
    table.add_column("59/60/61 rejection", justify="right")
    table.add_column("Worst delay", justify="right")
    table.add_column("Finding")
    feasible: list[tuple[float, float]] = []
    for feedback in BROADBAND_GAIN_FEEDBACK_OHM:
        for r6 in BROADBAND_R6_OHM:
            cases = rows[(feedback, r6)]
            frequency_rows = [item for case in cases for item in case.frequencies]
            wanted_ratio = min(item.modulation_to_carrier for item in frequency_rows
                               if item.purpose == "wanted")
            by_frequency = {frequency: [item for item in frequency_rows
                                        if item.frequency_hz == frequency]
                            for frequency in (59.0, 60.0, 61.0)}
            reference = min(item.transfer_gain for item in frequency_rows
                            if item.frequency_hz == 10.0)
            rejection = [20*math.log10(max(reference, 1e-30)/max(
                max(item.transfer_gain for item in by_frequency[frequency]), 1e-30))
                         for frequency in (59.0, 60.0, 61.0)]
            delay = max(item.delay_s for item in frequency_rows
                        if item.purpose == "wanted" and item.frequency_hz >= 4)
            failures = [failure for case in cases for failure in case.failures]
            table.add_row(
                f"{feedback/1e3:g}k / {r6/1e3:g}k",
                f"{samples+1} × {phase_steps} × {len(BROADBAND_WANTED_HZ+BROADBAND_REJECTION_HZ)}",
                f"{wanted_ratio:.3%}", "/".join(f"{value:.1f}" for value in rejection)+" dB",
                f"{delay*1e3:.1f} ms", failures[0] if failures else "all modeled gates pass",
            )
            if not failures:
                feasible.append((feedback, r6))
    console.print(table)
    frequency_table = Table(title="Per-frequency reporting (worst across every candidate/build)")
    frequency_table.add_column("Frequency")
    frequency_table.add_column("Purpose")
    frequency_table.add_column("Gain", justify="right")
    frequency_table.add_column("Delay", justify="right")
    frequency_table.add_column("Speaker modulation", justify="right")
    for frequency in sorted(set(BROADBAND_WANTED_HZ+BROADBAND_SLOW_HZ+BROADBAND_REJECTION_HZ)):
        items = [item for result in results for item in result.frequencies
                 if item.frequency_hz == frequency]
        purpose = items[0].purpose
        modulation = [item.speaker_modulation_rms_a for item in items
                      if item.speaker_modulation_rms_a is not None]
        frequency_table.add_row(
            f"{frequency:g} Hz", purpose, f"{min(item.transfer_gain for item in items):.5g}",
            f"{max(item.delay_s for item in items)*1e3:.2f} ms",
            "AC-only" if not modulation else f"{min(modulation)*1e6:.3f} µA RMS",
        )
    console.print(frequency_table)
    console.print(
        "Model boundaries: slow 0.1–0.5 Hz rows are AC characterization only; "
        "the notch Q and op-amps are behavioral; LM386 nonlinear recovery and "
        "real electrode-to-speaker behavior remain isolated-bench gated."
    )
    require(feasible, "no gain-feedback/R6 combination passes every identical complete-path gate")
    require(False, "hardware selection remains closed pending device-level notch coverage and isolated bench validation")


@app.command("simulate-sonification-frontier")
def simulate_sonification_frontier(
    electrode: str = typer.Option("wet", help="wet gating or dry informational profile"),
    samples: int = typer.Option(2_001, min=1),
    seed: int = typer.Option(0x48454144, min=0),
    phase_steps: int = typer.Option(16, min=16),
    workers: int = typer.Option(4, min=1, max=32),
) -> None:
    """Execute every physical build/phase and select only from passing paths."""
    require(electrode in ("wet", "dry"), "electrode must be wet or dry")
    _, values = schematic_data()
    inventory = read_inventory(BOM, values)
    r6_values = tuple(sorted({item.value for item in inventory
                              if item.kind == "R" and 47_000 <= item.value <= 1_000_000}))
    require(r6_values, "inventory has no plausible oscillator-control resistor")
    rng = random.Random(seed)
    builds = [nominal_sonification_build(values, electrode)]
    builds.extend(sampled_sonification_build(values, electrode, rng)
                  for _ in range(samples))
    tasks = [
        (candidate, r6, build, phase_steps, seed, build_index)
        for candidate in CANDIDATES for r6 in r6_values
        for build_index, build in enumerate(builds)
    ]
    aggregates = {
        (candidate.name, r6): [math.inf, math.inf, 0.0, None]
        for candidate in CANDIDATES for r6 in r6_values
    }
    total_phases = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        for name, r6, build_index, result, delay in executor.map(
                _sonification_frontier_case, tasks, chunksize=1):
            require(result.phases_executed == phase_steps,
                    f"{name}/{r6:g}: phase coverage mismatch")
            total_phases += result.phases_executed
            aggregate = aggregates[(name, r6)]
            aggregate[0] = min(aggregate[0], result.worst.modulation_ratio)
            aggregate[1] = min(aggregate[1], result.worst.alpha_to_carrier)
            aggregate[2] = max(aggregate[2], delay)
            if result.first_failure and aggregate[3] is None:
                aggregate[3] = f"build {build_index}: {result.first_failure}"
    rows = []
    for candidate in CANDIDATES:
        for r6 in r6_values:
            ratio, alpha, delay, failure = aggregates[(candidate.name, r6)]
            rows.append((candidate, r6, delay, ratio, alpha, failure))
    expected = len(CANDIDATES)*len(r6_values)*(samples+1)*phase_steps
    require(total_phases == expected,
            f"executed {total_phases} phases, expected {expected}")
    table = Table(title=f"Physical end-to-end sonification campaign — {electrode}")
    table.add_column("Candidate / R6")
    table.add_column("Builds × phases", justify="right")
    table.add_column("Worst delay", justify="right")
    table.add_column("Min speaker α/artifact", justify="right")
    table.add_column("Gate")
    for candidate, r6, delay, ratio, alpha, failure in rows:
        table.add_row(f"{candidate.name} / {r6/1e3:g}k", f"{samples+1} × {phase_steps}",
                      f"{delay*1e3:.2f} ms", f"{ratio:.4f}", failure or "PASS")
    console.print(table)
    feasible = [row for row in rows if row[-1] is None and row[0].name != "mfb2"]
    require(feasible, "no hardware candidate passes every physical build and phase")
    nondominated = [row for row in feasible if not any(
        other[2] <= row[2] and other[3] >= row[3]
        and (other[2] < row[2] or other[3] > row[3]) for other in feasible)]
    delays = [row[2] for row in nondominated]
    ratios = [row[3] for row in nondominated]
    delay_span = max(delays)-min(delays)
    ratio_span = max(ratios)-min(ratios)
    def score(row):
        return math.hypot(
            0 if delay_span == 0 else (row[2]-min(delays))/delay_span,
            0 if ratio_span == 0 else (max(ratios)-row[3])/ratio_span,
        )
    selected = min(nondominated, key=lambda row: (
        score(row), row[2], row[0].physical_parts, row[1]))
    console.print(f"Executed exactly {expected} complete speaker-current phase cases.")
    console.print(f"Selected physical knee: [bold]{selected[0].name}, "
                  f"R6={selected[1]/1e3:g} kΩ[/bold] (distance {score(selected):.4f}).")
    if electrode == "dry":
        console.print("[yellow]Dry-electrode verdict: INFORMATIONAL ONLY.[/yellow]")


@app.command("simulate-filter-network")
def simulate_filter_network() -> None:
    """Cross-check and report the candidate physical MFB network."""
    verify_physical_filter_synthesis()
    print_physical_filter_synthesis()


@app.command("synthesize-mfb")
def synthesize_mfb_command() -> None:
    """Audit inventory and report the deterministic stocked MFB network."""
    _, values = schematic_data()
    verify_inventory_synthesis(values)
    print_inventory_synthesis(values)


@app.command("characterize-ti-model")
def characterize_ti_model_command() -> None:
    """Run the first fail-closed translated-TI behavioral contract."""
    error, current = spice_ti_dc_characterization()
    gain, phase, bandwidth = spice_ti_ac_characterization()
    bias_current = spice_ti_bias_characterization()
    slew = spice_ti_slew_characterization()
    low_swing, high_swing = spice_ti_swing_characterization()
    noise = spice_ti_noise_characterization()
    detector_peak, detector_recovery = spice_ti_rectifier_transient()
    overshoot_150, settling_150 = spice_ti_cable_transient(150.0)
    overshoot_250, settling_250 = spice_ti_cable_transient(250.0)
    console.print(f"TI follower error: {error*1e3:.3f} mV; quiescent current: {current*1e3:.3f} mA.")
    console.print(f"TI follower LF gain/phase: {gain:.6f} / {phase:.3f}°; -3 dB bandwidth: {bandwidth/1e6:.3f} MHz.")
    console.print(f"TI input bias: {bias_current*1e9:.2f} nA; positive slew: {slew/1e6:.3f} V/µs; loaded swing: {low_swing:.3f}–{high_swing:.3f} V.")
    console.print(f"TI integrated 0.5–100 Hz follower noise: {noise*1e6:.3f} µV RMS.")
    console.print(f"TI precision-rectifier peak/recovery: {detector_peak:.3f} V / {detector_recovery:.3f} s.")
    console.print(f"TI isolated-cable overshoot: {overshoot_150:.1%} at 150 pF, {overshoot_250:.1%} at 250 pF; settling: {settling_150*1e6:.1f}/{settling_250*1e6:.1f} µs.")
    require(max(overshoot_150, overshoot_250) <= 0.20,
            "TI cable overshoot exceeds 20%")
    require(max(settling_150, settling_250) <= 1e-3,
            "TI cable settling exceeds 1 ms")


@app.command("characterize-buffer-transient")
def characterize_buffer_transient_command() -> None:
    """Exercise actual 8.2 kΩ buffer transient corners without claiming margin."""
    nets, values = schematic_data()
    assert_eeg_signal_path(nets, values)
    isolation = resistance(values["R24"])
    results = [
        spice_ti_cable_transient(cable_pf, isolation*riso_scale)
        for riso_scale in (0.95, 1.05)
        for cable_pf in (150.0, 250.0)
    ]
    require(len(results) == 4, "TI transient corner coverage is incomplete")
    overshoot = max(result[0] for result in results)
    settling = max(result[1] for result in results)
    require(overshoot <= 0.20, f"TI cable overshoot is {overshoot:.1%}")
    require(settling <= 1e-3, f"TI cable settling is {settling*1e6:.1f} us")
    console.print(
        "[green]NOMINAL TI TRANSIENT PASS[/green]: 4 Riso/cable corners; "
        f"worst overshoot {overshoot:.1%}, settling {settling*1e6:.1f} µs."
    )
    console.print(
        "[yellow]PHASE MARGIN UNRESOLVED:[/yellow] the TI macro-model does not "
        "converge with a valid loop break and has no process/temperature corners."
    )


@app.command("synthesize-cable-isolation")
def synthesize_cable_isolation_command() -> None:
    """Select the least stocked isolation resistance passing TI transients."""
    _, values = schematic_data()
    resistor, overshoot, settling = select_cable_isolation(values)
    console.print(f"Selected stocked cable isolation: {resistor:g} Ω; worst "
                  f"overshoot {overshoot:.1%}; settling {settling*1e6:.1f} µs.")


@app.command("compare-physical-frontier")
def compare_physical_frontier() -> None:
    """Compare the implemented KiCad blocks with the physical model boundary."""
    nets, values = schematic_data()
    assert_eeg_signal_path(nets, values)
    print_frontier_alignment()
    require_frontier_alignment()


@app.command("simulate-filter-stress")
def simulate_filter_stress(
    tier: str = typer.Option("build", help="nominal operating frontier"),
    samples: int = typer.Option(FRONTIER_SAMPLES, min=0),
    seed: int = typer.Option(0x48454144, min=0),
    electrode: str = typer.Option("wet", help="wet gating or dry informational profile"),
) -> None:
    """Evaluate the physical candidate near nominal operating conditions."""
    nets, values = schematic_data()
    assert_eeg_signal_path(nets, values)
    result = run_filter_stress(values, tier, samples, seed, electrode)
    print_filter_stress(result)
    if electrode == "dry":
        console.print("[yellow]Dry-electrode verdict: INFORMATIONAL ONLY.[/yellow]")
        return
    if tier == "build":
        require(result.first_failure is None,
                f"physical build-envelope failure: {result.first_failure}")
        try:
            dc_error, magnitude_error, phase_error = spice_filter_ac_crosscheck()
        except VerificationError:
            console.print("[bold red]Independent SPICE cross-check: BLOCKED[/bold red]")
            console.print("[bold red]Overall hardware gate: CLOSED[/bold red]")
            raise
        console.print(f"ngspice acquisition DC error: {dc_error*1e3:.2f} mV.")
        console.print(f"Python/SPICE AC agreement: {magnitude_error:.4f} dB, "
                      f"{phase_error:.4f}° worst case.")


@app.command("simulate-eeg")
def simulate_eeg() -> None:
    """Simulate the small-signal electrode-to-ALPHA response."""
    nets, values = schematic_data()
    assert_eeg_signal_path(nets, values)
    assert_eeg_simulation(values)
    print_eeg_simulation(values)


@app.command("simulate-artifacts")
def simulate_artifacts() -> None:
    """Test alpha distinguishability under explicit simultaneous artifacts."""
    nets, values = schematic_data()
    assert_eeg_signal_path(nets, values)
    verify_artifact_baseline_regression(values)
    print_artifact_simulation(values)


@app.command("simulate-active-electrodes")
def simulate_active_electrodes() -> None:
    """Compare passive cables with candidate unity-buffer active electrodes."""
    nets, values = schematic_data()
    assert_eeg_signal_path(nets, values)
    verify_active_electrode_baseline_regression(values)
    print_active_electrode_simulation(values)


@app.command("simulate-sharper-filter")
def simulate_sharper_filter() -> None:
    """Report the ideal, non-gating dual-biquad synthesis target."""
    nets, values = schematic_data()
    assert_eeg_signal_path(nets, values)
    assert_sharper_filter_simulation(values)
    print_sharper_filter_simulation(values)


if __name__ == "__main__":
    try:
        app()
    except VerificationError as error:
        console.print(f"[bold red]VERIFICATION FAILED:[/bold red] {error}")
        raise SystemExit(1) from None
    bounded_stage_sample,
    recovery_bound_seconds,
    DiodeModel,
