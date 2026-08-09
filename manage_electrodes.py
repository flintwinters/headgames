"""Project management implementation helpers."""

from manage_common import *  # noqa: F403
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


