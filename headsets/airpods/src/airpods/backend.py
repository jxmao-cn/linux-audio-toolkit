"""Backend interface implemented by the real BlueZ and mock backends."""

from __future__ import annotations

from typing import Callable, Optional, Protocol, runtime_checkable

from .models import Adapter, BatterySnapshot, Device


@runtime_checkable
class Backend(Protocol):
    """The small surface the CLI and daemon need from a Bluetooth backend."""

    def adapter(self) -> Adapter:
        ...

    def devices(self) -> list[Device]:
        ...

    def device(self, address: str) -> Optional[Device]:
        ...

    def start_discovery(self, transport: str = "auto") -> None:
        ...

    def stop_discovery(self) -> None:
        ...

    def is_discovering(self) -> bool:
        ...

    def pair(self, address: str) -> None:
        ...

    def connect(self, address: str) -> None:
        ...

    def wait_connected(self, address: str, timeout: float = 20.0) -> bool:
        ...

    def disconnect(self, address: str) -> None:
        ...

    def set_trusted(self, address: str, trusted: bool = True) -> None:
        ...

    def remove(self, address: str) -> None:
        ...

    def battery(self, address: str) -> BatterySnapshot:
        ...

    def register_agent(self, confirm: Callable[[str], bool]) -> None:
        ...

    def close(self) -> None:
        ...
