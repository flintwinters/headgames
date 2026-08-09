#!/usr/bin/env python3
"""Single project entrypoint for circuit calculations and verification."""

from __future__ import annotations

import math
import hashlib
import os
import random
import shutil
import subprocess
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
from ti_model_translation import TRANSLATOR_REVISION, translate_ti_lmx58


app = typer.Typer(no_args_is_help=True)
console = Console()
PROJECT_ROOT = Path(__file__).resolve().parent
SCHEMATIC = PROJECT_ROOT / "headgames.kicad_sch"
BOM = PROJECT_ROOT / "headgames_bom.csv"
TI_MODEL = PROJECT_ROOT / "models" / "ti" / "lmx58_lm2904.lib"
TI_NGSPICE_MODEL = PROJECT_ROOT / "models" / "ngspice" / "lmx58_lm2904.lib"

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
    ("electrode-site active buffers", True, False),
    ("LM324 acquisition and ALPHA", True, True),
    ("two-stage post-ALPHA MFB filter", True, False),
    ("LM358 precision peak detector", True, True),
    ("carrier control and LM386 audio output", False, True),
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

    # MEAS reaches U2B's non-inverting input through two safety resistors and
    # the series C12/R15 input. R12||C11 returns that node to VREF.
    exact_net(("J2", "2"), {("J2", "2"), ("R16", "2")})
    exact_net(("R16", "1"), {("R16", "1"), ("R14", "2")})
    exact_net(("R14", "1"), {("R14", "1"), ("C12", "2")})
    exact_net(("C12", "1"), {("C12", "1"), ("R15", "2")})
    exact_net(("U2", "5"), {
        ("U2", "5"), ("R15", "1"), ("R12", "1"), ("C11", "1")
    })

    # REF reaches U2B's inverting input through the symmetric network;
    # R22||C15 closes feedback from pin 7 at DIFF_OUT.
    exact_net(("J2", "1"), {("J2", "1"), ("R19", "2")})
    exact_net(("R19", "1"), {("R19", "1"), ("R20", "2")})
    exact_net(("R20", "1"), {("R20", "1"), ("C14", "2")})
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
    """Check declared impedances and the isolated cable-driver stability gate."""
    ranges = {"wet": (20_000.0, 26_000.0), "dry": (40_000.0, 52_000.0)}
    for name, (low, high) in ranges.items():
        profile = electrode_profile(name)
        impedances = tuple(abs(profile.impedance(frequency)) for frequency in (1.0, 10.0, 100.0))
        require(low <= impedances[1] <= high,
                f"{name} electrode impedance at 10 Hz is {impedances[1]:.0f} ohm")
        require(impedances[0] >= impedances[1] >= impedances[2],
                f"{name} electrode impedance is not monotonic")
    for cable_pf in (150.0, 250.0):
        stability = follower_cable_stability(1_000_000.0, 100.0, cable_pf * 1e-12)
        require(stability.phase_margin_deg >= 45.0,
                f"LM358 cable phase margin is {stability.phase_margin_deg:.1f} degrees")
        require(stability.overshoot_fraction <= 0.20,
                f"LM358 cable overshoot is {stability.overshoot_fraction:.1%}")


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
    expected_translation = translate_ti_lmx58(TI_MODEL.read_text(encoding="utf-8"))
    require(TI_NGSPICE_MODEL.is_file(), "translated TI ngspice model is missing")
    require(TI_NGSPICE_MODEL.read_text(encoding="utf-8") == expected_translation,
            f"translated TI model is stale; regenerate with {TRANSLATOR_REVISION}")


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


def spice_ti_translation_smoke() -> tuple[float, float]:
    """Instantiate the translated TI model and check its nominal DC contract."""
    require_spice_models()
    output_dir = PROJECT_ROOT / "build" / "spice"
    output_dir.mkdir(parents=True, exist_ok=True)
    deck = output_dir / "ti_translation_dc.cir"
    deck.write_text(f"""Headgames translated TI LM358 DC characterization
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
            f"translated TI model rejected by ngspice: {(completed.stderr or completed.stdout)[-800:]}")
    rows = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0].isdigit():
            try:
                rows.append(tuple(float(value) for value in fields[1:]))
            except ValueError:
                continue
    require(len(rows) == 1, "translated TI DC characterization produced no operating point")
    input_v, output_v, supply_a = rows[0]
    require(abs(output_v - input_v) <= 20e-3,
            f"translated TI follower DC error is {output_v-input_v:.6g} V")
    require(0.1e-3 <= abs(supply_a) <= 2e-3,
            f"translated TI quiescent supply current is {supply_a:.6g} A")
    return output_v - input_v, abs(supply_a)


def spice_filter_ac_crosscheck() -> tuple[float, float, float]:
    """Return nominal-model DC error and worst Python/SPICE AC errors."""
    require_spice_models()
    spice_ti_translation_smoke()
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
    verify_artifact_baseline_regression(values)
    verify_active_electrode_baseline_regression(values)
    verify_electrode_profiles()
    verify_physical_filter_synthesis()
    verify_inventory_synthesis(values)
    assert_precision_detector(nets, values)
    assert_isolated_battery_input(nets, values)
    assert_redundant_electrode_limiting(nets, values)
    assert_erc_clean()
    python_stress = run_filter_stress(values, "build", 0, 0x48454144)
    require(python_stress.cases == 1,
            "nominal physical frontier must evaluate exactly once")
    require(python_stress.first_failure is None,
            f"nominal physical frontier failed: {python_stress.first_failure}")
    require(python_stress.minimum_node_margin_v >= 0.250,
            "nominal physical frontier lacks 250 mV node margin")
    require(python_stress.maximum_output_current_a > 0.0,
            "physical detector did not exercise finite diode/output current")
    require_spice_models()
    require(
        tuple(name for name, modeled, schematic in FRONTIER_ALIGNMENT if modeled != schematic)
        == (
            "electrode-site active buffers",
            "two-stage post-ALPHA MFB filter",
            "carrier control and LM386 audio output",
        ),
        "documented KiCad/model alignment audit changed without reconciliation",
    )
    console.print(
        "[green]Regression suite passed.[/green] This reproduces documented "
        "results; it does not establish neurofeedback or hardware acceptance."
    )


@app.command()
def accept() -> None:
    """Require every declared physical-filter candidate acceptance gate."""
    nets, values = schematic_data()
    assert_eeg_signal_path(nets, values)
    assert_precision_detector(nets, values)
    require_frontier_alignment()
    verify_filter_stress(values)
    console.print("[bold green]MODEL ACCEPTANCE PASS: every declared model gate passed.[/bold green]")


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


@app.command("translate-ti-model")
def translate_ti_model_command() -> None:
    """Regenerate the narrow ngspice translation from the hash-locked TI source."""
    TI_NGSPICE_MODEL.parent.mkdir(parents=True, exist_ok=True)
    TI_NGSPICE_MODEL.write_text(
        translate_ti_lmx58(TI_MODEL.read_text(encoding="utf-8")), encoding="utf-8"
    )
    console.print(f"Generated {TI_NGSPICE_MODEL.relative_to(PROJECT_ROOT)} using {TRANSLATOR_REVISION}.")


@app.command("characterize-ti-model")
def characterize_ti_model_command() -> None:
    """Run the first fail-closed translated-TI behavioral contract."""
    error, current = spice_ti_translation_smoke()
    console.print(f"Translated TI follower error: {error*1e3:.3f} mV; supply current: {current*1e3:.3f} mA.")


@app.command("compare-physical-frontier")
def compare_physical_frontier() -> None:
    """Compare the implemented KiCad blocks with the physical model boundary."""
    nets, values = schematic_data()
    assert_eeg_signal_path(nets, values)
    assert_precision_detector(nets, values)
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
