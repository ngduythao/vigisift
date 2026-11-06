import re
from dataclasses import dataclass
from enum import StrEnum

# Same rule as the CHECK in schema.sql.
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
MIN_PASSWORD_LENGTH = 12


class Role(StrEnum):
    OPERATOR = "operator"
    ADMIN = "admin"


@dataclass(frozen=True)
class Operator:
    id: int
    username: str
    display_name: str
    role: Role

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN


class Verdict(StrEnum):
    """What the operator says about the clip. The ground truth for evals, independent of the AI."""

    SUSPICIOUS = "suspicious"
    NORMAL = "normal"
    UNCLEAR = "unclear"
