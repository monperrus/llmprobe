"""Typed exceptions for llmprobe."""


class AgentProbeError(Exception):
    """Base class for all llmprobe exceptions."""


class AuthenticationError(AgentProbeError):
    """Raised when an API key or auth token cannot be obtained."""
