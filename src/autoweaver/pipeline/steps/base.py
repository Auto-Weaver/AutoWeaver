"""Base class for pipeline processing steps."""

from abc import ABC, abstractmethod
from typing import Any, Dict

from ..types import PipelineContext


class ProcessStep(ABC):
    """Abstract base class for pipeline processing steps.
    
    Each step receives a PipelineContext, performs some processing,
    and returns the updated context. Steps can:
    - Modify the processed_image
    - Add detections to the detections list
    - Add metadata to the metadata dict
    
    Example:
        >>> class MyStep(ProcessStep):
        ...     def process(self, ctx: PipelineContext) -> PipelineContext:
        ...         # Do something with ctx.processed_image
        ...         ctx.metadata["my_value"] = 42
        ...         return ctx
    """

    def __init__(self, params: Dict[str, Any] = None):
        """Initialize the processing step.
        
        Args:
            params: Step-specific parameters.
                Special key '_custom_name' can override the step name.
        """
        self._params = params or {}
        # Extract custom name if provided
        self._custom_name = self._params.pop("_custom_name", None)

    @property
    def name(self) -> str:
        """Step name for logging and identification.
        
        Returns custom name if set via config, otherwise class name.
        """
        if self._custom_name:
            return self._custom_name
        return self.__class__.__name__

    @property
    def params(self) -> Dict[str, Any]:
        """Step parameters."""
        return self._params

    @abstractmethod
    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Process the context and return updated context.
        
        Args:
            ctx: Pipeline context with current state.
            
        Returns:
            Updated context (usually the same object, modified).
        """
        pass

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(params={self._params})"
