"""Army settings of the battle plan: execution mode and cohesion (the buttons on an
army's battle plan bar). Read from 1.19.3 on 18.10.2026:

  CSetOrderGroupExecutionTypeCommand (0x30)  +0x24 handle COrdersGroup (type 53)
                                             +0x2c i32 type -> COrdersGroup +0x1A4
  CSetOrderGroupCohesionTypeCommand (0x30)   same, -> COrdersGroup +0x1A8
  An army group passes the setting on to its armies.

EExecutionType (common/defines NMilitary, PLAN_EXECUTE_SUPPLY_CHECK): 0 careful (attacks
only with full supply, avoids forts >= 5), 1 balanced, 2 rush (attacks whatever the plan
holds, even if the line grows longer; no supply check), 4 rush_weak.
Cohesion (PLAN_COHESION_WEIGHTS): 0 flexible, 1 balanced, 2 rigid, 3 immobile.
"""
from industry import CMD, _send

CMD["SetExecution"] = (0x4469520, 0x30)
CMD["SetCohesion"] = (0x44695E8, 0x30)
OG_HANDLE, OG_EXECUTION, OG_COHESION = 0x08, 0x1A4, 0x1A8
EXECUTION = {"careful": 0, "balanced": 1, "rush": 2}
COHESION = {"flexible": 0, "balanced": 1, "rigid": 2}


def settings(g, og):
    p = g.p
    inv = lambda d, v: next((k for k, x in d.items() if x == v), v)
    return {"execution": inv(EXECUTION, p.i32(og + OG_EXECUTION)),
            "cohesion": inv(COHESION, p.i32(og + OG_COHESION))}


def set_mode(g, og, execution=None, cohesion=None):
    """Set an army's (or army group's) execution mode and/or cohesion by name."""
    p = g.p
    h = p.u64(og + OG_HANDLE)
    out = {}
    if execution is not None:
        ok, why = _send(g, "SetExecution", [(0x24, "<Q", h), (0x2C, "<i", EXECUTION[execution])])
        out["execution"] = execution if ok else "refused: %s" % why
    if cohesion is not None:
        ok, why = _send(g, "SetCohesion", [(0x24, "<Q", h), (0x2C, "<i", COHESION[cohesion])])
        out["cohesion"] = cohesion if ok else "refused: %s" % why
    return out
