"""Use approvals: "always allow" persisted by permission hash, "allow once" in memory."""

import json
import os
import time
from pathlib import Path


class Approvals:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.once: set[str] = set()
        try:
            self.always: dict[str, dict] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.always = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.always, indent=1, sort_keys=True))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def is_always(self, perm_hash: str) -> bool:
        return bool(perm_hash) and perm_hash in self.always

    def allow_always(self, perm_hash: str, name: str, version: int | None) -> None:
        self.always[perm_hash] = {
            "name": name,
            "version": version,
            "granted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._save()

    def allow_once(self, perm_hash: str) -> None:
        self.once.add(perm_hash)

    def take(self, perm_hash: str) -> bool:
        """True if a run is approved: always-allowed, or one pending allow-once (consumed)."""
        if self.is_always(perm_hash):
            return True
        if perm_hash in self.once:
            self.once.discard(perm_hash)
            return True
        return False

    def entries(self) -> list[dict]:
        return [
            {"perm_hash": h, **v}
            for h, v in sorted(self.always.items(), key=lambda kv: kv[1].get("name", ""))
        ]

    def revoke(self, key: str) -> list[dict]:
        """Revoke by tool name or by a perm-hash prefix (>= 6 chars). Returns the removed entries."""
        hits = [
            h for h, v in self.always.items() if v.get("name") == key or (len(key) >= 6 and h.startswith(key))
        ]
        removed = [{"perm_hash": h, **self.always.pop(h)} for h in hits]
        if removed:
            self._save()
        return removed
