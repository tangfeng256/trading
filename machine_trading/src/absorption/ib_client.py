from __future__ import annotations

from dataclasses import dataclass

from .config import IBConfig


@dataclass
class IBClient:
    config: IBConfig
    paper: bool = True

    def connect(self):
        try:
            from ib_insync import IB
        except ImportError as exc:
            raise RuntimeError("Install ib_insync for live/paper IBKR connectivity") from exc
        self.ib = IB()
        self.ib.connect(
            self.config.host,
            self.config.port,
            clientId=self.config.client_id,
            timeout=self.config.connect_timeout_sec,
        )
        return self.ib

    def disconnect(self) -> None:
        if hasattr(self, "ib") and self.ib.isConnected():
            self.ib.disconnect()
