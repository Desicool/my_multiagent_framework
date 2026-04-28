# PyInstaller binary symlink rot after worktree changes

**Resolved:** 2026-04-27 · **Source:** `bd remember beidou-dev-finish-rebuild-install`

## Problem

After a development cycle, `beidou --help` ran an out-of-date binary even after a fresh `pyinstaller --noconfirm beidou.spec`. The rebuilt artifact under `dist/beidou` was correct, but `~/.local/bin/beidou` resolved through a symlink that pointed at an **old worktree path** (e.g. `/home/desico/code/workspace/.../dist/beidou`), not the canonical project at `/home/desico/code/my_simple_agent/dist/beidou`.

## Root cause

`~/.local/bin/beidou` is a symlink. The symlink target is whatever path was in use when the install happened. If the install ran from a transient git worktree under `~/code/workspace/...` (typical when reviewing a branch), the symlink keeps pointing there even after the worktree is removed or stale.

## Fix

Operational, not code:

```bash
source .venv/bin/activate
pyinstaller --noconfirm beidou.spec
ls -la ~/.local/bin/beidou      # verify target
ln -sf /home/desico/code/my_simple_agent/dist/beidou ~/.local/bin/beidou
beidou --help                    # verify
```

The canonical project path is `/home/desico/code/my_simple_agent`. If the symlink ever points elsewhere, repoint it.

## Decision / lesson

- **Symlinks are sticky to the path that created them.** Any "install" step that creates a symlink from a transient working directory will rot when that directory moves or disappears.
- **Verify the symlink target after every install**, not just that the install command succeeded.
- For team installs, prefer `pip install -e .` from the canonical path or a wrapper script that resolves the canonical path at runtime, rather than a symlink to a build artifact.

## References

- Memory: `bd memories beidou-dev-finish-rebuild-install`.
- Build spec: `beidou.spec`.
