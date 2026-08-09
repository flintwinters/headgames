#!/usr/bin/env python3
"""Single project entrypoint for circuit calculations and verification."""

from __future__ import annotations

import math
import random
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import typer
from rich.console import Console
from rich.table import Table

from circuit_sim import (
    ActiveElectrodeChannel,
    CascadedBandpass,
    EegPathComponents,
    EnvelopeResult,
    SignalTone,
    active_electrode_output_noise_rms,
    logarithmic_sweep,
    magnitude_db,
    simulate_ac,
    simulate_active_electrode_inputs,
    simulate_electrode_inputs,
    simulate_peak_detector,
)
from physical_filter import (
    MfbStageParts,
    OpAmpModel,
    component_corner_cases,
    ideal_stage_transfer,
    integrated_output_noise_rms,
    solve_cascade_ac,
    solve_stage_ac,
)


app = typer.Typer(no_args_is_help=True)
console = Console()
PROJECT_ROOT = Path(__file__).resolve().parent
SCHEMATIC = PROJECT_ROOT / "headgames.kicad_sch"


class VerificationError(RuntimeError):
    """A durable project acceptance condition was not satisfied."""


def require(condition: bool, message: str) -> None:
    """Raise explicitly so optimization can never erase a verification gate."""
    if not condition:
        raise VerificationError(message)


def require_assertions_enabled() -> None:
    """Fail closed until legacy assertions have all migrated to ``require``."""
    require(__debug__, "python -O is forbidden: legacy verification assertions remain")


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


def active_electrode_channels(
) -> tuple[ActiveElectrodeChannel, ActiveElectrodeChannel]:
    """Return the declared candidate buffer and deliberately unequal cables."""
    shared = {
        "input_capacitance": 5e-12,
        "gain_bandwidth_hz": 1_000_000.0,
        "output_resistance": 100.0,
        "input_bias_current": 10e-12,
        "white_voltage_noise": 25e-9,
    }
    return (
        ActiveElectrodeChannel(
            electrode_resistance=20_000.0,
            cable_capacitance=150e-12,
            **shared,
        ),
        ActiveElectrodeChannel(
            electrode_resistance=100_000.0,
            cable_capacitance=250e-12,
            **shared,
        ),
    )


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


def assert_artifact_simulation(values: dict[str, str]) -> None:
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
    without_alpha = simulate_peak_detector(
        artifact_fixture_outputs(values, include_alpha=False), release
    )
    with_alpha = simulate_peak_detector(
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
    without_alpha = simulate_peak_detector(without_outputs, release)
    with_alpha = simulate_peak_detector(with_outputs, release)
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


def assert_active_electrode_simulation(values: dict[str, str]) -> None:
    """Regression-check the candidate active electrode against the same fixture."""
    passive = artifact_fixture_outputs(values, include_alpha=True)
    active = active_artifact_fixture_outputs(values, include_alpha=True)
    assert len(passive) == len(active) == 4
    passive_mains = abs(passive[2][1])
    active_mains = abs(active[2][1])
    assert active_mains < passive_mains / 100, (
        f"active electrode did not reject imbalance-converted mains: {active_mains:.6f} V"
    )

    model = eeg_path_model(values)
    channel, _ = active_electrode_channels()
    balanced_common_mode = simulate_active_electrode_inputs(
        model, 60.0, 0.1, 0.1, channel, channel
    )
    assert abs(balanced_common_mode) < 1e-12

    release = resistance(values["R18"]) * capacitance(values["C17"])
    artifacts = simulate_peak_detector(
        active_artifact_fixture_outputs(values, include_alpha=False), release
    )
    with_alpha = simulate_peak_detector(active, release)
    relative_change = (with_alpha.mean_v - artifacts.mean_v) / artifacts.mean_v
    # Active buffering is expected to remove cable/common-mode conversion, but
    # it cannot remove differential electrode motion. Preserve that distinction.
    assert relative_change < 0.25
    white_noise_rms = active_electrode_output_noise_rms(
        model, *active_electrode_channels()
    )
    assert white_noise_rms < abs(active[3][1]) / 10


def print_active_electrode_simulation(values: dict[str, str]) -> None:
    """Compare the passive cable and candidate active-electrode architecture."""
    release = resistance(values["R18"]) * capacitance(values["C17"])
    passive_outputs = artifact_fixture_outputs(values, include_alpha=True)
    active_outputs = active_artifact_fixture_outputs(values, include_alpha=True)
    active_artifacts = simulate_peak_detector(
        active_artifact_fixture_outputs(values, include_alpha=False), release
    )
    active_with_alpha = simulate_peak_detector(active_outputs, release)
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
    assumptions.add_row("Buffer output resistance", "100 ohm", "100 ohm")
    assumptions.add_row("Buffer GBW", "1 MHz", "1 MHz")
    assumptions.add_row("Input capacitance", "5 pF", "5 pF")
    assumptions.add_row("Input bias current", "10 pA", "10 pA")
    assumptions.add_row("White voltage noise", "25 nV/rtHz", "25 nV/rtHz")
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
    artifact_envelope = simulate_peak_detector(artifacts, release_seconds)
    alpha_envelope = simulate_peak_detector(with_alpha, release_seconds)
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
    require_assertions_enabled()
    nets, values = schematic_data()
    assert_passives_have_values(values)
    assert_audio_input_path(nets)
    assert_vref_capacitor_isolated(nets)
    assert_audio_drive_bounded(values)
    assert_audio_output_stabilized(nets, values)
    assert_eeg_signal_path(nets, values)
    assert_eeg_simulation(values)
    assert_artifact_simulation(values)
    assert_active_electrode_simulation(values)
    verify_physical_filter_synthesis()
    assert_precision_detector(nets, values)
    assert_isolated_battery_input(nets, values)
    assert_redundant_electrode_limiting(nets, values)
    assert_erc_clean()
    console.print("[green]Schematic connectivity checks passed.[/green]")


@app.command("simulate-filter-network")
def simulate_filter_network() -> None:
    """Cross-check and report the candidate physical MFB network."""
    verify_physical_filter_synthesis()
    print_physical_filter_synthesis()


@app.command("simulate-eeg")
def simulate_eeg() -> None:
    """Simulate the small-signal electrode-to-ALPHA response."""
    _, values = schematic_data()
    assert_eeg_simulation(values)
    print_eeg_simulation(values)


@app.command("simulate-artifacts")
def simulate_artifacts() -> None:
    """Test alpha distinguishability under explicit simultaneous artifacts."""
    _, values = schematic_data()
    assert_artifact_simulation(values)
    print_artifact_simulation(values)


@app.command("simulate-active-electrodes")
def simulate_active_electrodes() -> None:
    """Compare passive cables with candidate unity-buffer active electrodes."""
    _, values = schematic_data()
    assert_active_electrode_simulation(values)
    print_active_electrode_simulation(values)


@app.command("simulate-sharper-filter")
def simulate_sharper_filter() -> None:
    """Report the ideal, non-gating dual-biquad synthesis target."""
    _, values = schematic_data()
    assert_sharper_filter_simulation(values)
    print_sharper_filter_simulation(values)


if __name__ == "__main__":
    app()
