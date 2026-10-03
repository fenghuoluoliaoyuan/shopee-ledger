"""快照文件的体检：存在吗？进了版本库吗？

为什么单独成模块
----------------
A 级参数的凭据是「可打开的 URL + 快照」。但**"snapshot_ref 这个字符串在"不等于"证据在"**：
之前 .gitignore 忽略了整个 data/，32 个 A 级参数里有 22 个的快照其实没进仓库，
而 INV-003 只查引用字符串，一路绿灯——clone 出来证据链是断的。

所以把"这个快照到底能不能作为证据"做成一个可复查的判断，让 validate 与测试都能用。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class SnapshotHealth:
    ref: str
    exists: bool
    tracked: bool | None      # None = 判不出来（没有 git 或不在此仓库内）
    detail: str = ""
    param_id: str = ""        # 报告里不说是哪个参数等于没说

    @property
    def usable(self) -> bool:
        """能不能作为证据：文件在，且随版本库走。"""
        return self.exists and self.tracked is not False


def resolve(ref: str | None, root: Path | None = None) -> Path | None:
    if not ref:
        return None
    path = Path(ref.replace("\\", "/"))
    if not path.is_absolute():
        path = (root or ROOT) / path
    return path


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                              timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None


def is_tracked(path: Path, root: Path | None = None) -> bool | None:
    """文件是否进了版本库。

    返回 None 表示判不出来（没有 git、不是仓库、或文件在仓库外）——
    **不能把"判不出来"当成"没进"**，那会在没有 git 的环境里制造假警报。
    """
    base = root or ROOT
    try:
        relative = path.resolve().relative_to(Path(base).resolve())
    except ValueError:
        return None
    result = _git(["ls-files", "--error-unmatch", "--", str(relative)], base)
    if result is None:
        return None
    if result.returncode == 0:
        return True
    ignored = _git(["check-ignore", "-q", "--", str(relative)], base)
    if ignored is None:
        return None
    if ignored.returncode == 0:
        return False
    # 既没被跟踪也没被忽略：说明是还没 git add 的新文件
    return False


def check(ref: str | None, root: Path | None = None) -> SnapshotHealth:
    path = resolve(ref, root)
    if path is None:
        return SnapshotHealth(ref or "", exists=False, tracked=None, detail="没有 snapshot_ref")
    if not path.exists():
        return SnapshotHealth(ref or "", exists=False, tracked=None,
                              detail="文件不存在：%s" % path)
    tracked = is_tracked(path, root)
    if tracked is True:
        return SnapshotHealth(ref or "", exists=True, tracked=True, detail="已进版本库")
    if tracked is False:
        return SnapshotHealth(ref or "", exists=True, tracked=False,
                              detail="文件在，但**没进版本库**——clone 出来就断了")
    return SnapshotHealth(ref or "", exists=True, tracked=None, detail="判不出来是否进了版本库")


def audit(params: dict, root: Path | None = None) -> list[SnapshotHealth]:
    """把 A 级参数的快照逐个查一遍。"""
    out: list[SnapshotHealth] = []
    for param in params.values():
        if getattr(param, "evidence_level", None) != "A":
            continue
        ref = (getattr(param, "source", None) or {}).get("snapshot_ref")
        health = check(ref, root)
        health.param_id = getattr(param, "id", "") or ""
        out.append(health)
    return out
