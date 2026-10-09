"""Workspace file tools for the agent: ws_read and ws_write, jailed to ./workspace."""

from pathlib import Path

MAX_READ = 64 * 1024


class WsError(Exception):
    pass


def jail(root: Path, rel: str) -> Path:
    """Resolve rel inside root. Refuse absolute paths, '..' escapes and symlinks that leave root."""
    if not isinstance(rel, str) or not rel.strip():
        raise WsError("path is empty")
    if rel.startswith(("/", "\\", "~")) or Path(rel).is_absolute():
        raise WsError("absolute paths are not allowed; use a path relative to workspace/")
    rel = rel.removeprefix("workspace/")
    base = root.resolve()
    p = (base / rel).resolve()
    if not p.is_relative_to(base):
        raise WsError("path escapes workspace/")
    return p


def ws_read(root: Path, path: str) -> str:
    p = jail(root, path)
    if p.is_dir():
        items = sorted(p.iterdir())[:200]
        return (
            "\n".join(f"{c.name}/" if c.is_dir() else f"{c.name}  {c.stat().st_size} B" for c in items)
            or "(empty)"
        )
    if not p.exists():
        raise WsError(f"no such file: workspace/{p.relative_to(root.resolve())}")
    with p.open("rb") as f:
        data = f.read(MAX_READ + 1)
    text = data[:MAX_READ].decode("utf-8", errors="replace")
    if len(data) > MAX_READ:
        text += f"\n...[truncated at 64 KB of {p.stat().st_size} B]"
    return text


def ws_write(root: Path, path: str, content: str) -> dict:
    p = jail(root, path)
    if p == root.resolve() or p.is_dir():
        raise WsError("path is a directory")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content if isinstance(content, str) else str(content), encoding="utf-8")
    return {"ok": True, "path": f"workspace/{p.relative_to(root.resolve())}"}
