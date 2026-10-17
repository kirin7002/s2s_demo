"""Future integration contracts only; no storage, tools, or outbound calls here.

Identity/consent and tool authorization must be designed before wiring these ports.
The running gateway does not instantiate or invoke them.
"""
from typing import Protocol


class MemoryHub(Protocol):
    async def context(self, subject_id: str, session_id: str) -> str: ...
    async def accept_event(self, subject_id: str, event: dict) -> None: ...


class ToolExecutor(Protocol):
    async def execute(self, subject_id: str, name: str, arguments: dict) -> dict: ...
