"""Project management implementation helpers."""

from manage_common import *  # noqa: F403
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


