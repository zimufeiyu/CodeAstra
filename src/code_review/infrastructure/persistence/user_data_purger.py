from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QuarantineMove:
    source: Path
    staged: Path


@dataclass(frozen=True)
class QuarantineTicket:
    directory: Path
    moves: tuple[QuarantineMove, ...]


class UserDataPurger:
    def __init__(self, owned_roots: list[Path], quarantine_root: Path) -> None:
        self._owned_roots = [path.resolve() for path in owned_roots]
        self._quarantine_root = quarantine_root.resolve()

    def stage(self, user_id: str) -> QuarantineTicket:
        if not user_id or user_id in {".", ".."} or any(c in user_id for c in "/\\"):
            raise ValueError("invalid user id")
        ticket_dir = self._quarantine_root / uuid.uuid4().hex
        moves: list[QuarantineMove] = []
        try:
            for index, root in enumerate(self._owned_roots):
                source = (root / user_id).resolve()
                if source.parent != root:
                    raise ValueError("user path escapes an owned root")
                if not source.exists():
                    continue
                staged = ticket_dir / str(index)
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(staged))
                moves.append(QuarantineMove(source=source, staged=staged))
        except Exception:
            self.restore(QuarantineTicket(ticket_dir, tuple(moves)))
            raise
        return QuarantineTicket(ticket_dir, tuple(moves))

    def restore(self, ticket: QuarantineTicket) -> None:
        for move in reversed(ticket.moves):
            if move.staged.exists():
                move.source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(move.staged), str(move.source))
        if ticket.directory.exists():
            shutil.rmtree(ticket.directory)
        self._remove_empty_quarantine_root()

    def finalize(self, ticket: QuarantineTicket) -> None:
        if ticket.directory.exists():
            shutil.rmtree(ticket.directory)
        self._remove_empty_quarantine_root()

    def _remove_empty_quarantine_root(self) -> None:
        if self._quarantine_root.exists() and not any(self._quarantine_root.iterdir()):
            self._quarantine_root.rmdir()
