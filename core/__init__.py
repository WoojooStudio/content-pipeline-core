"""content-pipeline core — public API."""
from .counter import next_cnt, scaffold_cnt
from .db import get_db, init_db
from .gate import GateError, record_gate, require_gate1, require_gate2, submit_gate
from .idempotency import AlreadyDone, check_idempotent
from .index import rebuild as rebuild_index
from .ledger import read_all as read_ledger
from .ledger import record as record_ledger
from .plan import DEFAULT_PLAN, PlanError, load_plan, write_plan
from .schedule import ScheduleRejected, clear_schedule, set_schedule
from .slots import assign_next_slot
from .status import derive_status

__all__ = [
    "next_cnt", "scaffold_cnt",
    "get_db", "init_db",
    "GateError", "record_gate", "require_gate1", "require_gate2", "submit_gate",
    "AlreadyDone", "check_idempotent",
    "rebuild_index",
    "read_ledger", "record_ledger",
    "DEFAULT_PLAN", "PlanError", "load_plan", "write_plan",
    "ScheduleRejected", "clear_schedule", "set_schedule",
    "assign_next_slot",
    "derive_status",
]
