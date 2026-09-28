"""Exceptions raised by MetaOri. The MCP layer reports them as tool errors."""


class MetaOriError(Exception):
    """Base class for every MetaOri failure."""


class ComfyUnavailable(MetaOriError):
    """ComfyUI could not be reached."""


class ComfyRequestError(MetaOriError):
    """ComfyUI answered with an error, such as a workflow validation failure."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


class WorkflowError(MetaOriError):
    """A workflow pack is invalid, missing, or cannot serve a request."""


class ParamError(MetaOriError):
    """A tool argument is invalid."""


class JobNotFound(MetaOriError):
    """No job record matches the given id."""


class BlenderScriptError(MetaOriError):
    """A MetaOri bpy script failed inside Blender."""
