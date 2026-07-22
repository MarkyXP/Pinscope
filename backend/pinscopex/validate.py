"""Direct datasheet review — validates IC usage by comparing the actual
circuit to the component's datasheet.

No intermediate rule extraction.  Claude reads the datasheet PDF and the
component's circuit neighborhood together and flags issues directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

from backend.pinscopex.models import (
    ComponentConstraints,
    ComponentType,
    DesignGraph,
    Finding,
    NetType,
    ValidationReport,
)
from backend.pinscopex.pin_function_tokens import parse_net_token
from backend.pinscopex.validation_tools import (
    ALL_TOOLS,
    SUBMIT_REVIEW_SCHEMA,
    ConstraintsMap,
    execute_tool,
    _format_specs,
    _is_thermal_pad_pin,
    _pin_sort_key,
    _reviewer_voltage_str,
)
from backend.services.llm.factory import get_provider
from backend.services.llm.types import (
    Completion,
    Message,
    PdfBlock,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSchema,
)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an electrical engineer reviewing how a component is used in a \
hardware design. You have the component's datasheet and a description of \
how it's wired in the actual circuit.

### Review approach
Treat this IC as a COVERAGE CHECKLIST, not a single investigation. Before \
hunting for problems, enumerate every focus area this IC has — derive them \
from its pins, nets, neighbors, and subtype. A typical checklist:
- Power & decoupling on each supply pin.
- Each signal interface to each connected component — voltage \
compatibility, direction, and correct cross-connection (e.g. TX↔RX).
- Absolute-maximum ratings on each pin vs. the actual rail driving it.
- Reset / enable / boot / mode-strap / configuration pins.
- Clock or crystal circuit, if present.
- Required external components named by the datasheet.
- Unused / no-connect pins.

Then work the areas one at a time. For EACH area, don't just confirm a \
part is present — ask what specific failure mode would make it wrong \
(missing part, wrong value, over-voltage, swapped pair, wrong topology) \
and check the datasheet and the actual netlist topology against that \
failure mode.

Every area must end up accounted for: either as a finding, or listed in \
`checked_areas` as reviewed-and-correct. After you resolve one area, move \
on to the NEXT area — do NOT stop and submit just because you found or \
cleared the first issue. You have a generous turn budget; the goal is to \
cover the whole IC, not to finish fast.

### Reference designators — datasheet vs. schematic
The datasheet's reference/application circuit uses its OWN example \
designators (e.g. "R2", "C1", "L1"). These are NOT the designators in \
this project's schematic. The project's real designators are the ones \
shown in the component context (e.g. "U1", "R5", "C12").

Before citing any passive or discrete in a finding, resolve its role to \
the actual schematic designator:
1. Identify the component's *role* from the datasheet (e.g. "the resistor \
between the VIN pin and the SW pin", "the feedback divider top resistor", \
"the bootstrap capacitor between SW and BOOT").
2. Use the component context — or `find_connected_components` / \
`get_net_for_pin` — to find which schematic designator plays that role \
in this design.
3. Cite ONLY the schematic designator (and its value/MPN) in your \
finding. Never cite the datasheet's example designator.

If no schematic component plays that role, say so explicitly ("no \
component is connected between pin 3 (VIN) and pin 5 (SW)") rather than \
naming a datasheet-example part. If you cannot resolve the role to a \
schematic designator with confidence, demote the finding to WARNING or \
INFO and describe the role instead of naming a part.

### What to report
Only report issues.  Do not report things that are correct.

If your investigation concludes the design is correct — even when the \
surface reading suggested otherwise (e.g., "C1 (100 nF) is below the \
1 µF minimum, but C24 (1 µF) in parallel satisfies the spec", or "no \
dedicated input cap is shown, but C3 is on the VIN net and satisfies \
the requirement") — do NOT submit it as a finding. Add the topic to \
`checked_areas` instead. A finding whose own `why` field confirms the \
requirement is met dilutes the signal of real issues. If you write \
"satisfies", "meets the requirement", "is in the correct place", or \
"no issue" in your reasoning, the result belongs in `checked_areas`, \
not `findings`.

For each issue:
- **finding**: A concise one-line title of the issue (the rule title). \
Keep it to a single line — cite the key component refs, values, net names, \
or pin numbers, but do not elaborate. No multi-sentence descriptions here.
- **why**: The explanation — what the datasheet says and what could go \
wrong. Keep this to **2 lines at most** (roughly 2 short sentences). This \
is the most important field — explain the engineering consequence, not \
just the rule, but stay terse.
- **status**: ERROR (will cause malfunction or violate abs max), \
WARNING (may degrade reliability or is conditionally wrong), \
INFO (worth noting but unlikely to cause problems).
- **source_page**: The datasheet page where the requirement is stated.
- **source_quote**: The exact verbatim sentence or clause from the datasheet \
that states the requirement. Copy it precisely, character-for-character (a \
short span, ~200 chars max) so it can be located and highlighted in the PDF. \
Omit this field when the requirement is shown only in a figure or a \
rasterized table with no selectable text — do not paraphrase or invent a quote.
- **source_designator**: Leave unset when `source_page`/`source_quote` come \
from THIS component's datasheet (the default). Set it to a connected \
component's designator (e.g. `U3`) only when the page/quote come from that \
neighbor's datasheet that you fetched via `get_datasheet_excerpt` — this \
links the page number to the right datasheet.
- **recommendation**: What to change (for ERROR/WARNING only).

### Calibration
ERROR only for clear violations: required pin floating, voltage exceeding \
absolute max, required external component completely missing, wrong \
connection topology.

WARNING when: component value differs from recommended but might be \
adequate, rule is conditional on firmware/mode, concern is real but not \
certain to cause failure.

INFO when: design uses a valid but non-standard approach, optional feature \
is unused, or a layout-level concern exists that cannot be verified from \
the netlist.

### ERROR requires a concrete harm pathway
Every ERROR that alleges damage, abs-max violation, or out-of-spec \
stress must state the harm pathway with concrete numbers, not \
speculation. Before submitting an ERROR, the `why` field must answer:
1. **Which pin or component takes the stress** (this IC's pin, an \
internal node named by the datasheet, or an external part).
2. **What the actual voltage / current / temperature on it is**, derived \
from the topology (the rail it ties to, the divider ratio, the regulator \
output, the bias current). Numbers, not net labels.
3. **What the datasheet's limit is**, quoted from an abs-max table, \
recommended-operating range, or pin description.
4. **Why (1) exceeds (3)** — the inequality, in numbers.

If you cannot produce all four, downgrade to WARNING and write the \
`why` as `Unverified: <which of (1)-(4) you could not establish>`. \
Hedged language alone — "may damage", "could degrade", "might cause" — \
is not enough for ERROR; replace it with the inequality or demote the \
finding. This applies especially when the alleged damage is to an \
*internal* component (internal DC-block cap, ESD diode, on-die clamp): \
those are designed against the same package abs-max ratings as the \
external pin, so an external stress within the pin's abs-max does not \
damage the part inside.

Two additional constraints on the inequality:
- **Pin-matched limit.** The abs-max number in (3) must be from the \
abs-max row for *the same pin or signal* that takes the stress in \
(1). Vdd's abs-max does not apply to an RF, signal, or I/O pin — \
those pins have their own abs-max rows (commonly `V_RFIN`, `V_pin`, \
`V_in` ranges, or are governed by the recommended-operating range). \
If the datasheet does not list an abs-max for the specific pin under \
stress, write `Unverified: no abs-max listed for pin <n>` and demote \
to WARNING — do not borrow a different pin's number.
- **Strict inequality.** Abs-max is the don't-exceed line. The \
inequality in (4) must be strict (`actual > limit`). "Equal to \
abs-max" is not a violation — it may stress lifetime but does not \
qualify as damage. If the math comes out to `=` rather than `>`, the \
finding is at most a WARNING.

### Decoupling capacitors
Larger caps satisfy smaller specs: 470nF satisfies "0.1uF", 10uF satisfies \
"1uF minimum".  Only flag if actual value is below the minimum specified.

### Netlist limitations
You are reviewing a NETLIST, not PCB layout.  You cannot verify component \
proximity, trace routing, or thermal management.  If a functional \
requirement is met at the netlist level, do not flag it as an issue.

### Identify the role of each external part before judging it
For each external part on this IC's pins (R, C, L, FB, diodes, \
transistors), derive its role in the design from first principles \
before concluding whether the connection is correct. The role is the \
answer to "what does this part do in this circuit?" — not the answer \
to "does this pattern have a name I recognize?". Reason from:
1. **What the pin does** (from the datasheet pin description in your \
context — e.g. "DC blocked", "AC coupled", "internally biased", \
"open-drain", "high impedance", "reference output").
2. **What the part is** (its value class and approximate value — an \
inductor at RF frequencies is a choke; a small cap to GND is shunt \
decoupling; a series cap is AC coupling or DC blocking; a divider \
sets a sense ratio).
3. **Where the other end of the part goes** — trace it with \
`find_connected_components` / `get_net_for_pin` / the bridges list. \
A part terminated on a power rail does something different from one \
terminated at a connector or another IC pin.
4. **What (1) + (2) + (3) imply about the part's purpose.**

When a documented characteristic of the pin would *prevent* the \
surface-reading interaction (e.g. a DC rail tied through an inductor \
to a pin that is documented as DC-blocked), the part is almost always \
serving the rest of the circuit, not the chip — its role is found by \
asking what the remaining circuit needs the part for, including \
loads reached through a connector or coax further down the net. Do \
not raise a finding against the chip for a part that does not stress \
the chip.

If you cannot articulate the role after a brief look, submit the \
concern as `status="WARNING"` with `why` starting `Unverified: role \
of <part> on pin <n> not determined` — never ERROR on a component \
whose purpose you have not identified.

### Budget per concern: cap ONE concern, not the whole review
A single concern (one potential finding under investigation) gets at \
most two follow-up tool calls beyond what was already in your initial \
context. If the concern is not resolved within that budget, submit it \
as WARNING with `why` starting `Unverified: <what you could not \
establish in two queries>` and move on to the next area. This per-concern \
cap exists so one concern cannot swallow the whole review — NOT so you \
finish early. Your total budget across all concerns is generous: spend it \
on breadth. The failure mode to avoid is leaving focus areas of this IC \
uninvestigated, not spending too many turns. Do not call submit_review \
while any enumerated focus area is still uninvestigated.

### Net names are not voltage labels
Net names are user-chosen labels — they describe a signal's *role*, not its \
actual voltage. A net named `VBAT_SENSE`, `8S_LiPo`, or `HV_FB` may carry \
only a low-voltage MCU control line, a divided-down sense voltage, or be \
misnamed entirely.

Before flagging any absolute-max violation, supply-mismatch, or "pin driven \
beyond rated input" issue, use `find_connected_components` to identify the \
*actual* driver of the net (power rail, regulator output, MCU pin, voltage \
divider, connector, etc.). Only flag when the topology confirms the \
voltage. If the driver is ambiguous, demote to WARNING and describe what \
would need to be verified.

### Voltage tags in tool output: trusted but sparse
The `(power, X.X V)` annotation in net-info lines and `[power, X.X V]` tag \
on pin lines only appear when the voltage is sourced from the netlist \
itself — either the net name encodes it (`+5V`, `+3V3`, `1V5`) or the \
user declared it via a power-source hint. Power-tree-derived voltages \
(deterministic propagation through passthroughs, regulator-output back- \
annotation, model inferences) are deliberately suppressed from your tool \
output — they are too lossy to trust at review time, and trusting them \
has produced false-positive findings in the past.

When a pin's net has no voltage tag, the netlist does not establish what \
voltage flows there. Trace topology (find_connected_components, walking \
back through passthroughs and regulators) to discover the source, or \
treat the rail as unknown.

### Rail voltages and VREF: do not guess
When you cannot establish an IC's supply or signal voltage from any of:

- the net name (e.g., `+5V`, `+3V3`, `GND`),
- a `(power, X.X V)` tag in tool output,
- a connected source / regulator output whose voltage IS visible by the \
rules above (reached by walking topology through find_connected_components),

you must NOT reconstruct it by assuming a VREF on an upstream regulator's \
feedback divider. VREF varies by part (1.20V LDO, 1.25V LDO, 0.6V buck, \
0.8V buck, 0.925V buck-boost, 1.205V LDO, ...). A guessed VREF cascades \
into a wrong rail voltage and false-positive out-of-spec findings — this \
has happened (assumed VREF=0.5V → Vdd=1.48V → bogus 'below operating \
range' WARNING).

If a finding hinges on knowing the rail voltage and you cannot establish \
it from the rules above, downgrade to WARNING with `why` starting \
`Unverified: rail voltage at <net> could not be established without \
guessing a regulator's VREF`. Do not raise ERROR on guessed rails.

### Cross-IC interface checks and uncertainty
When a finding hinges on a *connected* IC's spec (5V-tolerance, abs-max, \
VIH/VIL, drive strength), that spec lives in the neighbor's datasheet, \
not yours. Before raising ERROR on such a finding, call \
`get_datasheet_excerpt(designator, topic)` on the neighbor (e.g. \
`topic="pin_voltage_levels"` for 5V-tolerance, `"absolute_max"` for \
stress ratings) and read the returned pages. When a finding then cites a \
page or quote you read from that neighbor's excerpt, set the finding's \
`source_designator` to the neighbor's designator and put the neighbor's \
page number in `source_page` — the citation must point at the datasheet the \
evidence actually lives in, not yours.

If the excerpt does not resolve the spec, call `submit_review` with \
`status="WARNING"` (not ERROR) for that finding, and start its `why` \
field with `Unverified: <the assumption you had to make>`. Reserve ERROR \
for cases where the violation is established from both sides of the \
interface — a false ERROR is the single biggest trust-killer for this \
review.

### Alternate-function feasibility vs. direction
Pins on peripheral-named nets show their datasheet alternate-function list \
inline as `[alt: ...]` (and `get_pintable` shows it for any pin on demand). \
That list is datasheet-extracted ground truth for what the pin can be muxed \
to. Use it for a FEASIBILITY check — never a direction check:

- FEASIBILITY (hard ERROR): if a net name asserts a peripheral function — \
e.g. a net `...UART5-TX...` on a pin whose `[alt: ...]` exposes UART5 only \
as `UART5_RX` — the silicon cannot route that function to that pin. It is \
physically unrealizable regardless of anything downstream. Raise ERROR and \
name the functions the pin actually exposes for that peripheral.
- DIRECTION (context-dependent — do NOT auto-flag): a TX wired to the other \
device's RX is normal. A direct UART link crosses TX→RX; a transceiver, \
isolator, or level-shifter is often straight-through (MCU TX → transceiver \
TXD/DI). Whether a TX/RX (or SDA/SCL) connection is correct depends on the \
role of the part on the other end, which you must reason about from the \
circuit — never flag a TX-on-an-RX-named-net (or vice versa) on naming \
alone. Only raise a direction ERROR when topology forces it (e.g. two \
push-pull outputs on one net). Otherwise WARNING/INFO, stating the \
downstream role you'd need to confirm.

### Pin labels in your context can be wrong
The `Pin N (NAME)` labels in the component context come from a separate \
datasheet-extraction pass. For image-only PDFs, small or dense pin \
tables, and non-standard parts, that pass can mis-label individual pins \
(D+/D− swaps, TX/RX, CC1/CC2, IN+/IN−, anode/cathode, A/K, +/−). Before \
raising an ERROR whose logic turns on the polarity or identity of a \
specific pin pair on THIS IC (differential-pair swap, supply polarity, \
input/output orientation), re-read the pin-mapping page of the datasheet \
PDF already in your initial context and verify each pin label against \
it. If the datasheet contradicts the in-context label, trust the \
datasheet — demote the finding to WARNING and state explicitly which \
pin label in the context appears mis-extracted (e.g. \
`Pin A6 labeled "D−" in context, datasheet shows "D+"`).  The `[alt: ...]` \
alternate-function list shown for peripheral-named-net pins is taken \
verbatim from the datasheet pin table and is reliable even when the short \
`(NAME)` label is not — prefer it when judging what a pin can be muxed to.

### Direction-control and transceiver function tables
Bidirectional transceivers, level shifters, mux/demux, bus switches, and \
analog switches (74xx245, 74xx125, 74xx157, TS3A-family, etc.) often \
print their function/truth table in a column-segmented layout where two \
adjacent cells read as a single English phrase ("input B = A", \
"high-Z input"). Scanning left-to-right inverts the meaning and \
invalidates every downstream finding. Before raising any ERROR \
involving bus contention, "two outputs on one net", or direction-control \
polarity, re-read the function table from THIS IC's datasheet PDF and \
quote each cell of the relevant row separately. State the direction \
explicitly ("DIR=H → A is input, B is output, A→B") before claiming \
output contention.

### One root cause = one finding
If two ERRORs you're about to submit collapse to the same underlying \
mistake — e.g. a single mis-configured DIR pin produces both "bus \
contention on TXD" AND "device is unidirectional only" — submit ONE \
combined finding that names the root cause. Restate the downstream \
consequences inside the `why` field instead of as separate findings. \
Two ERRORs that share a premise read as independent problems, double \
the review's apparent severity, and dilute trust if the shared premise \
turns out to be wrong.

### Bridges between IC pins
The component context includes a `Bridges between <ref>'s pins:` section \
listing 2-or-more-terminal components that connect two of this IC's nets \
(decoupling caps, feedback dividers, sense resistors, snubbers, etc.). This \
is the most direct view of external passives associated with the IC. \
Before claiming a required external part is missing, scan this section — \
the part may be there under a different role label. Required passives \
must always be cited from the bridges list (or via a graph tool query) — \
never inferred from a single-pin listing alone.

### Exposed pad / thermal pad (EP, DAP, ePAD)
The datasheet pintable's EP/DAP pin number often does not match the number \
the schematic symbol uses.  Schematic symbols commonly assign the exposed \
pad a custom number (frequently pin_count+1, or a unique name).  \
Unmatched schematic pins that aren't in the datasheet pintable are listed \
as "Additional schematic pins (not in datasheet pintable)" at the end of \
the component context — these are almost always the EP/thermal pad.  \
Before flagging an EP-unconnected error, check that no additional \
schematic pin is tied to GND.  If any additional pin is on a GND net, \
treat the EP requirement as satisfied and do not flag it.

### Output — submit_review is the ONLY way findings reach the report
You MUST call the `submit_review` tool to record findings. Writing \
findings as a JSON block in your text response does NOT save them — they \
will be dropped. When you are ready to record findings (even just one), \
call `submit_review` with the findings array and checked_areas list. If \
the circuit matches the datasheet with no issues, still call \
`submit_review` with an empty findings array.

In **checked_areas**, list what you reviewed and confirmed correct — \
short labels like "input decoupling", "output capacitor", "enable logic", \
"voltage margins".  This tells the engineer what was verified, not just \
what failed.

Before calling submit_review, confirm every focus area you enumerated at \
the start is accounted for — present in either `findings` or \
`checked_areas`. If any enumerated area is still uninvestigated, \
investigate it before submitting.

Use the graph query tools to investigate connections beyond the provided \
context if needed.
"""


# Maximum turns for the review agentic loop
_MAX_REVIEW_TURNS = 10


# ---------------------------------------------------------------------------
# Component context builder
# ---------------------------------------------------------------------------

_GROUND_NET_MAX_COMPONENTS = 5  # Summarize ground nets with more than this


def build_component_context(
    graph: DesignGraph,
    constraints_map: ConstraintsMap,
    ref: str,
) -> str:
    """Build a text summary of an IC's full circuit neighborhood.

    Shows every pin, its net, and every component connected to that net
    (with values and specs).  Ground/power nets with many connections are
    summarized to avoid noise.
    """
    comp = graph.components.get(ref)
    if not comp:
        return f"Component '{ref}' not found in design graph."

    constraints = constraints_map.get(comp.mpn or "")
    lines: list[str] = []

    # Header
    lines.append(f"Component: {ref} ({comp.mpn or comp.value})")
    if comp.component_subtype:
        lines.append(f"Type: {comp.component_subtype}")
    if constraints and constraints.package_info:
        pi = constraints.package_info
        lines.append(f"Package: {pi.package}, {pi.pin_count} pins")
    lines.append("")

    # Build pin list — prefer extracted pintable order, fall back to netlist.
    # (pin_num, pin_name, net_name, note, functions)
    pin_entries: list[
        tuple[str, str | None, str | None, str | None, list[str] | None]
    ] = []
    matched_schematic_pins: set[str] = set()
    unmatched_ep_entries: list = []  # pintable EP rows whose number isn't in schematic

    if constraints and constraints.pintable:
        for p in sorted(
            constraints.pintable, key=lambda x: _pin_sort_key(str(x.number))
        ):
            net_name = comp.pins.get(str(p.number))
            if net_name is not None:
                matched_schematic_pins.add(str(p.number))
                pin_entries.append((str(p.number), p.name, net_name, None, p.functions))
            elif _is_thermal_pad_pin(p):
                unmatched_ep_entries.append(p)
            else:
                pin_entries.append((str(p.number), p.name, None, None, p.functions))
    else:
        for pn in sorted(comp.pins.keys(), key=_pin_sort_key):
            matched_schematic_pins.add(pn)
            pin_entries.append((pn, None, comp.pins[pn], None, None))

    # Orphan schematic pins: present in netlist but not matched to any
    # pintable entry. Commonly this is the EP/thermal pad under a user-
    # chosen pin number (e.g. pin_count+1).
    orphan_pins = [pn for pn in comp.pins if pn not in matched_schematic_pins]
    orphan_pins.sort(key=_pin_sort_key)

    # If there's exactly one unmatched EP pintable row and one orphan
    # schematic pin, map them together in the main list rather than
    # listing both separately.
    fused_ep_note = (
        "exposed pad / thermal pad — datasheet pintable lists this "
        "without a usable pin number; matched to orphan schematic pin"
    )
    if len(unmatched_ep_entries) == 1 and len(orphan_pins) == 1:
        ep_row = unmatched_ep_entries[0]
        orphan_pin = orphan_pins[0]
        pin_entries.append(
            (
                orphan_pin,
                ep_row.name,
                comp.pins[orphan_pin],
                fused_ep_note,
                ep_row.functions,
            )
        )
        unmatched_ep_entries = []
        orphan_pins = []

    # Track nets already shown to avoid repetition
    seen_nets: set[str] = set()

    for pin_num, pin_name, net_name, note, functions in pin_entries:
        name_str = f" ({pin_name})" if pin_name else ""
        note_str = f"  [{note}]" if note else ""
        # Render the datasheet alternate-function list inline only for pins whose
        # net name asserts a peripheral role (UART5_TX, I2C1_SDA, ...), so the
        # reviewer can check the asserted function against what the pin actually
        # supports — without bloating the context for every GPIO.
        alt_str = ""
        if functions and net_name and parse_net_token(net_name):
            alt_str = f"  [alt: {', '.join(functions)}]"

        if not net_name:
            lines.append(f"Pin {pin_num}{name_str} → [unconnected]{note_str}")
            lines.append("")
            continue

        net = graph.nets.get(net_name)
        if not net:
            lines.append(f"Pin {pin_num}{name_str} → {net_name}{alt_str}")
            lines.append("")
            continue

        voltage_str = _reviewer_voltage_str(net)
        lines.append(
            f"Pin {pin_num}{name_str} → {net_name} "
            f"[{net.net_type.value}{voltage_str}]{alt_str}{note_str}"
        )

        # If we already showed this net's components, just note it
        if net_name in seen_nets:
            lines.append(f"  (same net as above)")
            lines.append("")
            continue
        seen_nets.add(net_name)

        # Collect neighbors on this net (excluding self)
        neighbors = [
            pc
            for pc in net.pins
            if pc.component_ref != ref and pc.component_ref in graph.components
        ]

        # For large ground/power nets, summarize
        if len(neighbors) > _GROUND_NET_MAX_COMPONENTS and net.net_type in (
            NetType.GROUND,
            NetType.POWER,
        ):
            # Group by type
            by_type: dict[str, list[str]] = {}
            for pc in neighbors:
                nb = graph.components[pc.component_ref]
                ctype = nb.component_type.value
                by_type.setdefault(ctype, []).append(pc.component_ref)
            parts = [
                f"{len(refs)} {ctype}{'s' if len(refs) > 1 else ''}"
                for ctype, refs in sorted(by_type.items())
            ]
            lines.append(
                f"  {len(neighbors)} components on this net: {', '.join(parts)}"
            )
            # Still list ICs specifically since they're important
            for pc in neighbors:
                nb = graph.components[pc.component_ref]
                if nb.component_type == ComponentType.IC:
                    pin_name_str = ""
                    nb_constraints = constraints_map.get(nb.mpn or "")
                    if nb_constraints:
                        p = nb_constraints.pin_by_number(pc.pin_number)
                        if p:
                            pin_name_str = f" ({p.name})"
                    lines.append(
                        f"  {nb.reference}: {nb.mpn or nb.value} [pin {pc.pin_number}{pin_name_str}]"
                    )
        else:
            for pc in neighbors:
                nb = graph.components[pc.component_ref]
                mpn_str = f", {nb.mpn}" if nb.mpn else ""
                specs_str = _format_specs(nb.specs)
                if specs_str:
                    specs_str = f" ({specs_str})"

                # Pin name on the neighbor
                pin_name_str = ""
                nb_constraints = constraints_map.get(nb.mpn or "")
                if nb_constraints:
                    p = nb_constraints.pin_by_number(pc.pin_number)
                    if p:
                        pin_name_str = f" ({p.name})"

                lines.append(
                    f"  {nb.reference}: {nb.value}{mpn_str}{specs_str}"
                    f" [pin {pc.pin_number}{pin_name_str}]"
                )

        lines.append("")

    # Bridges: components whose pins land on two or more of this IC's nets.
    # Captures Rsense / feedback dividers / decoupling caps / snubbers /
    # protection resistors that span two IC pins — easy to miss when each
    # endpoint is on a different net section, especially when one endpoint
    # is on a power/ground net that gets summarized.
    pin_name_by_num = {pn: pname for pn, pname, _, _, _ in pin_entries if pname}
    ic_net_to_pins: dict[str, list[str]] = {}
    for ic_pin, ic_net in comp.pins.items():
        if ic_net:
            ic_net_to_pins.setdefault(ic_net, []).append(ic_pin)
    ic_nets_set = set(ic_net_to_pins.keys())

    def _label_endpoint(net: str) -> str:
        pins = sorted(ic_net_to_pins.get(net, []), key=_pin_sort_key)
        prefix = "pins" if len(pins) > 1 else "pin"
        names = [pin_name_by_num.get(p) for p in pins]
        named = [n for n in names if n]
        if named:
            unique = list(dict.fromkeys(named))  # preserve order, dedupe
            name_str = "/".join(unique)
            return f"{prefix} {'/'.join(pins)} ({name_str}, {net})"
        return f"{prefix} {'/'.join(pins)} ({net})"

    # A bridge is "interesting" only if at least one endpoint is a signal
    # net. Pure VCC↔GND bridges (bypass caps, every IC sharing the rail)
    # would otherwise drown out the signal-bearing topology like Rsense or
    # MCU pulldowns. Decoupling-cap counts are already visible in the
    # per-pin listing's "N capacitors on this net" summary.
    def _is_signal_net(name: str) -> bool:
        n = graph.nets.get(name)
        return bool(n and n.net_type == NetType.SIGNAL)

    bridge_lines: list[str] = []
    skipped_power_only = 0
    for nb_ref, nb in graph.components.items():
        if nb_ref == ref:
            continue
        nets_touched = {n for n in nb.pins.values() if n in ic_nets_set}
        if len(nets_touched) < 2:
            continue
        if not any(_is_signal_net(n) for n in nets_touched):
            skipped_power_only += 1
            continue
        nets_sorted = sorted(nets_touched)
        endpoints = " ↔ ".join(_label_endpoint(n) for n in nets_sorted)
        mpn_str = f", {nb.mpn}" if nb.mpn else ""
        specs_str = _format_specs(nb.specs)
        if specs_str:
            specs_str = f" ({specs_str})"
        value_str = nb.value if nb.value else nb.component_type.value
        bridge_lines.append(
            f"  {nb.reference}: {value_str}{mpn_str}{specs_str} — bridges {endpoints}"
        )

    if bridge_lines or skipped_power_only:
        lines.append(f"Bridges between {ref}'s pins (signal-bearing only):")
        if bridge_lines:
            bridge_lines.sort()
            lines.extend(bridge_lines)
        if skipped_power_only:
            lines.append(
                f"  ({skipped_power_only} additional bypass/rail-sharing "
                f"bridges between power & ground nets, omitted — see per-pin listing for counts)"
            )
        lines.append("")

    # Orphan schematic pins — not matched to any pintable entry. These are
    # frequently the EP/thermal pad (schematic symbols commonly assign a
    # custom pin number to the exposed pad).
    if orphan_pins or unmatched_ep_entries:
        lines.append("Additional schematic pins (not in datasheet pintable):")
        if unmatched_ep_entries:
            ep_names = ", ".join(
                f"{p.name} (pintable #{p.number})" for p in unmatched_ep_entries
            )
            lines.append(
                f"  (datasheet pintable lists these without a schematic-matched "
                f"pin number — likely the exposed pad: {ep_names})"
            )
        for pn in orphan_pins:
            net_name = comp.pins.get(pn)
            if not net_name:
                continue
            net = graph.nets.get(net_name)
            if net is None:
                lines.append(f"  Pin {pn} → {net_name}")
                continue
            voltage_str = _reviewer_voltage_str(net)
            lines.append(f"  Pin {pn} → {net_name} [{net.net_type.value}{voltage_str}]")
        if not orphan_pins and unmatched_ep_entries:
            lines.append(
                "  (no matching orphan schematic pin found — the EP may be "
                "genuinely unconnected in the schematic)"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-IC review
# ---------------------------------------------------------------------------


class ReviewResult:
    """Findings + coverage from a single IC review."""

    __slots__ = ("findings", "checked_areas")

    def __init__(self, findings: list[Finding], checked_areas: list[str]):
        self.findings = findings
        self.checked_areas = checked_areas


def _dict_to_tool_schema(tool_dict: dict) -> ToolSchema:
    """Convert an Anthropic-style tool dict to a ToolSchema dataclass."""
    return ToolSchema(
        name=tool_dict["name"],
        description=tool_dict.get("description", ""),
        input_schema=tool_dict.get("input_schema", {}),
    )


async def review_component_async(
    graph: DesignGraph,
    constraints_map: ConstraintsMap,
    ic_ref: str,
    pdf_path: str,
    model: str = "anthropic/claude-sonnet-4-6",
) -> ReviewResult:
    """Review an IC's usage against its datasheet.  Returns findings + coverage.

    Async version using the unified LiteLLM provider.
    """
    comp = graph.components[ic_ref]
    mpn = comp.mpn or comp.value
    context = build_component_context(graph, constraints_map, ic_ref)

    # Build user message with PDF + text
    user_blocks: list = [PdfBlock(path=Path(pdf_path))]
    user_blocks.append(TextBlock(text=f"Review this component's usage:\n\n{context}"))
    user_msg = Message(role="user", content=user_blocks)

    messages: list[Message] = [user_msg]

    provider = get_provider("validation")
    session = await provider.create_session(
        model=model,
        system=SYSTEM_PROMPT,
        max_tokens=4096,
        temperature=0.0,
    )

    try:
        for turn in range(_MAX_REVIEW_TURNS):
            is_last_turn = turn == _MAX_REVIEW_TURNS - 1

            # On the last turn, force submit_review
            if is_last_turn:
                tools = [_dict_to_tool_schema(SUBMIT_REVIEW_SCHEMA)]
                tool_choice = {"name": "submit_review"}
            else:
                tools = [_dict_to_tool_schema(t) for t in ALL_TOOLS]
                tool_choice = "auto"

            completion: Completion = await session.complete(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
            )

            # Check for submit_review
            for tc in completion.tool_calls:
                if tc.name == "submit_review":
                    return _parse_review(tc.input, ic_ref, mpn)

            # Process graph tool calls
            tool_result_blocks: list[ToolResultBlock] = []
            for tc in completion.tool_calls:
                if tc.name != "submit_review":
                    result_text = execute_tool(
                        graph, constraints_map, tc.name, tc.input
                    )
                    tool_result_blocks.append(
                        ToolResultBlock(
                            tool_use_id=tc.id,
                            name=tc.name,
                            content=result_text,
                        )
                    )

            if not tool_result_blocks:
                # Model responded with text only — no tools called, no submission
                break

            # Append assistant response + tool results to conversation
            messages.append(
                Message(role="assistant", content=completion.raw_assistant_blocks)
            )
            messages.append(Message(role="user", content=tool_result_blocks))

    finally:
        await session.close()

    return ReviewResult([], [])  # No findings submitted


def review_component(
    graph: DesignGraph,
    constraints_map: ConstraintsMap,
    ic_ref: str,
    pdf_path: str,
    model: str = "anthropic/claude-sonnet-4-6",
) -> ReviewResult:
    """Sync wrapper around review_component_async for CLI use."""
    return asyncio.run(
        review_component_async(graph, constraints_map, ic_ref, pdf_path, model)
    )


def _coerce_str_list(value) -> list[str]:
    """Coerce a tool-input value into a list of non-empty strings.

    Claude occasionally violates the tool schema (e.g. returns a stringified
    list instead of a real array). Sanitize here so downstream Pydantic
    validation of ValidationReport cannot fail on a single IC's output.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if x is not None and str(x).strip()]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [
                    str(x).strip() for x in parsed if x is not None and str(x).strip()
                ]
        except (json.JSONDecodeError, ValueError):
            pass
        return [s]
    return [str(value).strip()]


def _parse_review(
    tool_input: dict,
    ic_ref: str,
    mpn: str,
    *,
    mpn_by_designator: dict[str, str] | None = None,
    connected: set[str] | None = None,
) -> ReviewResult:
    """Parse submit_review tool output into findings + coverage.

    A finding whose evidence came from a *connected* neighbor's datasheet
    excerpt carries that neighbor's designator in ``source_designator`` — its
    ``source_page`` is a page in the neighbor's PDF, not the IC under review.
    ``mpn_by_designator`` resolves that designator to the MPN so ``reference``
    (and the frontend viewer) point at the correct datasheet; ``connected``
    restricts which neighbor designators are honored. When the map/neighbor is
    absent or unresolvable, the citation falls back to this IC's own datasheet
    so the cited page and the datasheet the viewer opens never disagree.
    """
    mpn_by_designator = mpn_by_designator or {}
    findings: list[Finding] = []
    raw_findings = tool_input.get("findings") or []
    if not isinstance(raw_findings, list):
        raw_findings = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        try:
            page = item.get("source_page")
            raw_src = str(item.get("source_designator") or "").strip()
            if (
                raw_src
                and raw_src != ic_ref
                and raw_src in mpn_by_designator
                and (connected is None or raw_src in connected)
            ):
                src_designator: str | None = raw_src
                src_mpn = mpn_by_designator[raw_src]
            else:
                src_designator = None
                src_mpn = mpn
            findings.append(
                Finding(
                    designator=ic_ref,
                    mpn=mpn,
                    source_designator=src_designator,
                    finding=item["finding"],
                    why=item.get("why", ""),
                    status=item["status"],
                    source_page=page,
                    source_quote=item.get("source_quote", ""),
                    recommendation=item.get("recommendation", ""),
                    reference=f"{src_mpn} datasheet p.{page if page is not None else '?'}",
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            print(f"Skipping malformed finding for {ic_ref}: {exc}", file=sys.stderr)
            continue
    checked_areas = _coerce_str_list(tool_input.get("checked_areas"))
    return ReviewResult(findings, checked_areas)


def assign_finding_ids(findings: list[Finding]) -> None:
    """Assign finding_id: {designator}-{001}, {002}, ..."""
    counter: Counter[str] = Counter()
    for f in findings:
        counter[f.designator] += 1
        f.finding_id = f"{f.designator}-{counter[f.designator]:03d}"


# ---------------------------------------------------------------------------
# Datasheet loading (for pintable/constraints lookup)
# ---------------------------------------------------------------------------


def _load_datasheets(directory: str | Path) -> dict[str, ComponentConstraints]:
    """Load all extracted datasheet JSONs, keyed by MPN."""
    result: dict[str, ComponentConstraints] = {}
    dirpath = Path(directory)
    if not dirpath.is_dir():
        return result
    for f in dirpath.glob("*.json"):
        text = f.read_text().strip()
        if not text:
            log.warning("Empty datasheet JSON: %s", f)
            continue
        raw = json.loads(text)
        c = ComponentConstraints.model_validate(raw)
        result[c.mpn] = c
    return result


def _match_constraints(
    mpn: str | None,
    datasheets: dict[str, ComponentConstraints],
) -> ComponentConstraints | None:
    """Match a component MPN to extracted constraints (exact then normalized)."""
    if not mpn:
        return None
    if mpn in datasheets:
        return datasheets[mpn]
    norm = re.sub(r"[/_\-\s]", "", mpn).upper()
    for ds_mpn, constraints in datasheets.items():
        if re.sub(r"[/_\-\s]", "", ds_mpn).upper() == norm:
            return constraints
    return None


def _build_constraints_map(
    datasheets: dict[str, ComponentConstraints],
) -> ConstraintsMap:
    """Build MPN -> constraints map for tool lookups."""
    return dict(datasheets)


# ---------------------------------------------------------------------------
# Main (CLI entry point)
# ---------------------------------------------------------------------------


def validate_design(
    graph_path: str,
    pdf_dir: str,
    output_path: str = "report.json",
    datasheets_dir: str = "datasheets/extracted",
    model: str = "anthropic/claude-sonnet-4-6",
) -> ValidationReport:
    """Load graph, review every IC against its datasheet, write report."""
    from backend.pinscopex.utils import safe_mpn

    raw = json.loads(Path(graph_path).read_text())
    graph = DesignGraph.model_validate(raw)
    datasheets = _load_datasheets(datasheets_dir)
    constraints_map = _build_constraints_map(datasheets)

    all_findings: list[Finding] = []
    all_coverage: dict[str, list[str]] = {}

    pdf_dir_path = Path(pdf_dir)

    for ref, comp in sorted(graph.components.items()):
        if comp.component_type != ComponentType.IC:
            continue

        # Find the datasheet PDF
        mpn = comp.mpn or comp.value
        pdf_path = pdf_dir_path / f"{safe_mpn(mpn)}.pdf"
        if not pdf_path.is_file():
            print(f"Skipping {ref} ({mpn}) — no datasheet PDF at {pdf_path}")
            continue

        print(f"Reviewing {ref} ({mpn}) ...", flush=True)
        result = review_component(
            graph,
            constraints_map,
            ref,
            str(pdf_path),
            model=model,
        )
        all_findings.extend(result.findings)
        if result.checked_areas:
            all_coverage[ref] = result.checked_areas
        print(
            f"  {len(result.findings)} findings: "
            f"{sum(1 for f in result.findings if f.status == 'ERROR')} ERROR, "
            f"{sum(1 for f in result.findings if f.status == 'WARNING')} WARNING, "
            f"{sum(1 for f in result.findings if f.status == 'INFO')} INFO"
        )
        if result.checked_areas:
            print(f"  Checked OK: {', '.join(result.checked_areas)}")

    assign_finding_ids(all_findings)

    summary = {"total": len(all_findings), "ERROR": 0, "WARNING": 0, "INFO": 0}
    for f in all_findings:
        summary[f.status] = summary.get(f.status, 0) + 1

    report = ValidationReport(
        project=Path(graph_path).stem,
        timestamp=datetime.now(timezone.utc).isoformat(),
        findings=all_findings,
        summary=summary,
        coverage=all_coverage,
    )

    Path(output_path).write_text(report.model_dump_json(indent=2))
    print(f"\nReport: {output_path}")
    print(
        f"Total: {summary['total']} — "
        f"{summary['ERROR']} ERROR, {summary['WARNING']} WARNING, {summary['INFO']} INFO"
    )
    return report


if __name__ == "__main__":
    gpath = sys.argv[1] if len(sys.argv) > 1 else "simple_project/design_graph.json"
    pdir = sys.argv[2] if len(sys.argv) > 2 else "simple_project/datasheets"
    opath = sys.argv[3] if len(sys.argv) > 3 else "simple_project/report.json"
    validate_design(gpath, pdir, opath)
