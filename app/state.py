from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RecoveryState:
    is_active: bool = False
    stage: str = "idle"
    last_trigger: Optional[datetime] = None
    last_recovery_end: Optional[datetime] = None

    def set_triggered(self):
        self.is_active = True
        self.stage = "grace_period"
        self.last_trigger = datetime.now(timezone.utc)

    def set_stage(self, stage: str):
        self.stage = stage

    def clear(self):
        self.is_active = False
        self.stage = "idle"
        self.last_recovery_end = datetime.now(timezone.utc)

    def in_cooldown(self, cooldown_minutes: int) -> bool:
        if self.last_recovery_end is None:
            return False
        elapsed = (datetime.now(timezone.utc) - self.last_recovery_end).total_seconds()
        return elapsed < cooldown_minutes * 60
