"""Shared verification/invocation/cancellation types for hosted and local targets."""

from __future__ import annotations

from typing import Protocol

from ..contracts import CancellationResult, InvocationResult, VerificationRecord

__all__ = ["CancellationResult", "InvocationResult", "TargetAdapter", "VerificationRecord"]


class TargetAdapter(Protocol):
    def verify(self, target_version, smoke_input, execution_context) -> VerificationRecord: ...
    def invoke(self, invocation_manifest, case_input, execution_context) -> InvocationResult: ...
    def cancel(self, invocation_id, execution_context) -> CancellationResult: ...
