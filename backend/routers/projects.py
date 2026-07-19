"""Project CRUD and file upload endpoints."""

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 30 MB

from backend.config import settings
from backend.pinscopex.utils import safe_mpn
from backend.routers.deps import get_storage, get_user_id, resolve_or_404
from backend.services import projects as proj_svc

router = APIRouter(tags=["projects"])


# --- Library check ---


class LibraryCheckRequest(BaseModel):
    ic_mpns: list[str] = []
    passive_mpns: list[str] = []
    simple_mpns: list[str] = []


@router.post("/library/check")
async def check_library(req: LibraryCheckRequest, request: Request):
    """Check which MPNs are already resolved in the global library."""
    storage = get_storage(request)
    ic_resolved = [mpn for mpn in req.ic_mpns if proj_svc.library_has_extraction(storage, mpn)]

    patterns = proj_svc.load_library_patterns(storage) if req.passive_mpns else []

    passive_resolved: list[str] = []
    if req.passive_mpns:
        from backend.pinscopex.resolve_passives import resolve_mpn

        passive_resolved = [
            mpn for mpn in req.passive_mpns
            if resolve_mpn(mpn, patterns) is not None
            or proj_svc.library_has_passive_model(storage, mpn) is not None
        ]

    simple_resolved = [mpn for mpn in req.simple_mpns if proj_svc.library_has_model(storage, mpn)]

    # Check which MPNs already have datasheets in the library
    all_mpns = set(req.ic_mpns + req.passive_mpns + req.simple_mpns)
    datasheets_available = [
        mpn for mpn in all_mpns
        if proj_svc.library_has_datasheet(storage, mpn, patterns=patterns)
    ]

    return {
        "ic_resolved": ic_resolved,
        "passive_resolved": passive_resolved,
        "simple_resolved": simple_resolved,
        "datasheets_available": datasheets_available,
    }


class CreateProjectRequest(BaseModel):
    name: str


# --- CRUD ---


@router.post("/projects")
async def create_project(req: CreateProjectRequest, request: Request):
    """Create a new project.  No per-user project cap — credits are the rate limiter."""
    storage = get_storage(request)
    user_id = get_user_id(request)
    meta = proj_svc.create_project(storage, user_id, req.name)
    return meta.model_dump()


@router.get("/projects")
async def list_projects(request: Request):
    storage = get_storage(request)
    user_id = get_user_id(request)
    owned = proj_svc.list_projects(storage, user_id)
    shared = proj_svc.list_shared_projects(storage, user_id)
    return [m.model_dump() for m in owned + shared]


@router.get("/projects/{project_id}")
async def get_project(project_id: str, request: Request):
    _, meta = await resolve_or_404(request, project_id)
    return meta.model_dump()


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    storage = get_storage(request)
    user_id = get_user_id(request)

    # If user is the owner, delete the project
    if proj_svc.delete_project(storage, user_id, project_id):
        return {"ok": True}

    # If user is a collaborator, remove themselves instead of deleting
    result = proj_svc.resolve_project_access(storage, user_id, project_id)
    if result:
        owner_user_id, _ = result
        proj_svc.remove_collaborator(storage, owner_user_id, project_id, user_id)
        return {"ok": True, "removed_self": True}

    raise HTTPException(404, "Project not found")


# --- Reopen (cancelled / error / complete → draft, for rerun) ---


class RenameRequest(BaseModel):
    name: str


@router.patch("/projects/{project_id}")
async def rename_project(project_id: str, req: RenameRequest, request: Request):
    """Update a project's display name."""
    storage = get_storage(request)
    result = proj_svc.resolve_project_access(storage, get_user_id(request), project_id)
    if not result:
        raise HTTPException(404, "Project not found")
    owner_user_id, _ = result
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name must be non-empty")
    meta = proj_svc.update_project(storage, owner_user_id, project_id, name=name)
    return meta.model_dump()


@router.post("/projects/{project_id}/reopen")
async def reopen_project(project_id: str, request: Request):
    """Flip a finished-state project back to draft so the user can rerun it.

    Preserves uploads, column mappings, power-source hints, extraction
    cache, and historical spend. Clears pipeline artifacts (graph, report,
    etc.) and the pause/review bookkeeping so the next run starts clean.
    """
    storage = get_storage(request)
    result = proj_svc.resolve_project_access(storage, get_user_id(request), project_id)
    if not result:
        raise HTTPException(404, "Project not found")
    owner_user_id, meta = result
    if meta.status in (proj_svc.STATUS_RUNNING, proj_svc.STATUS_QUEUED):
        raise HTTPException(409, "Pipeline is running; cancel it before reopening")
    meta = proj_svc.reopen_project(storage, owner_user_id, project_id)
    return meta.model_dump()


# --- File downloads + datasheet inventory (for rerun prefill) ---


@router.get("/projects/{project_id}/files/bom")
async def download_bom(project_id: str, request: Request):
    storage = get_storage(request)
    owner_user_id, _ = await resolve_or_404(request, project_id)
    key = proj_svc.get_bom_key(storage, owner_user_id, project_id)
    if not key:
        raise HTTPException(404, "BOM not uploaded")
    return Response(
        content=storage.read_bytes(key),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bom.csv"'},
    )


@router.get("/projects/{project_id}/files/netlist")
async def download_netlist(project_id: str, request: Request):
    storage = get_storage(request)
    owner_user_id, _ = await resolve_or_404(request, project_id)
    key = proj_svc.get_netlist_key(storage, owner_user_id, project_id)
    if not key:
        raise HTTPException(404, "Netlist not uploaded")
    # Reflect the stored extension (.asc for PADS, .edn for EDIF) in the
    # download filename so the user gets back what they uploaded.
    ext = key.rsplit(".", 1)[-1] if "." in key.rsplit("/", 1)[-1] else "asc"
    return Response(
        content=storage.read_bytes(key),
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="netlist.{ext}"',
        },
    )


@router.get("/projects/{project_id}/netlist/subdesigns")
async def get_netlist_subdesigns(project_id: str, request: Request):
    """Return the sub-design layout of an uploaded EDIF netlist.

    Re-parses the stored ``.edn`` file. For PADS netlists or single-sub-design
    EDIF, returns an empty list. Also returns the currently-saved
    ``selected`` list (None = "include everything") so the wizard can render
    the picker pre-populated.
    """
    from backend.pinscopex.parsers_edif import list_edif_subdesigns
    import tempfile, os

    storage = get_storage(request)
    owner_user_id, meta = await resolve_or_404(request, project_id)
    if meta.netlist_format != "edif":
        return {"sub_designs": [], "selected": None}
    key = proj_svc.get_netlist_key(storage, owner_user_id, project_id)
    if not key:
        return {"sub_designs": [], "selected": None}
    data = storage.read_bytes(key)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".edn")
    try:
        tmp.write(data)
        tmp.close()
        subs = list_edif_subdesigns(tmp.name)
    finally:
        os.unlink(tmp.name)
    return {"sub_designs": subs, "selected": meta.netlist_subdesigns}


class NetlistSubdesignsUpdate(BaseModel):
    selected: list[str] | None  # null = include every sub-design


@router.put("/projects/{project_id}/netlist/subdesigns")
async def set_netlist_subdesigns(
    project_id: str, payload: NetlistSubdesignsUpdate, request: Request,
):
    """Persist the user's sub-design selection for an EDIF netlist.

    ``selected = null`` means "include every sub-design" (the default and
    only meaningful value for PADS / single-sub-design EDIF). Pipeline runs
    pass this list to the parser to filter instances/nets.
    """
    storage = get_storage(request)
    user_id = get_user_id(request)
    result = proj_svc.resolve_project_access(storage, user_id, project_id)
    if not result:
        raise HTTPException(404, "Project not found")
    owner_user_id = result[0]
    cleaned = [s.strip() for s in (payload.selected or []) if s and s.strip()]
    meta = proj_svc.update_project(
        storage, owner_user_id, project_id,
        netlist_subdesigns=cleaned if payload.selected is not None else None,
    )
    return meta.model_dump()


@router.get("/projects/{project_id}/files/datasheets")
async def list_datasheets(project_id: str, request: Request):
    """List safe-MPN stems for datasheets uploaded to this project.

    Returned stems are the filename prefix (filename without ``.pdf``).
    The frontend classifies the BOM to recover MPNs and matches each
    against these stems via its own safe_mpn() mirror.
    """
    storage = get_storage(request)
    owner_user_id, _ = await resolve_or_404(request, project_id)
    stems = proj_svc.list_project_datasheets(storage, owner_user_id, project_id)
    return {"stems": sorted(stems)}


# --- File uploads ---


@router.post("/projects/{project_id}/upload/bom")
async def upload_bom(
    project_id: str,
    file: UploadFile,
    request: Request,
    reference_column: str = "Reference",
    mpn_column: str = "Manufacturer Part Number",
    column_is_lcsc: bool | None = None,
):
    storage = get_storage(request)
    result = proj_svc.resolve_project_access(storage, get_user_id(request), project_id)
    if not result:
        raise HTTPException(404, "Project not found")
    user_id = result[0]  # owner_user_id for storage paths
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    # Convert xlsx to CSV if needed
    filename = file.filename or ""
    if filename.lower().endswith(".xlsx"):
        try:
            import io, openpyxl, csv as csv_mod

            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.active
            out = io.StringIO()
            writer = csv_mod.writer(out)
            for row in ws.iter_rows(values_only=True):
                writer.writerow([("" if c is None else str(c)) for c in row])
            wb.close()
            data = out.getvalue().encode("utf-8")
        except Exception as e:
            raise HTTPException(400, f"Invalid Excel file: {e}")

    # Validate by attempting to parse
    import os
    import tempfile

    from backend.pinscopex.parsers import parse_bom

    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
        tmp.write(data)
        tmp.close()
        bom = parse_bom(tmp.name, reference_col=reference_column, mpn_col=mpn_column)
        os.unlink(tmp.name)
    except Exception as e:
        raise HTTPException(400, f"Invalid BOM file: {e}")

    # If the chosen MPN column is entirely LCSC ids, resolve them all to
    # real manufacturer part numbers before storing the BOM. Column-level
    # only: every non-empty cell must match ^C\d+$, or the column is left
    # alone. Mixed BOMs (some real MPNs, some LCSC ids) are out of scope —
    # users must pick a single representation per column. The resolved
    # mapping is also stashed in project metadata so the wizard UI can
    # surface "C12044 → STM32F103C8T6" to the user.
    lcsc_resolved = 0
    lcsc_detected = False
    lcsc_map: dict[str, str] = {}
    lcsc_payloads: dict[str, dict] = {}
    try:
        from backend.services.purple_parts import (
            detect_lcsc_column, resolve_lcsc_column_bytes,
        )
        force_lcsc = column_is_lcsc
        lcsc_detected = detect_lcsc_column(data, mpn_column)
        if force_lcsc or lcsc_detected:
            data, lcsc_resolved, lcsc_map, lcsc_payloads = await resolve_lcsc_column_bytes(
                data, mpn_col=mpn_column,
            )
    except Exception:
        # Resolver failures must not block uploads — pipeline-stage resolver
        # is still a backstop, and the user can manually upload datasheets.
        import logging
        logging.getLogger(__name__).warning("purple-parts BOM resolve failed", exc_info=True)

    # If the LCSC rewrite ran, reparse the BOM so component classification
    # below sees the resolved MPNs (and so the count we return matches what
    # the pipeline will see).
    if lcsc_resolved:
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
            tmp.write(data)
            tmp.close()
            bom = parse_bom(tmp.name, reference_col=reference_column, mpn_col=mpn_column)
            os.unlink(tmp.name)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Re-parse after LCSC rewrite failed; using pre-rewrite BOM for classification",
                exc_info=True,
            )

    # Classify components at upload time so the wizard can render the right
    # per-row resolution UI (ic → datasheet upload, passive → lcsc-resolve,
    # simple → datasheet upload). Mirrors the bucket logic in
    # services/pipeline.py:_stage_bom_parse so the field is correct after
    # either path runs.
    from backend.pinscopex.taxonomy import SIMPLE_TYPES, type_for_ref

    ic_mpns: list[str] = []
    passive_mpns: list[str] = []
    simple_mpns: list[str] = []
    _seen_ic: set[str] = set()
    _seen_passive: set[str] = set()
    _seen_simple: set[str] = set()
    for ref, info in sorted(bom.items()):
        mpn = info.get("mpn")
        if not mpn:
            continue
        typ = type_for_ref(ref)
        if typ == "ic":
            if mpn not in _seen_ic:
                _seen_ic.add(mpn)
                ic_mpns.append(mpn)
        elif typ == "passive":
            if mpn not in _seen_passive:
                _seen_passive.add(mpn)
                passive_mpns.append(mpn)
        elif typ and typ in SIMPLE_TYPES:
            if mpn not in _seen_simple:
                _seen_simple.add(mpn)
                simple_mpns.append(mpn)

    # Real-MPN BOMs: enrich passives from the LCSC catalogue by reverse
    # MPN lookup so the wizard can pre-resolve their specs through the exact
    # same machinery as the LCSC-column path. We populate the LCSC maps keyed
    # by the catalogue's LCSC id, so the wizard's mpn→lcsc map lights up the
    # "Resolving Passive Specs" step and /lcsc/resolve-passive handles them.
    # Genuine MPN columns only — skip when the column was LCSC ids (handled
    # above) so we never feed raw LCSC ids into by-mpn.
    if (
        settings.use_purple_parts
        and not lcsc_detected
        and not column_is_lcsc
        and passive_mpns
    ):
        try:
            from backend.services.purple_parts import lookup_mpn_batch
            parts = await lookup_mpn_batch(passive_mpns)
            for pmpn, part in parts.items():
                if part and part.get("lcsc") and part.get("description"):
                    lcsc_map[part["lcsc"]] = pmpn
                    lcsc_payloads[part["lcsc"]] = dict(part)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "purple-parts by-mpn passive enrich failed", exc_info=True,
            )

    key = proj_svc.save_bom(storage, user_id, project_id, data)
    # Store column mappings for the pipeline to use.
    # Stash the LCSC → MPN map (if any) so the wizard UI can render it.
    update_kwargs: dict = {
        "bom_columns": {"reference": reference_column, "mpn": mpn_column},
        "component_mpns": {
            "ic": ic_mpns,
            "passive": passive_mpns,
            "simple": simple_mpns,
        },
    }
    if lcsc_map:
        update_kwargs["lcsc_to_mpn"] = lcsc_map
    if lcsc_payloads:
        update_kwargs["lcsc_payloads"] = lcsc_payloads
    proj_svc.update_project(storage, user_id, project_id, **update_kwargs)
    return {
        "path": key,
        "components": len(bom),
        "lcsc_resolved": lcsc_resolved,
        "lcsc_detected": lcsc_detected,
        "lcsc_to_mpn": lcsc_map,
    }


@router.post("/projects/{project_id}/upload/netlist")
async def upload_netlist(project_id: str, file: UploadFile, request: Request):
    storage = get_storage(request)
    result = proj_svc.resolve_project_access(storage, get_user_id(request), project_id)
    if not result:
        raise HTTPException(404, "Project not found")
    user_id = result[0]  # owner_user_id for storage paths
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")

    # Auto-detect PADS vs EDIF from the file's first bytes — users don't pick
    # a format, the wizard accepts either.
    from backend.pinscopex.parsers import (
        detect_netlist_format, parse_netlist_any, validate_netlist,
    )
    from backend.pinscopex.parsers_edif import list_edif_subdesigns
    import tempfile, os

    fmt = detect_netlist_format(data)
    suffix = ".edn" if fmt == "edif" else ".asc"
    sub_designs: list[dict] = []
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(data)
        tmp.close()
        parts, nets, _ = parse_netlist_any(tmp.name)
        # For EDIF, also surface the sub-design layout so the wizard can
        # decide whether to prompt the user. Cheap second parse — same file.
        if fmt == "edif":
            sub_designs = list_edif_subdesigns(tmp.name)
        os.unlink(tmp.name)
    except Exception as e:
        raise HTTPException(400, f"Invalid netlist: {e}")
    issues = validate_netlist(parts, nets)
    if issues:
        raise HTTPException(400, f"Netlist failed sanity check: {'; '.join(issues)}")
    key = proj_svc.save_netlist(storage, user_id, project_id, data, fmt=fmt)
    # EDIF: emit a designator→pins preview matching the PADS browser-side
    # shape, so the wizard's power-sources step can render its dropdowns
    # without re-parsing the (s-expression-heavy) file in the browser.
    designator_pins: list[dict] = []
    if fmt == "edif":
        designator_pins = _build_designator_pins(parts, nets)
    return {
        "path": key,
        "parts": len(parts),
        "nets": len(nets),
        "format": fmt,
        "sub_designs": sub_designs,
        "designator_pins": designator_pins,
    }


def _build_designator_pins(
    parts: dict[str, str],
    nets: dict[str, list[tuple[str, str]]],
) -> list[dict]:
    """Flatten parsed netlist into [{ref, pins:[{number, net_name}]}].

    Inverts the net→[(ref, pin)] adjacency from ``parse_netlist_any`` into
    a per-designator list. Output order matches the PADS browser preview
    (natural sort on refs and on pin numbers) so the wizard's dropdowns
    look identical regardless of netlist format.
    """
    from backend.pinscopex.utils import natural_sort_key

    by_ref: dict[str, dict[str, str]] = {ref: {} for ref in parts}
    for net_name, pins in nets.items():
        for ref, pin in pins:
            ref_pins = by_ref.setdefault(ref, {})
            ref_pins.setdefault(pin, net_name)

    out: list[dict] = []
    for ref in sorted(by_ref, key=natural_sort_key):
        pin_map = by_ref[ref]
        sorted_pins = [
            {"number": num, "net_name": pin_map[num]}
            for num in sorted(pin_map, key=natural_sort_key)
        ]
        out.append({"ref": ref, "pins": sorted_pins})
    return out


class DatasheetUploadMeta(BaseModel):
    mpn: str


@router.post("/projects/{project_id}/upload/datasheets")
async def upload_datasheets(
    project_id: str, file: UploadFile, mpn: str,
    request: Request, also_for: str | None = None,
):
    """Upload a datasheet PDF for a specific MPN, optionally saving for additional MPNs."""
    storage = get_storage(request)
    result = proj_svc.resolve_project_access(storage, get_user_id(request), project_id)
    if not result:
        raise HTTPException(404, "Project not found")
    user_id = result[0]  # owner_user_id for storage paths
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "File must be a PDF")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        size_mb = len(data) / 1024 / 1024
        raise HTTPException(
            413,
            f"{file.filename or mpn} is {size_mb:.1f} MB — exceeds {MAX_UPLOAD_BYTES // 1024 // 1024} MB limit",
        )
    key = proj_svc.save_datasheet(storage, user_id, project_id, mpn, data)
    # Save same file under additional MPN names (for shared passive datasheets)
    extra_mpns: list[str] = []
    if also_for:
        for extra_mpn in also_for.split(","):
            extra_mpn = extra_mpn.strip()
            if extra_mpn:
                proj_svc.save_datasheet(storage, user_id, project_id, extra_mpn, data)
                extra_mpns.append(extra_mpn)
    return {"path": key, "mpn": mpn, "also_for": extra_mpns}


# --- Collaborators ---


class AddCollaboratorRequest(BaseModel):
    email: str


@router.get("/projects/{project_id}/collaborators")
async def list_collaborators(project_id: str, request: Request):
    """List collaborators for a project. Accessible by owner and collaborators."""
    storage = get_storage(request)
    user_id = get_user_id(request)
    result = proj_svc.resolve_project_access(storage, user_id, project_id)
    if not result:
        raise HTTPException(404, "Project not found")
    owner_user_id, meta = result

    # Build member list: owner first, then collaborators
    all_user_ids = [owner_user_id] + [c for c in meta.collaborators if c != owner_user_id]
    collaborators = []
    if settings.use_auth:
        async with httpx.AsyncClient() as client:
            for uid in all_user_ids:
                entry: dict = {"user_id": uid, "name": None, "email": None, "image_url": None,
                               "role": "owner" if uid == owner_user_id else "collaborator"}
                try:
                    resp = await client.get(
                        f"https://api.clerk.com/v1/users/{uid}",
                        headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
                    )
                    if resp.status_code == 200:
                        clerk = resp.json()
                        first = clerk.get("first_name") or ""
                        last = clerk.get("last_name") or ""
                        entry["name"] = f"{first} {last}".strip() or None
                        emails = clerk.get("email_addresses", [])
                        if emails:
                            entry["email"] = emails[0].get("email_address")
                        entry["image_url"] = clerk.get("image_url")
                except Exception:
                    pass
                collaborators.append(entry)
    else:
        # Local dev — just return user_ids without enrichment
        collaborators = [
            {"user_id": uid, "name": None, "email": None, "image_url": None,
             "role": "owner" if uid == owner_user_id else "collaborator"}
            for uid in all_user_ids
        ]

    return {"owner_user_id": owner_user_id, "collaborators": collaborators}


@router.post("/projects/{project_id}/collaborators")
async def add_collaborator(project_id: str, req: AddCollaboratorRequest, request: Request):
    """Add a collaborator by email. Owner only."""
    storage = get_storage(request)
    user_id = get_user_id(request)

    # Only the owner can add collaborators
    meta = proj_svc.get_project(storage, user_id, project_id)
    if not meta:
        raise HTTPException(404, "Project not found")

    if not settings.use_auth:
        raise HTTPException(400, "Collaboration requires authentication to be enabled")

    # Look up user by email via Clerk Backend API
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.clerk.com/v1/users",
            params={"email_address": [req.email]},
            headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
        )
    if resp.status_code != 200:
        raise HTTPException(502, "Failed to look up user")

    users = resp.json()
    if not users:
        raise HTTPException(404, "No user found with that email")

    clerk_user = users[0]
    collab_user_id = clerk_user.get("id")
    if not collab_user_id:
        raise HTTPException(404, "No user found with that email")

    # Can't add yourself
    if collab_user_id == user_id:
        raise HTTPException(400, "Cannot add yourself as a collaborator")

    # Check if already a collaborator
    if collab_user_id in meta.collaborators:
        raise HTTPException(409, "User is already a collaborator")

    proj_svc.add_collaborator(storage, user_id, project_id, collab_user_id)

    # Return the collaborator info
    first = clerk_user.get("first_name") or ""
    last = clerk_user.get("last_name") or ""
    emails = clerk_user.get("email_addresses", [])
    return {
        "user_id": collab_user_id,
        "name": f"{first} {last}".strip() or None,
        "email": emails[0].get("email_address") if emails else None,
        "image_url": clerk_user.get("image_url"),
    }


@router.delete("/projects/{project_id}/collaborators/{collaborator_user_id}")
async def remove_collaborator(project_id: str, collaborator_user_id: str, request: Request):
    """Remove a collaborator. Owner or admin."""
    from backend.routers.admin import is_admin

    storage = get_storage(request)
    user_id = get_user_id(request)

    meta = proj_svc.get_project(storage, user_id, project_id)
    owner_user_id = user_id
    if not meta:
        # Admin can remove a collaborator from a project they don't own.
        if not await is_admin(request):
            raise HTTPException(404, "Project not found")
        result = proj_svc.find_project_any_user(storage, project_id)
        if not result:
            raise HTTPException(404, "Project not found")
        owner_user_id, meta = result

    if collaborator_user_id not in meta.collaborators:
        raise HTTPException(404, "User is not a collaborator")

    proj_svc.remove_collaborator(storage, owner_user_id, project_id, collaborator_user_id)
    return {"ok": True}


@router.post("/projects/{project_id}/collaborators/{collaborator_user_id}/make-owner")
async def make_collaborator_owner(
    project_id: str, collaborator_user_id: str, request: Request,
):
    """Promote a collaborator to owner. Admin only.

    Used when Sid creates a project on behalf of another user and needs to
    hand it off cleanly. The current owner is demoted to a collaborator;
    Sid (or any admin) can then remove themselves via DELETE in a second
    action.
    """
    from backend.routers.admin import is_admin

    if not await is_admin(request):
        raise HTTPException(403, "Admin access required")

    storage = get_storage(request)
    result = proj_svc.find_project_any_user(storage, project_id)
    if not result:
        raise HTTPException(404, "Project not found")
    current_owner_user_id, _ = result

    try:
        proj_svc.transfer_ownership(
            storage, current_owner_user_id, project_id, collaborator_user_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {"ok": True, "owner_user_id": collaborator_user_id}


# --- DigiKey auto-fetch ---


@router.get("/digikey/datasheet")
async def fetch_digikey_datasheet(mpn: str, request: Request):
    """Fetch a datasheet PDF from DigiKey for the given MPN.

    Returns the PDF bytes on success, or a JSON error on failure.
    """
    from backend.services.digikey import fetch_datasheet

    result = await fetch_datasheet(mpn)
    if not result.ok:
        # 404, not 502: "DigiKey has no exact match" / "the manufacturer CDN
        # blocked the download" is an expected per-MPN miss the wizard handles
        # (it shows a "fetch failed — upload manually" row), not a broken
        # gateway. 502 made a board full of exotic parts read as a server
        # meltdown in the browser console.
        return JSONResponse(
            status_code=404,
            content={"detail": result.error or "Failed to fetch datasheet", "url": result.url},
        )
    headers = {"Content-Disposition": f'attachment; filename="{mpn}.pdf"'}
    if result.url:
        headers["X-Datasheet-Url"] = result.url
    return Response(content=result.pdf_bytes, media_type="application/pdf", headers=headers)


# --- DigiKey auto-resolve ---


class AutoResolveItem(BaseModel):
    mpn: str
    component_type: str  # "discrete", "connector", "crystal", etc.


class AutoResolveRequest(BaseModel):
    items: list[AutoResolveItem]


@router.post("/digikey/auto-resolve")
async def auto_resolve(req: AutoResolveRequest, request: Request):
    """Auto-resolve simple component specs via DigiKey params + Haiku mapping.

    Fetches structured parameters from DigiKey for each MPN, maps them to
    taxonomy specs using a lightweight Claude model, and saves results to
    the shared library. Batches up to 10 DigiKey calls in parallel.
    """
    import asyncio

    from backend.services.digikey import fetch_params
    from backend.services.extraction import auto_resolve_specs

    if not settings.use_digikey:
        raise HTTPException(400, "DigiKey API not configured")
    #if not settings.anthropic_api_key:
    #    raise HTTPException(400, "Anthropic API key not configured")

    storage = get_storage(request)
    sem = asyncio.Semaphore(10)

    async def resolve_one(item: AutoResolveItem) -> dict:
        async with sem:
            try:
                # Skip if already in library
                safe = safe_mpn(item.mpn)
                if item.component_type == "passive":
                    lib_key = f"library/passives/{safe}.json"
                    # Also check legacy location for pre-migration data
                    if not storage.exists(lib_key):
                        legacy_key = f"library/models/{safe}.json"
                        if storage.exists(legacy_key):
                            return {"mpn": item.mpn, "status": "resolved"}
                else:
                    lib_key = f"library/models/{safe}.json"
                if storage.exists(lib_key):
                    return {"mpn": item.mpn, "status": "resolved"}

                # Fetch params from DigiKey
                result = await fetch_params(item.mpn)
                if not result.ok or not result.params:
                    return {"mpn": item.mpn, "status": "failed", "error": result.error or "No parameters"}

                # Map params to taxonomy via Haiku
                model = await auto_resolve_specs(
                    mpn=item.mpn,
                    digikey_params=result.params.parameters,
                    digikey_category=result.params.category,
                    digikey_description=result.params.description,
                    component_type=item.component_type,
                )

                # Save to library
                storage.write_json(lib_key, model.model_dump())
                return {"mpn": item.mpn, "status": "resolved"}

            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Auto-resolve failed for %s: %s", item.mpn, e, exc_info=True,
                )
                msg = str(e) or type(e).__name__
                return {"mpn": item.mpn, "status": "failed", "error": msg}

    results = await asyncio.gather(*(resolve_one(item) for item in req.items))
    return {"results": results}


# --- LCSC per-row passive resolve (wizard-driven) ---


class LcscResolvePassiveRequest(BaseModel):
    lcsc_id: str


@router.post("/projects/{project_id}/lcsc/resolve-passive")
async def lcsc_resolve_passive(
    project_id: str, req: LcscResolvePassiveRequest, request: Request,
):
    """Resolve a single passive component to specs using its cached LCSC payload.

    Called by the wizard frontend per-row. The LCSC payload (mpn, manufacturer,
    package, description, category, subcategory) was cached on the project at
    BOM upload time. We synthesize a DigiKey-shaped payload from it and reuse
    ``auto_resolve_specs`` — the same path the pipeline takes during the
    passive extraction stage.

    Returns ``{mpn, safe_mpn, model, cached, lcsc_id}``.

    Errors:
      - 404 if ``lcsc_id`` is not in the project's ``lcsc_payloads`` cache
      - 402 with ``{reason, required, available}`` on insufficient credits
      - 502 on extraction failure (with the underlying error)
    """
    import tempfile
    from pathlib import Path

    from backend.services.api_logs import ApiLogger
    from backend.services.billing_hook import InsufficientCredits, get_billing
    from backend.services.extraction import auto_resolve_specs

    storage = get_storage(request)
    result = proj_svc.resolve_project_access(storage, get_user_id(request), project_id)
    if not result:
        raise HTTPException(404, "Project not found")
    owner_user_id, meta = result

    payloads = meta.lcsc_payloads or {}
    payload = payloads.get(req.lcsc_id)
    if not payload:
        raise HTTPException(404, f"No cached payload for LCSC id {req.lcsc_id!r}")

    mpn = (payload.get("mpn") or "").strip()
    if not mpn:
        raise HTTPException(404, f"Cached payload for {req.lcsc_id!r} has no MPN")

    safe = safe_mpn(mpn)
    project_model_key = (
        f"{proj_svc.project_prefix(owner_user_id, project_id)}/models/{safe}.json"
    )

    # Short-circuit: if the per-project model file already exists, return it
    # without re-charging. The pipeline's passive stage already short-circuits
    # the same file, so re-running the pipeline after this won't double-charge.
    if storage.exists(project_model_key):
        model_data = storage.read_json(project_model_key)
        return {
            "mpn": mpn,
            "safe_mpn": safe,
            "model": model_data,
            "cached": True,
            "lcsc_id": req.lcsc_id,
        }

    # Library hit: copy into project storage and return without charging.
    lib_key = proj_svc.library_has_passive_model(storage, mpn)
    if lib_key:
        storage.copy_object(lib_key, project_model_key)
        model_data = storage.read_json(project_model_key)
        return {
            "mpn": mpn,
            "safe_mpn": safe,
            "model": model_data,
            "cached": True,
            "lcsc_id": req.lcsc_id,
        }

    # No cache. Synthesize a DigiKey-shaped payload and call auto_resolve_specs.
    # Same shape used by the pipeline's LCSC-first branch in
    # services/pipeline.py:_stage_passive_extraction.
    synth_category = " / ".join(
        p for p in (payload.get("category"), payload.get("subcategory")) if p
    ) or None
    synth_params: list[dict[str, str]] = []
    if payload.get("package"):
        synth_params.append({"name": "Package / Case", "value": payload["package"]})
    if payload.get("manufacturer"):
        synth_params.append({"name": "Manufacturer", "value": payload["manufacturer"]})
    description = payload.get("description") or ""
    if not description:
        raise HTTPException(
            502,
            f"Cached LCSC payload for {req.lcsc_id!r} has no description — "
            "cannot auto-resolve",
        )

    # Download taxonomy to a temp dir so auto_resolve_specs can read/write it.
    # Mirrors the PipelineWorkspace pattern: pinscopex operates on local paths.
    api_logger = ApiLogger()
    with tempfile.TemporaryDirectory() as tmpdir:
        tax_dir = Path(tmpdir) / "taxonomy"
        tax_dir.mkdir()
        for key in storage.list_prefix("taxonomy/"):
            if key.endswith(".json"):
                filename = key.rsplit("/", 1)[-1]
                storage.download_to_local(key, tax_dir / filename)
        # Seed from repo taxonomy if storage had no taxonomy files yet
        if not any(tax_dir.glob("*.json")):
            repo_tax = settings.taxonomy_dir
            if repo_tax.is_dir():
                import shutil

                for f in repo_tax.glob("*.json"):
                    shutil.copy2(f, tax_dir / f.name)

        try:
            model = await auto_resolve_specs(
                mpn=mpn,
                digikey_params=synth_params,
                digikey_category=synth_category or "",
                digikey_description=description,
                component_type="passive",
                taxonomy_dir=tax_dir,
                api_logger=api_logger,
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "lcsc_resolve_passive: auto_resolve_specs failed for %s (lcsc=%s)",
                mpn, req.lcsc_id, exc_info=True,
            )
            raise HTTPException(502, f"Auto-resolve failed: {exc}") from exc

    # Charge the caller for the API work. The general-purpose primitive is
    # billing charge — we don't have a PipelineContext here, so this
    # skips the pipeline's pause/resume machinery. allow_overdraft=False
    # gives the caller a clean 402 if their balance is too low. The work has
    # already been done; on shortage we refuse to persist the resolved model
    # so the user doesn't get the spec for free, and return 402 so the UI can
    # prompt for top-up. Top-up + retry will redo the resolve (one API call),
    # which is cheap.
    total_credits = sum(float(e.get("credits_charged") or 0) for e in api_logger.entries)
    insufficient_exc: InsufficientCredits | None = None
    if total_credits > 0:
        try:
            get_billing().charge(
                storage, owner_user_id, round(total_credits, 4),
                reason="pipeline_charge",
                run_id=project_id,
                unit_id=f"lcsc_resolve_passive:{mpn}",
                allow_overdraft=False,
            )
        except InsufficientCredits as exc:
            insufficient_exc = exc

    if insufficient_exc is not None:
        # Work already done but we refuse to persist when the caller can't
        # afford it — otherwise we'd give resolved specs away for free.
        raise HTTPException(
            402,
            detail={
                "reason": "insufficient_credits",
                "required": insufficient_exc.required,
                "available": insufficient_exc.available,
            },
        )

    # Persist to project storage and the shared library (MPN-backed, mirrors
    # the pipeline LCSC branch).
    storage.write_json(project_model_key, model.model_dump())
    proj_svc.save_to_library(
        storage, project_model_key, "passives", f"{safe}.json",
    )

    # Append the api logger entries to the project's api_logs.jsonl so
    # the cost shows up in admin and per-project reporting. Mirrors
    # ApiLogger.flush but appends instead of overwriting.
    try:
        logs_key = (
            f"{proj_svc.project_prefix(owner_user_id, project_id)}/api_logs.jsonl"
        )
        existing = storage.read_text(logs_key) if storage.exists(logs_key) else ""
        appended = existing + api_logger.to_jsonl()
        if appended:
            storage.write_text(logs_key, appended)
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "lcsc_resolve_passive: failed to append api_logs.jsonl", exc_info=True,
        )

    # Bump the project's recorded total cost so admin/usage reflects this work.
    try:
        from backend.services.api_logs import total_cost as _total_cost

        added_cost = _total_cost(api_logger.entries)
        if added_cost > 0:
            current_total = float(meta.total_cost_usd or 0)
            proj_svc.update_project(
                storage, owner_user_id, project_id,
                total_cost_usd=round(current_total + added_cost, 6),
                credits_spent=round(float(meta.credits_spent or 0) + total_credits, 4),
            )
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "lcsc_resolve_passive: failed to update total_cost_usd", exc_info=True,
        )

    return {
        "mpn": mpn,
        "safe_mpn": safe,
        "model": model.model_dump(),
        "cached": False,
        "lcsc_id": req.lcsc_id,
    }
