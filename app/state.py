from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RecoveryState:
    is_active: bool = False
    stage: str = "idle"
    last_trigger: Optional[datetime] = None

    def set_triggered(self):
        self.is_active = True
        self.stage = "grace_period"
        self.last_trigger = datetime.now(timezone.utc)

    def set_stage(self, stage: str):
        self.stage = stage

    def clear(self):
        self.is_active = False
        self.stage = "idle"
