"""Build a DesignGraph deterministically from netlist + BOM + extracted datasheets."""

from __future__ import annotations

import json
import re
from pathlib import Path
from logging import getLogger

from backend.pinscopex.utils import safe_mpn
from backend.pinscopex.models import (
    Component,
    ComponentConstraints,
    ComponentModel,
    ComponentSpecs,
    ComponentType,
    DesignGraph,
    Net,
    NetType,
    PinConnection,
    SimpleComponentSpecs,
)

# Datasheets are loaded here for pin-name enrichment during graph build,
# but NOT embedded into the graph. The validator loads them separately.
from backend.pinscopex.parsers import parse_bom, parse_netlist_any
from backend.pinscopex.resolve_passives import (
    SkippedItem,
    resolve_bom,
    resolved_to_specs,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = getLogger(__name__)

# ---------------------------------------------------------------------------
# Component type classification
# ---------------------------------------------------------------------------

_PREFIX_TYPE: dict[str, ComponentType] = {
    "R": ComponentType.RESISTOR,
    "C": ComponentType.CAPACITOR,
    "L": ComponentType.INDUCTOR,
    "U": ComponentType.IC,
    "IC": ComponentType.IC,
    "J": ComponentType.CONNECTOR,
    "X": ComponentType.CRYSTAL,
    "Y": ComponentType.CRYSTAL,
    "D": ComponentType.DISCRETE,
    "LED": ComponentType.DISCRETE,
    "Q": ComponentType.DISCRETE,
    "T": ComponentType.TRANSFORMER,
    "F": ComponentType.FUSE,
    "SW": ComponentType.SWITCH,
    "TP": ComponentType.TEST_POINT,
    "FM": ComponentType.FIDUCIAL,
    "MH": ComponentType.MECHANICAL,
}

# Fallback footprint patterns for designators whose prefix isn't a known
# EE convention (e.g. pure-numeric refs like "4", descriptive refs like
# "CV GND", "CAN BUS IN", "12V ACTIVE"). Order matters — first match wins.
_FOOTPRINT_TYPE_PATTERNS: list[tuple[re.Pattern, ComponentType]] = [
    (
        re.compile(
            r"(?i)(?:^|[\s_])("
            r"CONN(?:_|\b)|TERM(?:\b|_BLK)|HEADER|SOCKET|JACK|RECEPTACLE|PLUG|"
            r"SCREW\s*TERM|PINHEADER|BARREL|BANANA|XT30|XT60|XT90|USB|"
            r"WURTH\s*746\d|TE\s*282834|TE\s*2828\d|MOLEX|JST"
            r")"
        ),
        ComponentType.CONNECTOR,
    ),
    (re.compile(r"(?i)TestPoint|TEST[_\s]POINT|\bTP_"), ComponentType.TEST_POINT),
    (re.compile(r"(?i)^LED[\s_]|\bLED\s+\d{3,4}"), ComponentType.DISCRETE),
    (re.compile(r"(?i)^CAP[\s_]|\bCAP_|CAPACITOR"), ComponentType.CAPACITOR),
    (re.compile(r"(?i)^RES[\s_]|\bRES_|RESISTOR"), ComponentType.RESISTOR),
    (re.compile(r"(?i)^IND[\s_]|\bIND_|INDUCTOR"), ComponentType.INDUCTOR),
    (re.compile(r"(?i)DO214|DO220|SOD\d|SMD?J5|SMB_|SOT-?23"), ComponentType.DISCRETE),
]


def _classify_component(ref: str, footprint: str) -> ComponentType:
    """Classify a component by its reference prefix, with footprint fallback."""
    prefix = re.match(r"^[A-Za-z]+", ref)
    if prefix:
        t = _PREFIX_TYPE.get(prefix.group())
        if t is not None:
            return t
    # Fallback: use footprint hints when the ref prefix isn't recognised
    # (e.g. pure-numeric refs, or descriptive refs like "CV GND", "12V ACTIVE")
    fp = footprint or ""
    for pattern, ctype in _FOOTPRINT_TYPE_PATTERNS:
        if pattern.search(fp):
            return ctype
    return ComponentType.UNKNOWN


# ---------------------------------------------------------------------------
# Net type / voltage inference
# ---------------------------------------------------------------------------

# Patterns for common power rail names -> nominal voltage
_VOLTAGE_RE: list[tuple[re.Pattern, float]] = [
    (re.compile(r"^\+(\d+)V(\d+)$"), 0),  # +3V3 -> 3.3, +1V35 -> 1.35
    (re.compile(r"^\+(\d+(?:\.\d+)?)V$"), 0),  # +5V -> 5.0, +12V -> 12.0
]


def _parse_rail_voltage(name: str) -> float | None:
    """Try to extract a numeric voltage from a power-rail net name.

    Handles patterns like: +3V3, +5V, VDD_1V8, DVDD3V3, VBUS_5V0, etc.
    """
    # +3V3 style: digits + V + digits -> "3.3"
    m = re.match(r"^\+(\d+)V(\d+)$", name)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")

    # +5V style
    m = re.match(r"^\+(\d+(?:\.\d+)?)V$", name)
    if m:
        return float(m.group(1))

    # Embedded voltage: *_1V8, *_3V3, *1V35, *3V3, etc.
    m = re.search(r"(\d+)V(\d+)", name)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")

    # Embedded voltage: *_5V0, *_12V, *5V, etc.
    m = re.search(r"(\d+(?:\.\d+)?)V(?:\d|$|_)", name)
    if m:
        return float(m.group(1))

    return None


# Net name prefixes that indicate power rails (case-insensitive)
_POWER_PREFIXES = (
    "VCC",
    "VDD",
    "VBUS",
    "VBAT",
    "VSYS",
    "VSUP",
    "VPWR",
    "AVDD",
    "DVDD",
    "AVCC",
    "DVCC",
    "PVDD",
    "PVCC",
    "V_",
)

# Net name suffixes that indicate ground (case-insensitive)
_GROUND_SUFFIXES = ("_GND", "GND")
_GROUND_NAMES = {"GND", "AGND", "DGND", "PGND", "VSS", "AVSS", "DVSS", "PVSS"}


def _infer_net_properties(name: str) -> tuple[NetType, float | None]:
    """Deterministically classify a net by its name."""
    upper = name.upper()

    # Ground nets — exact names and suffixes
    if upper in _GROUND_NAMES or any(upper.endswith(s) for s in _GROUND_SUFFIXES):
        return NetType.GROUND, 0.0

    # Power rails: names starting with "+"
    if name.startswith("+"):
        voltage = _parse_rail_voltage(name)
        return NetType.POWER, voltage

    # Power rails: common prefixes (VDD, VCC, VBUS, etc.)
    if any(upper.startswith(p) for p in _POWER_PREFIXES):
        voltage = _parse_rail_voltage(name)
        return NetType.POWER, voltage

    # Everything else is a signal
    return NetType.SIGNAL, None


# ---------------------------------------------------------------------------
# Datasheet loading
# ---------------------------------------------------------------------------


def _load_datasheets(
    directory: str | Path,
) -> dict[str, tuple[Path, ComponentConstraints]]:
    """Load all extracted datasheet JSONs, keyed by MPN."""
    result: dict[str, tuple[Path, ComponentConstraints]] = {}
    dirpath = Path(directory)
    if not dirpath.is_dir():
        return result

    for json_file in dirpath.glob("*.json"):
        text = json_file.read_text().strip()
        if not text:
            logger.warning("Empty datasheet JSON: %s", json_file)
            continue
        raw = json.loads(text)
        constraints = ComponentConstraints.model_validate(raw)
        result[constraints.mpn] = (json_file, constraints)

    return result


def _match_datasheet(
    mpn: str | None,
    datasheets: dict[str, tuple[Path, ComponentConstraints]],
) -> tuple[Path | None, ComponentConstraints | None]:
    """Match a BOM MPN to an extracted datasheet. Tries exact then normalized."""
    if not mpn:
        return None, None

    # Exact match
    if mpn in datasheets:
        return datasheets[mpn]

    # Normalize: strip common suffixes, lowercase compare
    def _norm(s: str) -> str:
        return re.sub(r"[/_\-\s]", "", s).upper()

    mpn_norm = _norm(mpn)
    for ds_mpn, (path, constraints) in datasheets.items():
        if _norm(ds_mpn) == mpn_norm:
            return path, constraints

    return None, None


# ---------------------------------------------------------------------------
# Component model loading / saving (passive specs cache)
# ---------------------------------------------------------------------------


def _load_component_models(directory: str | Path) -> dict[str, ComponentSpecs]:
    """Load all component model JSONs, keyed by MPN."""
    result: dict[str, ComponentSpecs] = {}
    dirpath = Path(directory)
    if not dirpath.is_dir():
        return result
    for json_file in dirpath.glob("*.json"):
        raw = json.loads(json_file.read_text())
        model = ComponentModel.model_validate(raw)
        result[model.mpn] = model.specs
    return result


def _save_component_model(mpn: str, specs: ComponentSpecs, directory: Path) -> None:
    """Save a ComponentModel to the component-models directory."""
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = safe_mpn(mpn)
    model = ComponentModel(mpn=mpn, specs=specs)
    (directory / f"{safe_name}.json").write_text(model.model_dump_json(indent=2) + "\n")


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph(
    netlist_path: str | Path,
    bom_path: str | Path,
    datasheets_dir: str | Path = "datasheets/extracted",
    patterns_dir: str | Path = "component-patterns",
    component_models_dir: str | Path = "component-models",
    *,
    reference_col: str = "Reference",
    mpn_col: str = "Manufacturer Part Number",
    skipped: list[SkippedItem] | None = None,
    include_subdesigns: set[str] | None = None,
) -> DesignGraph:
    """Build a DesignGraph deterministically from project files.

    Steps:
        1. Parse netlist -> parts (ref, footprint) and nets (name, pin connections)
        2. Parse BOM -> values, MPNs, LCSC codes per reference
        3. Load extracted datasheets and match by MPN
        4. Resolve passive specs from patterns + cached component models
        5. Assemble components with classified type, linked constraints, and specs
        6. Assemble nets with inferred type/voltage and enriched pin names
    """
    # Parse BOM first so we can feed known refs into the netlist parser —
    # PADS-PCB netlists allow multi-word designators (e.g. "CV GND"), which
    # only tokenise correctly with the BOM's ref list as a lookup. EDIF
    # netlists ignore known_refs (designators are unambiguous tokens).
    bom = parse_bom(bom_path, reference_col=reference_col, mpn_col=mpn_col)
    parts, raw_nets, _ = parse_netlist_any(
        netlist_path,
        known_refs=set(bom.keys()),
        include_subdesigns=include_subdesigns,
    )
    datasheets = _load_datasheets(datasheets_dir)

    # --- Resolve passive specs ------------------------------------------------
    models_dir = Path(component_models_dir)
    mpn_specs: dict[str, ComponentSpecs] = _load_component_models(models_dir)
    mpn_subtype: dict[str, str] = {}  # MPN -> component_subtype from patterns

    for rp in resolve_bom(
        bom_path,
        patterns_dir,
        reference_col=reference_col,
        mpn_col=mpn_col,
        skipped=skipped,
    ):
        if rp.component_subtype:
            mpn_subtype[rp.mpn] = rp.component_subtype
        if rp.mpn not in mpn_specs:
            try:
                specs = resolved_to_specs(rp)
                mpn_specs[rp.mpn] = specs
                _save_component_model(rp.mpn, specs, models_dir)
            except Exception as e:
                if skipped is not None:
                    skipped.append(SkippedItem(rp.mpn, "passive_specs", str(e)))

    components: dict[str, Component] = {}
    nets: dict[str, Net] = {}

    # --- Build components ---------------------------------------------------
    # Some PADS-PCB netlist exports omit the *PART* section. When that happens
    # derive the component list from BOM entries + refs found in nets so the
    # graph is still fully populated.
    if not parts:
        net_refs = {ref for pins in raw_nets.values() for ref, _ in pins}
        all_refs = set(bom.keys()) | net_refs
        parts = {ref: bom.get(ref, {}).get("footprint", "") for ref in all_refs}

    for ref, footprint in parts.items():
        bom_entry = bom.get(ref, {})
        value = bom_entry.get("value", "")
        mpn = bom_entry.get("mpn")

        components[ref] = Component(
            reference=ref,
            value=value,
            footprint=footprint,
            component_type=_classify_component(ref, footprint),
            mpn=mpn,
            pins={},
        )

    # Build MPN -> constraints lookup for pin-name enrichment and subtype
    _constraints_by_ref: dict[str, ComponentConstraints] = {}
    for ref, comp in components.items():
        if comp.mpn:
            _, constraints = _match_datasheet(comp.mpn, datasheets)
            if constraints:
                _constraints_by_ref[ref] = constraints
                if constraints.component_subtype:
                    comp.component_subtype = constraints.component_subtype
            # Attach specs (passive or simple component) and subtype
            if comp.mpn in mpn_specs:
                comp.specs = mpn_specs[comp.mpn]
                # SimpleComponentSpecs carries its own subtype
                if not comp.component_subtype:
                    s = mpn_specs[comp.mpn]
                    if hasattr(s, "component_subtype") and s.component_subtype:
                        comp.component_subtype = s.component_subtype
            if not comp.component_subtype and comp.mpn in mpn_subtype:
                comp.component_subtype = mpn_subtype[comp.mpn]

    # --- Build nets and wire up pins ----------------------------------------

    for net_name, pin_list in raw_nets.items():
        net_type, voltage = _infer_net_properties(net_name)

        pin_connections: list[PinConnection] = []
        for ref, pin_num in pin_list:
            # Record on the component side: pin -> net
            if ref in components:
                components[ref].pins[pin_num] = net_name

            # Enrich pin name from datasheet (IC constraints or simple specs)
            pin_name = None
            constraints = _constraints_by_ref.get(ref)
            if constraints:
                pin_obj = constraints.pin_by_number(pin_num)
                if pin_obj:
                    pin_name = pin_obj.name
            elif ref in components and components[ref].mpn:
                # Check SimpleComponentSpecs pintable
                s = mpn_specs.get(components[ref].mpn)
                if isinstance(s, SimpleComponentSpecs) and s.pintable:
                    pin_obj = s.pin_by_number(pin_num)
                    if pin_obj:
                        pin_name = pin_obj.name

            pin_connections.append(
                PinConnection(
                    component_ref=ref,
                    pin_number=pin_num,
                    pin_name=pin_name,
                )
            )

        nets[net_name] = Net(
            name=net_name,
            net_type=net_type,
            voltage=voltage,
            pins=pin_connections,
        )

    return DesignGraph(components=components, nets=nets)
