"""快照文件的体检：存在吗？进了版本库吗？内容是正文吗？

为什么单独成模块
----------------
A 级参数的凭据是「可打开的 URL + 快照」。但**"snapshot_ref 这个字符串在"不等于"证据在"**：
之前 .gitignore 忽略了整个 data/，32 个 A 级参数里有 22 个的快照其实没进仓库，
而 INV-003 只查引用字符串，一路绿灯——clone 出来证据链是断的。

第二层是内容：2026-10-06 泰国税务那篇的来源被 WAF 拦了，拦截页（196 字节的
「您的请求可能存在威胁，已被拦截！」）被 `save_snapshot` 原样存成快照。
文件名、哈希、git 跟踪全都正常——**"文件在且进了版本库"不等于"里面是正文"**。

所以把"这个快照到底能不能作为证据"做成一个可复查的判断，让 validate 与测试都能用。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 拦截页（WAF / 风控）的标记。**只写实测见过的短语，不写「拦截」「威胁」这种泛词**：
# 实测 ARTICLE-26569 正文有「卖家包装不规范拦截标准示例」、ARTICLE-25237 有
# 「威胁航空器安全飞行的因素」、ARTICLE-28203 有「包裹会被拦截处理」——泛词会把
# 真文章判成拦截页。**误报比漏报更糟**（HANDOVER.md 坑 #7）：检查器一旦误报，
# 人会直接把它关掉。
BLOCK_PAGE_MARKERS = (
    "请求可能存在威胁",   # WAF 标题句（前半）
    "已被拦截",           # WAF 标题句（后半）
    "拦截时间",           # WAF 页脚：发生时刻
    "EventID",            # WAF 页脚：事故编号
)
# 两道闸门同时成立才算拦截页：**命中 ≥2 个标记** 且 **正文很短**。
# 单看"已被拦截"会误伤以后讲风控的文章；拦截页实测只有 196 字节，
# 给到 600 字已足够宽松（讲风控的文章动辄几千字）。
BLOCK_PAGE_MIN_MARKERS = 2
BLOCK_PAGE_MAX_LENGTH = 600


def looks_like_block_page(text: str | None) -> bool:
    """这段文本是不是 WAF/风控的拦截页，而不是正文。

    ``None`` / 空串 / 纯空白一律返回 False——**"判不出来"不等于"是拦截页"**，
    跟 ``is_tracked`` 返回 None 的用法同一个道理。
    """
    if not text:
        return False
    body = text.strip()
    if not body or len(body) > BLOCK_PAGE_MAX_LENGTH:
        return False
    hits = sum(1 for marker in BLOCK_PAGE_MARKERS if marker in body)
    return hits >= BLOCK_PAGE_MIN_MARKERS


def _is_block_page_file(path: Path) -> bool:
    """文件内容是不是拦截页。读不出来（权限/编码）返回 False：判不出来不能当命中。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return looks_like_block_page(text)


@dataclass
class SnapshotHealth:
    ref: str
    exists: bool
    tracked: bool | None      # None = 判不出来（没有 git 或不在此仓库内）
    detail: str = ""
    param_id: str = ""        # 报告里不说是哪个参数等于没说
    block_page: bool = False  # 文件在、也进了库，但内容是拦截页

    @property
    def usable(self) -> bool:
        """能不能作为证据：文件在，随版本库走，**且内容是正文而不是拦截页**。"""
        return self.exists and self.tracked is not False and not self.block_page


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
    if _is_block_page_file(path):
        # 内容闸门先于版本库闸门报：拦截页就算进了库也不是证据，
        # "已进版本库"在这里反而是更坏的消息（假证据被 clone 出去了）。
        detail = "内容是**拦截页**（WAF/风控），不是正文——不能当证据"
        if tracked is True:
            detail += "；而且已进版本库，clone 出来就是假证据"
        elif tracked is False:
            detail += "；也没进版本库"
        return SnapshotHealth(ref or "", exists=True, tracked=tracked, detail=detail,
                              block_page=True)
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
