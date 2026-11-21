"""Model routing for Azom: ChatGPT OAuth, xAI, and OpenRouter."""

from ecom_ops.model_router.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelRateLimitError,
    ModelRouteError,
    ModelUnavailableError,
)
from ecom_ops.model_router.router import ModelRouter, get_model_router, reset_model_router

__all__ = [
    "ModelAuthError",
    "ModelConfigError",
    "ModelRateLimitError",
    "ModelRouteError",
    "ModelRouter",
    "ModelUnavailableError",
    "get_model_router",
    "reset_model_router",
]
