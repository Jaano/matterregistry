import json

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlmodel import Session

from ..audit import log as audit_log
from ..database import get_session
from ..importer import ImportPlan, apply_import, plan_import

router = APIRouter(tags=["import"])


def _plan_to_dict(plan: ImportPlan) -> dict:
    return {
        "creates": plan.creates,
        "updates": plan.updates,
        "skips": plan.skips,
        "errors": plan.errors,
        "warnings": plan.warnings,
    }


@router.post("/import")
async def import_backup(
    request: Request,
    file: UploadFile = File(...),
    policy: str = "skip",
    commit: bool = False,
    session: Session = Depends(get_session),
):
    """Dry-run (default) or commit a full JSON backup.

    policy: skip (default) - preserve existing rows; replace - overwrite by ID.
    commit: false (default) - return the plan without writing; true - apply and commit.
    """
    if policy not in ("skip", "replace"):
        raise HTTPException(status_code=422, detail="policy must be 'skip' or 'replace'")
    try:
        content = await file.read()
        payload = json.loads(content)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse JSON: {exc}")

    if commit:
        plan = apply_import(session, payload, policy=policy)
        if plan.errors:
            raise HTTPException(status_code=422, detail=_plan_to_dict(plan))
        n = len(plan.creates) + len(plan.updates)
        action = "import.full" if policy == "replace" else "import.skip"
        reason = "ui.replace" if policy == "replace" else "ui.skip"
        audit_log(session, action=action, entity=f"import:{n}", reason=reason)
        session.commit()
    else:
        plan = plan_import(session, payload, policy=policy)

    return _plan_to_dict(plan)
