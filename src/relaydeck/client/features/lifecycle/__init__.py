"""Android lifecycle and connectivity domain. Flet-free.

Owns the app pause/resume/process-death model, the
pause-invalidates-writer gate, the
ordered resume flow, process-death and configuration-change
simulation, network-switch resynchronization, and the storage/privacy
policy. All logic here is testable without Flet and without a device;
the device matrix that needs real hardware stays explicitly blocked
(see `stores.FORCE_STOP_LIMITS` and `scripts/check_device_scenarios.py`).
"""

from relaydeck.client.features.lifecycle.connectivity import (
    LINK_EVENT_HISTORY,
    CursorDeduper,
    LinkEvent,
    LinkResync,
)
from relaydeck.client.features.lifecycle.machine import (
    AppPhase,
    ConfigChange,
    LifecycleError,
    LifecycleMachine,
)
from relaydeck.client.features.lifecycle.persist import (
    DRAFT_KEY_PREFIX,
    OUTAGE_MESSAGE,
    PausePersistReport,
    ResumeRestoreReport,
    draft_key,
    pause_persist,
    resume_restore,
)
from relaydeck.client.features.lifecycle.resume import (
    RESUME_ORDER,
    ResumeError,
    ResumeOrchestrator,
    ResumeReport,
    ResumeStep,
    restore_minimal_route,
)
from relaydeck.client.features.lifecycle.stores import (
    FORCE_STOP_LIMITS,
    ORDINARY_SAFE_KEYS,
    SECURE_ONLY_KEYS,
    STORAGE_POLICY,
    DeathReport,
    DurableStores,
    ProcessMemory,
    audit_stores,
    simulate_config_change,
    simulate_death,
)
from relaydeck.client.features.lifecycle.writer import (
    PAUSE_RULE,
    StaleWriterError,
    WriterGate,
)

__all__ = [
    "DRAFT_KEY_PREFIX",
    "FORCE_STOP_LIMITS",
    "LINK_EVENT_HISTORY",
    "OUTAGE_MESSAGE",
    "ORDINARY_SAFE_KEYS",
    "PAUSE_RULE",
    "RESUME_ORDER",
    "SECURE_ONLY_KEYS",
    "STORAGE_POLICY",
    "AppPhase",
    "ConfigChange",
    "CursorDeduper",
    "DeathReport",
    "DurableStores",
    "LifecycleError",
    "LifecycleMachine",
    "LinkEvent",
    "LinkResync",
    "PausePersistReport",
    "ProcessMemory",
    "ResumeError",
    "ResumeOrchestrator",
    "ResumeReport",
    "ResumeRestoreReport",
    "ResumeStep",
    "restore_minimal_route",
    "StaleWriterError",
    "WriterGate",
    "audit_stores",
    "draft_key",
    "pause_persist",
    "resume_restore",
    "simulate_config_change",
    "simulate_death",
]
