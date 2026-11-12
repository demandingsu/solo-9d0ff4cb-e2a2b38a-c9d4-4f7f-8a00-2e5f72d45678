"""HTML 解析：列表页链接发现 + 详情页字段提取。

解析策略（三层，逐层回退）：
1. 结构化标记：``data-field`` / ``meta[property]``，演示站点和现代采购平台可直接使用；
2. 标签匹配：按“发布时间/截止时间/取消”等中文标签定位文本节点；
3. 时间正则：在正文文本中兜底识别日期时间，并排除发布时间本身。

截止时间改期识别（更正公告专项）：
* 显式“原时间 → 现时间”改期表述（标签字段或正文均可），只取**调整后**的现行时间，
  绝不取句首的原时间（修复“取第一个日期仍报旧期限”的问题）；
* “未调整/不变/仍按原……执行”的更正仅为旧时间重述，本页不产生新期限，
  合并时沿用此前已生效的截止时间；
* 同一公告给出两个及以上相互矛盾的现行截止时间且无法统一时，标记
  ``deadline_conflict=True`` 并清空本页期限，交由合并器置为待核实，不按页面先后猜测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# 支持的日期时间写法，如 2026-11-05 09:00、2026年11月5日 9时00分、2026/11/05
_DATETIME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?",
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[:：时]\s*(\d{1,2})\s*分?(?:\s*(\d{1,2})\s*秒)?",
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?!\s*\d)",  # 仅日期，如 2026年11月03日
    )
)
_DATE_ONLY = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})\s*日?")

# 详情页字段标签
_PUBLISH_LABELS = ("发布时间", "发布日期", "公告时间", "公示时间", "发布日期时间")
_DEADLINE_LABELS = (
    "投标截止时间",
    "投标截止日期",
    "响应文件递交截止时间",
    "响应截止时间",
    "报价截止时间",
    "递交截止时间",
    "截止时间",
    "截止日期",
)
_CANCEL_LABELS = ("取消", "终止", "废标", "撤项")
_CANCEL_REASON_LABELS = ("取消原因", "终止原因", "废标原因")
_PROJECT_LABELS = ("项目编号", "项目编码", "采购编号", "招标编号")

# 改期表述：捕获组中“现时间”一侧的引导词，命中后只采用这些引导词后面的日期时间
# （“原/原定/原投标……”一侧的日期只是被替换的旧时间，绝不作为现行期限）
_RESCHEDULE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # 由/从 2026-11-12 09:00 调整/变更/延期 为/至/到 2026-11-18 09:00
        r"(?:由|从)[^。；;\n]{0,40}?(?:调整|变更|更改|修改|延期|顺延|推迟|改期)[^。；;\n]{0,12}?(?:为|至|到|到?达)\s*",
        # 原……为 X，现/现调整/现延期/现变更（为/至）Y；要求“现”后紧跟改期动词或“为/至/到”
        r"现(?:调整|延期|顺延|推迟|变更|更改|改期|确定|拟定)[^。；;\n]{0,6}?(?:为|至|到)?\s*",
        r"现(?:为|至|到)\s*",
        # 调整/延期/顺延/推迟/变更/改期 后/至/到/为 X（窗口不跨越逗号/冒号，避免吃到从句另一头）
        r"(?:调整|变更|更改|修改|延期|顺延|推迟|改期)[^。；;，,：:\n]{0,8}?(?:后|至|到|为)\s*",
        # 截止时间（的）调整/延期/顺延/变更（为/至/到）X
        r"截止时间(?:的)?[^。；;，,：:\n]{0,6}?(?:调整|变更|更改|修改|延期|顺延|推迟|改期)[^。；;，,：:\n]{0,6}?(?:为|至|到)\s*",
        # 新（的）（投标）截止时间（为）X
        r"新(?:的|投标|响应|报价|递交)?[^。；;，,：:\n]{0,8}?截止时间[^。；;，,：:\n]{0,6}?(?:为|是|：|:)?\s*",
        # 截止时间调整为 X（标签字段常见的短表述）
        r"截止时间[^。；;，,：:\n]{0,10}?(?:调整|变更|更改|修改|延期|顺延|推迟|改期)[^。；;，,：:\n]{0,6}?(?:为|至|到)\s*",
    )
)

# 声明“本公告未改期”的表述；同页出现此类声明时，单纯重述的旧日期不产生新期限
_NO_CHANGE_PATTERN = re.compile(
    r"(?:未|不予|不再|没有|无)\s*(?:调整|变更|更改|修改|延期|顺延|推迟|改期|变动|变化)"
    r"|(?:截止时间|投标截止时间|响应截止时间|递交截止时间|报价截止时间|开标时间)"
    r"[^。；;\n]{0,20}?(?:不变|保持不变|维持不变|仍按|仍为|仍维持|按原|照原|同原)"
    r"|其他(?:内容|事项)?(?:保持|维持)?不变|其余内容不变"
)

# 改期句中被替换的“原时间”从句：本身不承载现行期限，必须忽略
# （句子按逗号切分后，“原投标截止时间为 X”可能与“现延期至 Y”分家）
_OLD_TIME_CLAUSE = re.compile(
    r"(?:原|原定|原招标|原采购|原投标|原响应|原报价|原文件|此前|之前|原定投标|原递交)"
    r"[^。；;，,\n]{0,24}?(?:截止|递交|开标|投标|响应|报价|时间|期限|安排)"
    r"[^。；;，,\n]{0,12}?(?:为|是|：|:|定于|在)"
    r"|^[^。；;，,\n]{0,12}?(?:截止时间|开标时间)[^。；;，,\n]{0,8}?原定"
)

# 时限语义：正文兜底扫描时只看含投标时限语境的句子
_TIME_LIMIT_HINT = re.compile(r"截止|递交|提交|受理|送达|逾期|之前|前完成|开标")

# 合理公告年份窗口，过滤资质/业绩说明等场景下的异常或历史日期
_MIN_YEAR = 2000
_MAX_YEAR = 2100

# 标题里用于判定公告类型的关键词
_CORRECTION_KEYWORDS = ("更正", "澄清", "变更", "修改", "延期", "答疑", "补充公告")
_CANCELLATION_KEYWORDS = ("取消", "终止", "废标", "撤项")


class ParseError(ValueError):
    """页面结构无法解析出必要字段。"""


@dataclass
class DetailData:
    """详情页原始解析结果。"""

    title: str
    project_code: str | None
    published_at: datetime | None
    deadline: datetime | None
    cancelled: bool
    cancellation_reason: str | None
    kind_hint: str  # original / correction / cancellation，来自标题与正文线索
    deadline_conflict: bool = False  # 同页出现相互矛盾的现行截止时间
    deadline_candidates: list[datetime] = field(default_factory=list)  # 冲突时留存全部候选，便于人工核实
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def parse_datetime(text: str | None) -> datetime | None:
    """从一段文本中解析出第一个日期时间；只有日期时按当天 00:00 处理。"""
    if not text:
        return None
    for pattern in _DATETIME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groups()
        if len(groups) == 3:  # 仅日期
            year, month, day = (int(g) for g in groups)
            return datetime(year, month, day)
        year, month, day, hour, minute = (int(g) for g in groups[:5])
        second = int(groups[5]) if len(groups) >= 6 and groups[5] else 0
        return datetime(year, month, day, hour, minute, second)
    match = _DATE_ONLY.search(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return datetime(year, month, day)
    return None


def _iter_datetimes(text: str) -> list[datetime]:
    """提取一段文本中出现的**全部**日期时间，保持出现顺序并去重。

    完整日期时间模式与“仅日期”模式可能命中同一文本区间（如
    ``2026-11-18 09:00`` 同时匹配出 ``2026-11-18 00:00``），
    因此先收集完整日期时间的匹配区间，落在其中的仅日期结果一律丢弃。
    """
    full_spans: list[tuple[int, int]] = []
    full_matches: list[tuple[int, datetime]] = []

    # 先跑带时间的模式（前两个），记录其覆盖区间
    for pattern in _DATETIME_PATTERNS[:2]:
        for match in pattern.finditer(text):
            groups = match.groups()
            year, month, day, hour, minute = (int(g) for g in groups[:5])
            second = int(groups[5]) if len(groups) >= 6 and groups[5] else 0
            full_spans.append(match.span())
            full_matches.append((match.start(), datetime(year, month, day, hour, minute, second)))

    def _covered(span: tuple[int, int]) -> bool:
        return any(start <= span[0] and span[1] <= end for start, end in full_spans)

    date_matches: list[tuple[int, datetime]] = []
    # 仅日期模式（第三个内置模式 + 通用日期正则）
    for match in _DATETIME_PATTERNS[2].finditer(text):
        if not _covered(match.span()):
            date_matches.append((match.start(), datetime(*(int(g) for g in match.groups()))))
    for match in _DATE_ONLY.finditer(text):
        if not _covered(match.span()):
            date_matches.append((match.start(), datetime(*(int(g) for g in match.groups()))))

    results: list[datetime] = []
    seen: set[datetime] = set()
    for _, dt in sorted(full_matches + date_matches, key=lambda item: item[0]):
        if _MIN_YEAR <= dt.year <= _MAX_YEAR and dt not in seen:
            seen.add(dt)
            results.append(dt)
    return results


@dataclass
class _DeadlineVerdict:
    """单个文本片段的截止时间判定。"""

    deadline: datetime | None
    conflict: bool = False
    candidates: list[datetime] = field(default_factory=list)
    rescheduled: bool = False  # 片段是否为明确的“原时间→现时间”改期表述
    no_change: bool = False   # 片段是否声明未改期


def _analyse_deadline_text(text: str) -> _DeadlineVerdict:
    """分析一个文本片段中的截止时间表述。

    三类结果：
    * 明确改期（含“由 X 调整为 Y / 现延期至 Y / 新截止时间为 Y”等）→ 只取引导词之后
      的现行时间，句首旧时间一律忽略；多个不同现行时间 → 冲突；
    * 声明未改期（“未调整/不变/仍按原……执行”）→ 不产生期限，仅作旧时间重述处理；
    * 普通时限句 → 取句内首个日期时间（保持历史兜底行为）。
    """
    if not text:
        return _DeadlineVerdict(None)

    no_change = bool(_NO_CHANGE_PATTERN.search(text))

    new_times: list[datetime] = []
    seen: set[datetime] = set()
    for pattern in _RESCHEDULE_PATTERNS:
        for match in pattern.finditer(text):
            # 引导词之后到句末（或下一个逗号/分号前）的窗口内取现行时间
            window = text[match.end():]
            window = re.split(r"[。；;，,\n]", window, maxsplit=1)[0]
            # 引导词实际跨越了“原……为”旧时间从句（如“延期：原投标截止时间为 X”，
            # “原……为”可能已被引导词消耗，窗口直接以日期开头），
            # 窗口里的日期是被替换的旧时间，不得当作现行时间
            prefix = text[max(0, match.start() - 12):match.end()]
            if re.search(r"(?:原|原定|此前|之前|原招标|原投标|原响应|原报价)\D{0,20}(?:为|是|：|:)?$", prefix):
                continue
            if re.match(r"\s*(?:原|原定|此前|之前|原招标|原投标|原响应|原报价)", window):
                continue
            for dt in _iter_datetimes(window):
                if dt not in seen:
                    seen.add(dt)
                    new_times.append(dt)

    # 整句只有旧时间从句、没有任何可提取的现行时间（典型：逗号切分后单独成段的
    # “现对……延期：原投标截止时间为 X”）→ 忽略旧时间，不参与兜底取值
    if not new_times and _OLD_TIME_CLAUSE.search(text):
        return _DeadlineVerdict(None, no_change=no_change)

    if new_times:
        distinct = list(dict.fromkeys(new_times))
        if len(distinct) > 1:
            return _DeadlineVerdict(
                deadline=None,
                conflict=True,
                candidates=distinct,
                rescheduled=True,
                no_change=no_change,
            )
        return _DeadlineVerdict(deadline=distinct[0], rescheduled=True, no_change=no_change)

    if no_change:
        # 仅重述旧日期且声明未改期：本片段不贡献新期限
        return _DeadlineVerdict(None, no_change=True)

    dt = parse_datetime(text)
    if dt and _MIN_YEAR <= dt.year <= _MAX_YEAR:
        return _DeadlineVerdict(deadline=dt)
    return _DeadlineVerdict(None)


def same_site(url: str, base_url: str) -> bool:
    """判断两个 URL 是否同站点（scheme + host 一致；端口不同视为不同站点）。"""
    a, b = urlparse(url), urlparse(base_url)
    if not a.scheme or not a.netloc:
        return False
    return (a.scheme.lower(), a.netloc.lower()) == (b.scheme.lower(), b.netloc.lower())


def _soup(html: str) -> BeautifulSoup:
    # 优先 lxml，未安装时回退内置 html.parser
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - 取决于运行环境
        return BeautifulSoup(html, "html.parser")


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text).strip() if text else ""


# --------------------------------------------------------------------------- #
# 列表页
# --------------------------------------------------------------------------- #
def extract_detail_links(html: str, list_url: str) -> list[tuple[str, str, str]]:
    """提取列表页中指向同站点详情页的链接。

    返回 ``(绝对URL, 链接文本, 行上下文)`` 去重列表，保持页面出现顺序。
    行上下文取链接所在 ``tr``/``li``/容器行的完整文本，通常包含项目编号，
    用于在详情页抓取失败时仍能判定该链接是否属于目标项目。
    导航/分页/登录类链接通过路径关键词排除。
    """
    soup = _soup(html)
    seen: set[str] = set()
    links: list[tuple[str, str, str]] = []
    skip_path = re.compile(r"(login|logout|register|index|home|about|contact|search|#|javascript)", re.I)

    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(list_url, anchor["href"].strip())
        if not absolute.startswith(("http://", "https://")):
            continue
        if not same_site(absolute, list_url):
            continue  # 只抓同站点链接，外站公告不纳入
        parsed = urlparse(absolute)
        if skip_path.search(parsed.path) or parsed.path.rstrip("/") == urlparse(list_url).path.rstrip("/"):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        row = anchor.find_parent(["tr", "li", "dd", "article"]) or anchor.parent
        context = _clean(row.get_text(" ")) if row is not None else ""
        links.append((absolute, _clean(anchor.get_text()), context))
    return links


# --------------------------------------------------------------------------- #
# 详情页
# --------------------------------------------------------------------------- #
def _meta_value(soup: BeautifulSoup, key: str) -> str | None:
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        if name == key.lower():
            return meta.get("content")
    return None


def _data_field(soup: BeautifulSoup, name: str) -> str | None:
    """读取 ``data-field="name"`` 元素的取值。"""
    node = soup.find(attrs={"data-field": name})
    if node is None:
        return None
    value = node.get("data-value")
    if value is not None:
        return value.strip()
    return _clean(node.get_text())


def _labeled_value(soup: BeautifulSoup, labels: tuple[str, ...]) -> str | None:
    """按“标签：值”结构取值，兼容 <td>/<th>/<dt><dd>/<li>/<p> 等布局。"""
    label_set = set(labels)

    # 1) <th>标签</th><td>值</td> 或 <dt>/<dd>
    for label_node in soup.find_all(["th", "dt"]):
        if _clean(label_node.get_text()).rstrip("：:") in label_set:
            sibling = label_node.find_next(["td", "dd"])
            if sibling is not None:
                return _clean(sibling.get_text())

    # 2) 任意元素中“标签：值”同行文本
    pattern = re.compile(
        r"(?:^|\s)(" + "|".join(re.escape(x) for x in labels) + r")\s*[:：]\s*(.+?)(?:\s{2,}|$)"
    )
    for node in soup.find_all(["td", "li", "p", "span", "div"]):
        # 跳过还包含同层标签的容器，避免把整块正文当成值
        if node.find(["td", "li", "p", "div"]):
            continue
        text = _clean(node.get_text(" "))
        match = pattern.search(text)
        if match:
            return _clean(match.group(2))
    return None


def _extract_project_code(soup: BeautifulSoup) -> str | None:
    value = _data_field(soup, "project-code") or _labeled_value(soup, _PROJECT_LABELS)
    return _clean(value) or None


def _extract_published_at(soup: BeautifulSoup) -> datetime | None:
    raw = (
        _data_field(soup, "published-at")
        or _meta_value(soup, "article:published_time")
        or _labeled_value(soup, _PUBLISH_LABELS)
    )
    return parse_datetime(raw)


def _deadline_segments(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """收集页面上所有可能承载截止时间的文本片段（结构化标记 → 标签 → 正文）。

    保留全部来源而非首个命中，以便识别不同来源之间相互矛盾的现行截止时间。
    """
    segments: list[tuple[str, str]] = []
    raw = _data_field(soup, "deadline")
    if raw:
        segments.append(("data-field", raw))
    raw = _labeled_value(soup, _DEADLINE_LABELS)
    if raw:
        segments.append(("label", raw))

    # 正文正则兜底：逐块级元素（p/li/td/div 等）扫描含时限语义的文本，
    # 避免 get_text 把发布时间与截止时间拼进同一段导致误取发布时间；
    # 块内再按句末标点/逗号切分，兼容“应在 X 之前完成递交，逾期……”的写法
    for node in soup.find_all(["p", "li", "td", "div", "dd"]):
        if node.find(["p", "li", "td", "div"]):
            continue  # 只取叶子块，避免重复扫描容器
        block = _clean(node.get_text(" "))
        if "发布时间" in block or not _TIME_LIMIT_HINT.search(block):
            continue
        # 资质/业绩语境（如“近三年（2023年1月1日起）完成 2 项业绩”）不是投标时限
        history_hint = re.compile(r"近\s*\d+\s*年|业绩|资质|经验|成立以来|至今|起?算")
        for sentence in re.split(r"[。；;，,\n]", block):
            sentence = sentence.strip()
            if not sentence or history_hint.search(sentence):
                continue
            segments.append(("body", sentence))
    return segments


def _extract_deadline(
    soup: BeautifulSoup, body_text: str
) -> tuple[datetime | None, bool, list[datetime]]:
    """提取现行投标截止时间。

    返回 ``(deadline, conflict, candidates)``：
    * 改期表述只采用“现时间”一侧的日期，绝不取句首旧时间；
    * 声明未改期的旧时间重述不产生期限；
    * 同页出现多个相互矛盾的现行时间时，``conflict=True``、``deadline=None``，
      全部候选放入 ``candidates`` 供人工核实，不按页面先后猜测。
    """
    verdicts = [
        (source, _analyse_deadline_text(text))
        for source, text in _deadline_segments(soup)
    ]
    if not verdicts:
        return None, False, []

    page_no_change = any(v.no_change for _, v in verdicts)
    page_rescheduled = any(v.rescheduled for _, v in verdicts)
    # 声明未改期且全页无任何改期表述 → 普通片段中的日期只是旧时间重述，整体不贡献期限
    suppress_plain = page_no_change and not page_rescheduled

    candidates: list[datetime] = []
    seen: set[datetime] = set()

    def _collect(times: list[datetime]) -> None:
        for dt in times:
            if dt not in seen:
                seen.add(dt)
                candidates.append(dt)

    conflicts: list[_DeadlineVerdict] = []
    deadlines: list[datetime] = []
    for _, verdict in verdicts:
        if verdict.conflict:
            conflicts.append(verdict)
            _collect(verdict.candidates)
            continue
        if verdict.deadline is None:
            continue
        if suppress_plain and not verdict.rescheduled:
            continue  # 未改期更正里的旧日期重述
        deadlines.append(verdict.deadline)

    if conflicts:
        _collect(deadlines)
        return None, True, candidates

    distinct = list(dict.fromkeys(deadlines))
    if len(distinct) > 1:
        # 不同来源（如 data-field 与正文改期句）给出不一致的现行截止时间
        return None, True, distinct
    if distinct:
        return distinct[0], False, []
    return None, False, []


def _extract_cancelled(soup: BeautifulSoup, body_text: str) -> tuple[bool, str | None]:
    flagged = soup.find(attrs={"data-field": "cancelled"})
    if flagged is not None:
        flag = (flagged.get("data-value") or flagged.get_text()).strip().lower()
        if flag in ("true", "1", "yes", "是"):
            reason = _data_field(soup, "cancellation-reason") or _labeled_value(soup, _CANCEL_REASON_LABELS)
            return True, _clean(reason) or None
        return False, None

    reason = _labeled_value(soup, _CANCEL_REASON_LABELS)
    if reason:
        return True, _clean(reason)
    # 显式取消句式，避免“不取消”之类的否定误判
    if re.search(r"(?:本项目|本次采购|本次招标|该项目)[^。；\n]{0,12}(?:取消|终止|废标)", body_text):
        return True, None
    if re.search(r"(?:取消|终止|废标)(?:本次|本项目|该项目)?(?:采购|招标|投标)", body_text):
        return True, None
    return False, None


def _detect_kind(title: str, cancelled: bool) -> str:
    if cancelled or any(k in title for k in _CANCELLATION_KEYWORDS):
        return "cancellation"
    if any(k in title for k in _CORRECTION_KEYWORDS):
        return "correction"
    return "original"


def parse_detail(html: str, url: str) -> DetailData:
    """解析公告详情页，提取合并所需的全部字段。"""
    soup = _soup(html)

    title_node = soup.find("title")
    h1 = soup.find("h1")
    title = _clean(h1.get_text() if h1 else (title_node.get_text() if title_node else ""))
    if not title:
        raise ParseError(f"详情页缺少标题：{url}")

    body_text = _clean(soup.get_text(" "))
    project_code = _extract_project_code(soup)
    published_at = _extract_published_at(soup)
    deadline, deadline_conflict, deadline_candidates = _extract_deadline(soup, body_text)
    cancelled, reason = _extract_cancelled(soup, body_text)
    kind_hint = _detect_kind(title, cancelled)

    warnings: list[str] = []
    if published_at is None:
        warnings.append("未能解析发布时间，无法参与按时间合并")
    if deadline_conflict:
        formatted = "、".join(dt.strftime("%Y-%m-%d %H:%M") for dt in deadline_candidates)
        warnings.append(f"同页出现相互矛盾的现行投标截止时间（{formatted}），已标记待核实")

    return DetailData(
        title=title,
        project_code=project_code,
        published_at=published_at,
        deadline=deadline,
        cancelled=cancelled,
        cancellation_reason=reason,
        kind_hint=kind_hint,
        deadline_conflict=deadline_conflict,
        deadline_candidates=deadline_candidates,
        warnings=warnings,
    )
