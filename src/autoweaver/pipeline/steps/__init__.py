from .base import ProcessStep
from .tiling import TileStep, MergeTilesStep
from .yolo_detect import YOLODetectStep
from .postprocess import NMSStep, FilterStep, SortStep

# Step registry for config-based creation
# All implemented steps should be registered here
_STEP_REGISTRY = {
    # Tiling steps (for sliced inference on large images)
    "tile": TileStep,
    "merge_tiles": MergeTilesStep,
    # Detection steps
    "yolo": YOLODetectStep,
    # Postprocessing steps
    "nms": NMSStep,
    "filter": FilterStep,
    "sort": SortStep,
}


def create_step(config: dict) -> ProcessStep:
    """Create a processing step from configuration.
    
    Args:
        config: Step configuration with 'type' and optional 'params'.
            - type: Step type name (must be in registry)
            - name: Optional custom name for logging (defaults to type)
            - params: Step-specific parameters
        
    Returns:
        ProcessStep instance.
        
    Raises:
        ValueError: If step type is unknown.
        
    Example:
        >>> config = {"type": "resize", "name": "my_resize", "params": {"size": [640, 640]}}
        >>> step = create_step(config)
    """
    step_type = config.get("type")
    if step_type not in _STEP_REGISTRY:
        available = ", ".join(sorted(_STEP_REGISTRY.keys()))
        raise ValueError(
            f"Unknown step type: '{step_type}'. "
            f"Available types: {available}"
        )
    
    step_class = _STEP_REGISTRY[step_type]
    params = config.get("params", {})
    
    # Support custom name via params
    custom_name = config.get("name")
    if custom_name:
        params = params.copy()
        params["_custom_name"] = custom_name
    
    return step_class(params)


def register_step(name: str, step_class: type):
    """Register a custom step type.
    
    Args:
        name: Step type name for configuration.
        step_class: Step class (must inherit from ProcessStep).
    """
    if not issubclass(step_class, ProcessStep):
        raise TypeError(f"Step class must inherit from ProcessStep: {step_class}")
    _STEP_REGISTRY[name] = step_class


def list_available_steps() -> list:
    """List all available step types."""
    return sorted(_STEP_REGISTRY.keys())


__all__ = [
    # Base
    "ProcessStep",
    # Tiling (sliced inference)
    "TileStep",
    "MergeTilesStep",
    # Detection
    "YOLODetectStep",
    # Postprocessing
    "NMSStep",
    "FilterStep",
    "SortStep",
    # Factory functions
    "create_step",
    "register_step",
    "list_available_steps",
]

