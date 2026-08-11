from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from compliance_review.domain.models import ApplicabilityProfile, ControlSet

ModelT = TypeVar("ModelT", bound=BaseModel)


class ConfigLoadError(ValueError):
    """Raised when a configuration file cannot be loaded or validated."""


def load_yaml_model(path: Path, model_type: type[ModelT]) -> ModelT:
    if not path.is_file():
        raise ConfigLoadError(f"configuration file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigLoadError(f"cannot read YAML {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigLoadError(f"configuration root must be an object: {path}")

    try:
        return model_type.model_validate(raw)
    except ValidationError as exc:
        raise ConfigLoadError(f"invalid {model_type.__name__} in {path}: {exc}") from exc


def load_profile(path: Path) -> ApplicabilityProfile:
    return load_yaml_model(path, ApplicabilityProfile)


def load_controls(path: Path) -> ControlSet:
    return load_yaml_model(path, ControlSet)
