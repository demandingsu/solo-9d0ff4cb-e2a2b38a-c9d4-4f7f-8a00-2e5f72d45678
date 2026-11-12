"""业务合并逻辑：按发布时间合并原公告与更正公告。

规则（对应需求）：
* 按发布时间从早到晚依次应用公告；
* 更正公告未提及截止时间（或仅重述旧时间并声明未改期）时，沿用此前已经生效的截止时间；
* 更正公告给出明确“原时间→现时间”改期表述时，解析器只取现行时间，合并器以其覆盖；
* 同一公告给出相互矛盾的现行截止时间 → 整体标记“待核实”并清空有效期限，
  不按页面先后猜测；
* 出现取消采购信息后，项目不可投标；
* 任一关联详情页抓取/解析失败 → 整体标记“待核实”，且不返回任何截止时间，
  避免把旧时间当作最终期限；
* 发布时间缺失的公告无法参与时间合并，同样按待核实处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.models import NoticeKind, NoticeRecord, ProjectStatus


@dataclass
class MergeInput:
    """合并器输入：成功解析的公告 + 失败的关联页面。"""

    notices: list[NoticeRecord]
    failures: list[str] = field(default_factory=list)  # 抓取/解析失败的详情页 URL


@dataclass
class MergeResult:
    status: ProjectStatus
    can_bid: bool
    effective_deadline: datetime | None
    cancelled: bool
    ordered_notices: list[NoticeRecord]
    warnings: list[str]


def _sort_key(notice: NoticeRecord) -> datetime:
    # 调用方已保证 published_at 非空
    return notice.published_at or datetime.min


def merge_notices(data: MergeInput) -> MergeResult:
    warnings = list(data.failures and [f"关联详情页抓取失败：{u}" for u in data.failures])

    # 发布时间缺失 → 无法保证合并顺序，按待核实处理
    undated = [n for n in data.notices if n.published_at is None]
    if undated:
        warnings.extend(f"公告缺少可解析的发布时间：{n.url}" for n in undated)

    if data.failures or undated:
        return MergeResult(
            status=ProjectStatus.PENDING,
            can_bid=False,
            effective_deadline=None,  # 关键：失败时绝不返回旧时间
            cancelled=any(n.cancelled for n in data.notices),
            ordered_notices=sorted(data.notices, key=_sort_key),
            warnings=warnings,
        )

    ordered = sorted(data.notices, key=_sort_key)
    if not ordered:
        return MergeResult(
            status=ProjectStatus.NOT_FOUND,
            can_bid=False,
            effective_deadline=None,
            cancelled=False,
            ordered_notices=[],
            warnings=warnings,
        )

    # 同一公告内出现相互矛盾的现行截止时间 → 待核实且清空有效期限，
    # 绝不按页面先后猜测其中一个
    conflicting = [n for n in ordered if n.deadline_conflict]
    if conflicting:
        for notice in conflicting:
            candidates = "、".join(dt.strftime("%Y-%m-%d %H:%M") for dt in notice.deadline_candidates)
            warnings.append(
                f"公告存在相互矛盾的现行投标截止时间（{candidates or '候选时间缺失'}），"
                f"需人工核实：{notice.url}"
            )
        return MergeResult(
            status=ProjectStatus.PENDING,
            can_bid=False,
            effective_deadline=None,
            cancelled=any(n.cancelled for n in ordered),
            ordered_notices=ordered,
            warnings=warnings,
        )

    originals = [n for n in ordered if n.kind == NoticeKind.ORIGINAL]
    if not originals:
        warnings.append("未识别到原公告，仅有更正类公告，信息链不完整")
        return MergeResult(
            status=ProjectStatus.PENDING,
            can_bid=False,
            effective_deadline=None,
            cancelled=False,
            ordered_notices=ordered,
            warnings=warnings,
        )

    # 逐条应用：解析器已保证 deadline 只承载现行时间——
    # 明确改期取“现时间”覆盖旧期限；未改期的旧日期重述 deadline=None，自然沿用已生效时间
    current_deadline: datetime | None = None
    cancelled = False
    cancel_reason: str | None = None

    for notice in ordered:
        if notice.deadline is not None:
            current_deadline = notice.deadline
        if notice.cancelled:
            cancelled = True
            cancel_reason = notice.cancellation_reason or cancel_reason

    # 原公告本身就没有截止时间，且没有任何更正补充 → 无法给出有效期限
    if current_deadline is None:
        warnings.append("原公告及全部更正均未给出投标截止时间")
        return MergeResult(
            status=ProjectStatus.PENDING,
            can_bid=False,
            effective_deadline=None,
            cancelled=cancelled,
            ordered_notices=ordered,
            warnings=warnings,
        )

    if cancelled:
        if cancel_reason:
            warnings.append(f"采购已取消：{cancel_reason}")
        else:
            warnings.append("采购已取消，不可投标")
        return MergeResult(
            status=ProjectStatus.CLOSED,
            can_bid=False,
            # 取消后不可投标：不提供“有效截止时间”，避免调用方误用旧时间安排投标；
            # 最后已知时间仍保留在 notices 明细中可供追溯。
            effective_deadline=None,
            cancelled=True,
            ordered_notices=ordered,
            warnings=warnings,
        )

    return MergeResult(
        status=ProjectStatus.OPEN,
        can_bid=True,
        effective_deadline=current_deadline,
        cancelled=False,
        ordered_notices=ordered,
        warnings=warnings,
    )
