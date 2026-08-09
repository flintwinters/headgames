"""Inventory-backed passive networks for the physical MFB frontier."""

from __future__ import annotations

from dataclasses import dataclass
import csv
import math
from pathlib import Path
import re


_VALUE = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*([kMmunp]?)(?:[Ff]|(?:ohm))?\b")
_SCALE = {"": 1.0, "k": 1e3, "M": 1e6, "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12}


@dataclass(frozen=True)
class InventoryValue:
    kind: str
    value: float
    tolerance: float
    source: str
    quantity_on_hand: int | None


@dataclass(frozen=True)
class PassiveNetwork:
    operation: str
    children: tuple["PassiveNetwork", ...]
    nominal: float
    tolerance: float
    label: str

    @classmethod
    def leaf(cls, item: InventoryValue) -> "PassiveNetwork":
        return cls("part", (), item.value, item.tolerance, item.source)

    @property
    def part_count(self) -> int:
        return 1 if self.operation == "part" else sum(child.part_count for child in self.children)

    @property
    def canonical(self) -> str:
        if self.operation == "part":
            return self.label
        return f"{self.operation}({','.join(child.canonical for child in self.children)})"

    def endpoint(self, signs: tuple[int, ...]) -> float:
        """Evaluate one individual-physical-part tolerance endpoint."""
        iterator = iter(signs)
        def evaluate(node: PassiveNetwork) -> float:
            if node.operation == "part":
                return node.nominal * (1 + next(iterator) * node.tolerance)
            values = [evaluate(child) for child in node.children]
            if node.operation == "series":
                return sum(values)
            return 1 / sum(1 / value for value in values)
        return evaluate(self)

    def sample(self, random_source) -> float:
        """Sample every physical leaf independently within its bounded tolerance."""
        def evaluate(node: PassiveNetwork) -> float:
            if node.operation == "part":
                return node.nominal * (1 + random_source.uniform(-node.tolerance, node.tolerance))
            values = [evaluate(child) for child in node.children]
            if node.operation == "series":
                return sum(values)
            return 1 / sum(1 / value for value in values)
        return evaluate(self)


@dataclass(frozen=True)
class MfbSynthesis:
    r1: PassiveNetwork
    r2: PassiveNetwork
    r5: PassiveNetwork
    center_hz: float
    q: float
    gain: float
    response_error: float

    @property
    def part_count(self) -> int:
        return self.r1.part_count + self.r2.part_count + self.r5.part_count + 2


def _combine(operation: str, left: PassiveNetwork, right: PassiveNetwork) -> PassiveNetwork:
    children = tuple(sorted((left, right), key=lambda item: item.canonical))
    if operation == "series":
        nominal = left.nominal + right.nominal
    else:
        nominal = 1 / (1 / left.nominal + 1 / right.nominal)
    # This field is informational; sampling uses each leaf tolerance separately.
    tolerance = max(left.tolerance, right.tolerance)
    return PassiveNetwork(operation, children, nominal, tolerance, "")


def parse_compact_value(text: str) -> float:
    match = _VALUE.search(text.strip())
    if not match:
        raise ValueError(f"no compact value in {text!r}")
    return float(match.group(1)) * _SCALE[match.group(2)]


def read_inventory(path: Path, schematic_values: dict[str, str]) -> tuple[InventoryValue, ...]:
    """Read the BOM inventory union; native KiCad values override CSV comments."""
    items: dict[tuple[str, float], InventoryValue] = {}
    with path.open(newline="", encoding="utf-8-sig") as source:
        for row_number, row in enumerate(csv.DictReader(source), start=2):
            refs = tuple(ref.strip() for ref in row["Designator"].split(",") if ref.strip())
            if not refs or not refs[0].startswith(("R", "C")):
                continue
            kind = "R" if refs[0].startswith("R") else "C"
            authoritative = [schematic_values[ref] for ref in refs if ref in schematic_values]
            raw = authoritative[0] if authoritative else row["Comment"]
            value = parse_compact_value(raw)
            tolerance = 0.01 if "1%" in raw or "1%" in row["Part Description"] else (0.05 if kind == "R" else 0.10)
            qoh_text = row["QoH"].replace(",", "").strip()
            qoh = int(qoh_text) if qoh_text else None
            source_name = row["Part Number"].strip() or f"BOM:{row_number}:{raw.split()[0]}"
            key = kind, value
            previous = items.get(key)
            candidate = InventoryValue(kind, value, tolerance, source_name, qoh)
            if previous is None or candidate.tolerance < previous.tolerance:
                items[key] = candidate
    return tuple(sorted(items.values(), key=lambda item: (item.kind, item.value, item.source)))


def enumerate_networks(items: tuple[InventoryValue, ...], kind: str, max_parts: int = 4) -> tuple[PassiveNetwork, ...]:
    """Enumerate canonical series/parallel networks deterministically."""
    by_count: dict[int, dict[tuple[int, str], PassiveNetwork]] = {1: {}}
    for item in items:
        if item.kind == kind:
            leaf = PassiveNetwork.leaf(item)
            by_count[1][(round(math.log(leaf.nominal), 12), leaf.canonical)] = leaf
    for count in range(2, max_parts + 1):
        found: dict[tuple[int, str], PassiveNetwork] = {}
        for left_count in range(1, count):
            right_count = count - left_count
            for left in by_count[left_count].values():
                for right in by_count[right_count].values():
                    for operation in ("parallel", "series"):
                        network = _combine(operation, left, right)
                        # Canonical string removes construction-order duplicates.
                        found[(round(math.log(network.nominal), 12), network.canonical)] = network
        by_count[count] = found
    networks = [network for group in by_count.values() for network in group.values()]
    return tuple(sorted(networks, key=lambda item: (item.part_count, item.nominal, item.canonical)))


def closest_networks(networks: tuple[PassiveNetwork, ...], target: float, limit: int = 24) -> tuple[PassiveNetwork, ...]:
    return tuple(sorted(networks, key=lambda item: (abs(math.log(item.nominal / target)), item.part_count, item.canonical))[:limit])


def synthesize_mfb(items: tuple[InventoryValue, ...], max_parts: int = 4) -> MfbSynthesis:
    """Find the deterministic inventory network closest to the proven response."""
    resistors = enumerate_networks(items, "R", max_parts)
    capacitors = enumerate_networks(items, "C", 1)
    if not resistors or not any(math.isclose(cap.nominal, 100e-9, rel_tol=1e-9) for cap in capacitors):
        raise ValueError("inventory lacks resistors or the required 100 nF MFB capacitors")
    pools = (
        closest_networks(resistors, 255_000.0),
        closest_networks(resistors, 64_900.0),
        closest_networks(resistors, 510_000.0),
    )
    target = (9.79827727297, 1.56989199083, 1.0)
    candidates: list[MfbSynthesis] = []
    for r1 in pools[0]:
        for r2 in pools[1]:
            for r5 in pools[2]:
                c = 100e-9
                center = math.sqrt((r1.nominal + r2.nominal) / (c*c*r1.nominal*r2.nominal*r5.nominal)) / (2*math.pi)
                q = math.sqrt(c*c*r1.nominal*r2.nominal*r5.nominal*(r1.nominal+r2.nominal)) / (2*c*r1.nominal*r2.nominal)
                gain = r5.nominal / (2*r1.nominal)
                error = sum(math.log(actual / wanted) ** 2 for actual, wanted in zip((center, q, gain), target, strict=True))
                candidates.append(MfbSynthesis(r1, r2, r5, center, q, gain, error))
    return min(candidates, key=lambda item: (
        item.response_error, item.part_count,
        item.r1.canonical, item.r2.canonical, item.r5.canonical,
    ))
