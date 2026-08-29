from dataclasses import dataclass
from typing import Literal

GenerationMode = Literal[
    "success",
    "delayed",
    "failure",
    "timeout",
    "duplicate",
    "corrupt",
]

_ALLOWED_MODES = {
    "success",
    "delayed",
    "failure",
    "timeout",
    "duplicate",
    "corrupt",
}


@dataclass(frozen=True, slots=True)
class InternalGenerationOptions:
    """Non-HTTP controls supplied by trusted runtime wiring or test overrides."""

    modes: tuple[GenerationMode, ...] = ()

    def __post_init__(self) -> None:
        invalid = set(self.modes) - _ALLOWED_MODES
        if invalid:
            raise ValueError(f"unsupported internal generation modes: {sorted(invalid)}")

    def mode_for(self, index: int) -> GenerationMode:
        return self.modes[index] if index < len(self.modes) else "success"


def get_internal_generation_options() -> InternalGenerationOptions:
    return InternalGenerationOptions()
