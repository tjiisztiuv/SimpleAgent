"""空间（Space）模块：客户端持久化层的 W1 实现。

导入入口：from simpleagent.spaces import SpaceStore, Space, SpaceSpec, SessionMeta, Verification
"""

from simpleagent.spaces.models import (
    AgentBinding,
    GenericConfig,
    SessionMeta,
    Space,
    SpaceSpec,
    Verification,
    VerifyConfig,
    locked_reason,
    validate_executor,
)
from simpleagent.spaces.store import SpaceStore

__all__ = [
    "AgentBinding",
    "GenericConfig",
    "SessionMeta",
    "Space",
    "SpaceSpec",
    "SpaceStore",
    "Verification",
    "VerifyConfig",
    "locked_reason",
    "validate_executor",
]
