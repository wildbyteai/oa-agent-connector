"""Agent-side OA connector that uses the existing OA web endpoints."""

from .client import OAClient, OAConnectorError, PermissionGateError

__all__ = ["OAClient", "OAConnectorError", "PermissionGateError"]
