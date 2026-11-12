"""解析器与合并器的单元测试（不依赖网络）。"""

from datetime import datetime

import pytest

from app.merger import MergeInput, merge_notices
from app.models import NoticeKind, NoticeRecord, ProjectStatus
from app.parser import (
    extract_detail_links,
    parse_datetime,
    parse_detail,
    same_site,
)


# --------------------------------------------------------------------------- #
# parse_datetime
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-11-10 09:30", datetime(2026, 11, 10, 9, 30)),
        ("2026/11/10 09:30:00", datetime(2026, 11, 10, 9, 30, 0)),
        ("2026年11月15日 14时00分", datetime(2026, 11, 15, 14, 0)),
        ("发布时间：2026年11月03日", datetime(2026, 11, 3)),
        ("截止 2026-11-18T10:00", datetime(2026, 11, 18, 10, 0)),
        ("无时间文本", None),
    ],
)
def test_parse_datetime(text, expected):
    assert parse_datetime(text) == expected


def test_same_site():
    base = "http://localhost:8080/list.html"
    assert same_site("http://localhost:8080/detail/a.html", base)
    assert not same_site("https://example.gov/x.html", base)
    assert not same_site("http://localhost:9000/x.html", base)
    assert not same_site("/relative/x.html", base)


# --------------------------------------------------------------------------- #
# 列表页链接发现
# --------------------------------------------------------------------------- #
def test_extract_detail_links_filters_external_and_nav():
    html = """
    <html><body>
      <a href="/login.html">登录</a>
      <a href="https://example.gov/other.html">外站</a>
      <a href="/detail/a.html">公告A</a>
      <a href="detail/b.html?x=1">公告B</a>
      <a href="/detail/a.html">公告A重复</a>
      <a href="http://localhost:8080/list.html">列表自身</a>
    </body></html>
    """
    links = extract_detail_links(html, "http://localhost:8080/list.html")
    urls = [u for u, _, _ in links]
    assert urls == [
        "http://localhost:8080/detail/a.html",
        "http://localhost:8080/detail/b.html?x=1",
    ]
    assert {u: text for u, text, _ in links}["http://localhost:8080/detail/a.html"] == "公告A"


# --------------------------------------------------------------------------- #
# 详情页解析：三种页面形态
# --------------------------------------------------------------------------- #
LABEL_HTML = """
<html><head><title>测试项目公开招标公告</title></head><body>
<h1>测试项目公开招标公告</h1>
<table>
  <tr><th>项目编号</th><td>GSB-2026-001</td></tr>
  <tr><th>发布时间</th><td>2026-11-01 10:00</td></tr>
  <tr><th>投标截止时间</th><td>2026-11-10 09:30</td></tr>
</table>
</body></html>
"""

DATA_ATTR_HTML = """
<html><head><title>测试项目更正公告</title></head><body>
<h1>测试项目更正公告</h1>
<ul>
  <li data-field="project-code" data-value="GSB-2026-001"></li>
  <li data-field="published-at" data-value="2026-11-06 09:00"></li>
  <li data-field="deadline" data-value="2026-11-15 14:00"></li>
</ul>
</body></html>
"""

NARRATIVE_HTML = """
<html><head><title>测试项目竞争性磋商公告</title></head><body>
<h1>测试项目竞争性磋商公告</h1>
<p>项目编号：GSB-2026-007</p>
<p>发布时间：2026-11-05 08:45</p>
<p>供应商应在 2026年11月18日 10时00分 之前完成响应文件递交，逾期不再受理。</p>
</body></html>
"""

CANCEL_HTML = """
<html><head><title>测试项目取消公告</title></head><body>
<h1>测试项目取消公告</h1>
<ul>
  <li data-field="project-code" data-value="GSB-2026-002"></li>
  <li data-field="published-at" data-value="2026-11-04 11:00"></li>
  <li data-field="cancelled" data-value="true"></li>
  <li data-field="cancellation-reason" data-value="预算收回"></li>
</ul>
<p>因采购计划调整，本项目取消采购。</p>
</body></html>
"""


def test_parse_detail_label_layout():
    d = parse_detail(LABEL_HTML, "http://x/detail/a.html")
    assert d.project_code == "GSB-2026-001"
    assert d.published_at == datetime(2026, 11, 1, 10, 0)
    assert d.deadline == datetime(2026, 11, 10, 9, 30)
    assert d.cancelled is False
    assert d.kind_hint == "original"


def test_parse_detail_data_attr_layout():
    d = parse_detail(DATA_ATTR_HTML, "http://x/detail/b.html")
    assert d.published_at == datetime(2026, 11, 6, 9, 0)
    assert d.deadline == datetime(2026, 11, 15, 14, 0)
    assert d.kind_hint == "correction"


def test_parse_detail_narrative_fallback():
    d = parse_detail(NARRATIVE_HTML, "http://x/detail/c.html")
    assert d.project_code == "GSB-2026-007"
    assert d.published_at == datetime(2026, 11, 5, 8, 45)
    assert d.deadline == datetime(2026, 11, 18, 10, 0)
    assert d.kind_hint == "original"


def test_parse_detail_cancellation():
    d = parse_detail(CANCEL_HTML, "http://x/detail/d.html")
    assert d.cancelled is True
    assert d.cancellation_reason == "预算收回"
    assert d.kind_hint == "cancellation"


def test_parse_detail_requires_title():
    with pytest.raises(Exception):
        parse_detail("<html><body>无标题</body></html>", "http://x/e.html")


def test_deadline_fallback_ignores_qualification_history_dates():
    html = """
    <html><head><title>测试项目更正公告</title></head><body>
    <h1>测试项目更正公告</h1>
    <p>项目编号：GSB-2026-010</p>
    <p>发布时间：2026-11-03 15:30</p>
    <p>近三年（2023年1月1日至投标截止日）至少完成 2 项同类业绩。</p>
    <p>本更正仅澄清资格要求，未调整投标截止时间，按原招标文件执行。</p>
    </body></html>
    """
    d = parse_detail(html, "http://x/f.html")
    # 业绩说明里的 2023-01-01 不得被当成截止时间；本页确实未给出截止时间
    assert d.deadline is None


# --------------------------------------------------------------------------- #
# 改期表述：只取现行时间、未改期重述不产生期限、同页矛盾 → 冲突
# --------------------------------------------------------------------------- #
def test_label_field_explicit_old_to_new_takes_only_new_time():
    # 任务中的 bug 场景：标签字段写“由旧时间 调整为 新时间”，旧 API 取第一个日期
    html = """
    <html><head><title>测试项目更正公告</title></head><body>
    <h1>测试项目更正公告</h1>
    <ul>
      <li data-field="project-code" data-value="GSB-2026-001"></li>
      <li data-field="published-at" data-value="2026-11-08 09:30"></li>
      <li data-field="deadline"
          data-value="由 2026-11-12 09:00 调整为 2026-11-18 09:00">
        投标截止时间：由 2026-11-12 09:00 调整为 2026-11-18 09:00
      </li>
    </ul>
    </body></html>
    """
    d = parse_detail(html, "http://x/reschedule.html")
    assert d.deadline == datetime(2026, 11, 18, 9, 0)  # 现行时间，而非旧的 11-12
    assert d.deadline_conflict is False


def test_body_reschedule_sentence_ignores_leading_old_time():
    html = """
    <html><head><title>测试项目更正公告</title></head><body>
    <h1>测试项目更正公告</h1>
    <p>项目编号：GSB-2026-001</p>
    <p>发布时间：2026-11-08 09:30</p>
    <p>因安排调整，原投标截止时间为 2026-11-10 09:30，现延期至 2026年11月15日 14时00分，开标时间同步顺延。</p>
    </body></html>
    """
    d = parse_detail(html, "http://x/body-reschedule.html")
    assert d.deadline == datetime(2026, 11, 15, 14, 0)
    assert d.deadline_conflict is False


def test_no_change_correction_restating_old_date_yields_no_deadline():
    html = """
    <html><head><title>测试项目更正公告</title></head><body>
    <h1>测试项目更正公告</h1>
    <p>项目编号：GSB-2026-004</p>
    <p>发布时间：2026-11-05 14:00</p>
    <p>本更正公告未调整投标截止时间，投标截止时间仍按原招标文件 2026-11-12 09:00 执行，其他内容保持不变。</p>
    </body></html>
    """
    d = parse_detail(html, "http://x/no-change.html")
    assert d.deadline is None  # 旧日期只是重述，不得覆盖已生效期限
    assert d.deadline_conflict is False


def test_contradicting_current_deadlines_on_same_page_flag_conflict():
    html = """
    <html><head><title>测试项目更正公告</title></head><body>
    <h1>测试项目更正公告</h1>
    <ul>
      <li data-field="project-code" data-value="GSB-2026-005"></li>
      <li data-field="published-at" data-value="2026-11-09 16:00"></li>
      <li data-field="deadline" data-value="2026-11-20 09:30">投标截止时间：2026-11-20 09:30</li>
    </ul>
    <p>原投标截止时间为 2026-11-16 09:30，现延期至 2026年11月22日 10时00分，开标时间同步顺延。</p>
    </body></html>
    """
    d = parse_detail(html, "http://x/conflict.html")
    # 两个相互矛盾的“现行时间”：不按页面先后猜测，清空期限并标记冲突
    assert d.deadline is None
    assert d.deadline_conflict is True
    assert set(d.deadline_candidates) == {
        datetime(2026, 11, 20, 9, 30),
        datetime(2026, 11, 22, 10, 0),
    }
    assert any("矛盾" in w for w in d.warnings)


def test_two_identical_new_times_are_not_a_conflict():
    # data-field 与正文给出相同的现行时间（不同写法），属于重复声明而非矛盾
    html = """
    <html><head><title>测试项目更正公告</title></head><body>
    <h1>测试项目更正公告</h1>
    <ul>
      <li data-field="project-code" data-value="GSB-2026-001"></li>
      <li data-field="published-at" data-value="2026-11-08 09:30"></li>
      <li data-field="deadline" data-value="2026-11-18 09:00">投标截止时间：2026-11-18 09:00</li>
    </ul>
    <p>投标截止时间由 2026-11-12 09:00 调整为 2026年11月18日 09时00分。</p>
    </body></html>
    """
    d = parse_detail(html, "http://x/same-twice.html")
    assert d.deadline == datetime(2026, 11, 18, 9, 0)
    assert d.deadline_conflict is False


# --------------------------------------------------------------------------- #
# 合并器
# --------------------------------------------------------------------------- #
def _notice(kind, published, deadline=None, cancelled=False, url=None, reason=None):
    idx = id(_notice)
    return NoticeRecord(
        url=url or f"http://x/n{idx}.html",
        kind=kind,
        title=f"公告-{kind.value}",
        published_at=published,
        deadline=deadline,
        cancelled=cancelled,
        cancellation_reason=reason,
    )


def test_merge_correction_without_deadline_keeps_effective_time():
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 1, 10), datetime(2026, 11, 10, 9, 30)),
                _notice(NoticeKind.CORRECTION, datetime(2026, 11, 3, 15), None),
            ]
        )
    )
    assert result.status == ProjectStatus.OPEN
    assert result.can_bid is True
    assert result.effective_deadline == datetime(2026, 11, 10, 9, 30)


def test_merge_later_correction_overrides_deadline_even_if_unsorted_input():
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.CORRECTION, datetime(2026, 11, 6, 9), datetime(2026, 11, 15, 14)),
                _notice(NoticeKind.CORRECTION, datetime(2026, 11, 3, 15), None),
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 1, 10), datetime(2026, 11, 10, 9, 30)),
            ]
        )
    )
    assert result.status == ProjectStatus.OPEN
    assert result.effective_deadline == datetime(2026, 11, 15, 14, 0)
    # 输出按发布时间排序
    assert [n.published_at for n in result.ordered_notices] == sorted(n.published_at for n in result.ordered_notices)


def test_merge_cancellation_marks_closed():
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 2, 9), datetime(2026, 11, 12, 14, 30)),
                _notice(
                    NoticeKind.CANCELLATION,
                    datetime(2026, 11, 4, 11),
                    None,
                    cancelled=True,
                    reason="预算收回",
                ),
            ]
        )
    )
    assert result.status == ProjectStatus.CLOSED
    assert result.can_bid is False
    assert result.cancelled is True


def test_merge_any_failure_forces_pending_and_no_deadline():
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 5, 8), datetime(2026, 11, 18, 10)),
            ],
            failures=["http://x/g003-missing.html"],
        )
    )
    assert result.status == ProjectStatus.PENDING
    assert result.can_bid is False
    assert result.effective_deadline is None  # 旧时间不得作为最终期限
    assert any("抓取失败" in w for w in result.warnings)


def test_merge_missing_publish_time_forces_pending():
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 5, 8), datetime(2026, 11, 18, 10)),
                _notice(NoticeKind.CORRECTION, None, datetime(2026, 11, 20, 10)),
            ]
        )
    )
    assert result.status == ProjectStatus.PENDING
    assert result.effective_deadline is None


def test_merge_no_notices_is_not_found():
    result = merge_notices(MergeInput(notices=[]))
    assert result.status == ProjectStatus.NOT_FOUND
    assert result.effective_deadline is None


def test_merge_no_original_forces_pending():
    result = merge_notices(
        MergeInput(
            notices=[_notice(NoticeKind.CORRECTION, datetime(2026, 11, 3), datetime(2026, 11, 20, 10))]
        )
    )
    assert result.status == ProjectStatus.PENDING


def test_merge_original_without_deadline_and_no_supplement_is_pending():
    result = merge_notices(
        MergeInput(notices=[_notice(NoticeKind.ORIGINAL, datetime(2026, 11, 1))])
    )
    assert result.status == ProjectStatus.PENDING
    assert result.effective_deadline is None


def test_merge_conflicting_notice_forces_pending_and_clears_deadline():
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 3, 9), datetime(2026, 11, 16, 9, 30)),
                NoticeRecord(
                    url="http://x/conflict.html",
                    kind=NoticeKind.CORRECTION,
                    title="公告-correction",
                    published_at=datetime(2026, 11, 9, 16),
                    deadline=None,
                    deadline_conflict=True,
                    deadline_candidates=[
                        datetime(2026, 11, 20, 9, 30),
                        datetime(2026, 11, 22, 10, 0),
                    ],
                ),
            ]
        )
    )
    # 矛盾的现行截止时间：待核实、清空有效期限，不按页面先后猜一个
    assert result.status == ProjectStatus.PENDING
    assert result.can_bid is False
    assert result.effective_deadline is None
    assert any("矛盾" in w for w in result.warnings)
    # 冲突公告的候选时间仍随明细返回，供人工核实
    conflict_notice = next(n for n in result.ordered_notices if n.deadline_conflict)
    assert len(conflict_notice.deadline_candidates) == 2


def test_merge_no_change_then_reschedule_ends_with_current_time():
    # 仅重述旧日期（deadline=None）的更正不影响已生效期限；明确改期的更正再覆盖
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 2, 10), datetime(2026, 11, 12, 9, 0)),
                _notice(NoticeKind.CORRECTION, datetime(2026, 11, 5, 14), None),
                _notice(NoticeKind.CORRECTION, datetime(2026, 11, 8, 9, 30), datetime(2026, 11, 18, 9, 0)),
            ]
        )
    )
    assert result.status == ProjectStatus.OPEN
    assert result.effective_deadline == datetime(2026, 11, 18, 9, 0)
