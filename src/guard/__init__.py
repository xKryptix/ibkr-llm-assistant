"""Risk guard: every order passes every gate or it is not sent."""
from src.guard.gates import CLOSE_GATES, OPEN_GATES, GuardContext
from src.guard.guard import Approval, Decision, evaluate
from src.guard.kill_switch import KillSwitch, halt_file_present, kill_reason

__all__ = [
  "CLOSE_GATES",
  "OPEN_GATES",
  "Approval",
  "Decision",
  "GuardContext",
  "KillSwitch",
  "evaluate",
  "halt_file_present",
  "kill_reason",
]
