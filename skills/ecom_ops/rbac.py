"""Role-based access control for Azom agent (Jonatan viewer, Oscar full_admin)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

from ecom_ops.config import RbacConfig, load_app_config


class Permission(str, Enum):
    READ = "read"
    ORDER_STATUS_UPDATE = "order_status_update"
    PRODUCT_DESC_WRITE = "product_desc_write"
    SUPPORT_REPLY = "support_reply"
    MAIL_READ = "mail_read"
    MAIL_SEND = "mail_send"
    SSH_READ = "ssh_read"
    SSH_WRITE = "ssh_write"
    CODE_EDIT = "code_edit"
    MANAGE = "manage"
    ADMIN = "admin"


ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": frozenset(
        {Permission.READ, Permission.SSH_READ, Permission.MAIL_READ}
    ),
    "read_only": frozenset(
        {Permission.READ, Permission.SSH_READ, Permission.MAIL_READ}
    ),
    "operator": frozenset(
        {
            Permission.READ,
            Permission.ORDER_STATUS_UPDATE,
            Permission.PRODUCT_DESC_WRITE,
            Permission.SUPPORT_REPLY,
            Permission.MAIL_READ,
            Permission.MAIL_SEND,
            Permission.SSH_READ,
        }
    ),
    "full_admin": frozenset(set(Permission)),
}


class AccessDenied(PermissionError):
    """Raised when the actor lacks required permission."""


@dataclass(frozen=True)
class Actor:
    name: str
    role: str

    def has(self, permission: Permission) -> bool:
        perms = ROLE_PERMISSIONS.get(self.role, frozenset())
        return permission in perms or Permission.ADMIN in perms


@lru_cache(maxsize=1)
def _rbac_config() -> RbacConfig:
    return load_app_config().rbac


def resolve_actor(name: str | None = None) -> Actor:
    """Resolve actor from name; default agent operator role for automation."""
    cfg = _rbac_config()
    actor_name = (name or "agent").strip().lower()
    if actor_name in cfg.roles:
        return Actor(name=actor_name, role=cfg.roles[actor_name])
    if actor_name == "agent":
        # Automated ecom-ops runs as operator (not full_admin)
        return Actor(name="agent", role="operator")
    raise AccessDenied(f"Unknown actor: {actor_name}")


def require_permission(actor: Actor, permission: Permission) -> None:
    if not actor.has(permission):
        raise AccessDenied(
            f"Actor {actor.name!r} (role={actor.role}) lacks permission {permission.value}"
        )


def clear_rbac_cache() -> None:
    _rbac_config.cache_clear()
