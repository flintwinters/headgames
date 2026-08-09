#!/usr/bin/env python3
"""Single project entrypoint for circuit calculations and verification."""

from __future__ import annotations

from manage_common import *  # noqa: F403
from manage_sonification import *  # noqa: F403
from manage_sonification import _alpha_redesign_case, _broadband_redesign_case, _selected_spread_case, _sonification_frontier_case
from manage_electrodes import *  # noqa: F403
from manage_filters import *  # noqa: F403
from manage_spice import *  # noqa: F403
from manage_checks import *  # noqa: F403

@app.callback()
def main() -> None:
    """Calculate and verify the documented circuit design."""


@app.command()
def lint() -> None:
    """Enforce bounded Python module size through the project linter."""
    modules = sorted(
        module for module in PROJECT_ROOT.rglob("*.py")
        if not any(part.startswith(".") for part in module.relative_to(PROJECT_ROOT).parts)
    )
    require(modules, "no Python modules found to lint")
    subprocess.run(
        [sys.executable, "-m", "pylint", "--persistent=no",
         *(str(module) for module in modules)],
        cwd=PROJECT_ROOT,
        check=True,
    )


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
