"""Per-task / per-scenario report skeletons.

Every report used to be poured into one fixed shape — 摘要 / 核心要点 / 深度解读 /
建议 — regardless of whether it answered "how built-up is this area", "what
changed since last month", or "where should we plant trees first". The sections
therefore read the same every time even when the underlying analysis was
completely different.

A skeleton says, for one kind of analysis: what question the report answers,
which sections it gets, what each section must lead with, and how long it runs.
``resolve`` picks one from the scenario (checkup / change / score), else the task
family. Headings are written from the reader's side ("高值区在哪里" rather than
"空间格局与主导特征") and every skeleton ends with a plain-language data-caveat
section, so technical boundaries stop interrupting the business narrative.

Pure data + string assembly: no I/O, no model calls, so it is cheap to unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Shared closing section. Kept identical across skeletons on purpose: the caveats
# belong at the end, in the same place, so a reader learns where to find them.
_BOUNDARY_SECTION = {
    "heading": "数据说明与使用边界",
    "brief": "用大白话讲这次结果能信到什么程度、哪些数字是参考值、什么情况下需要实地核实。"
    "只用输入里给出的方法与边界线索，不要新增说法。",
    "length": "100-160字",
}


@dataclass(frozen=True)
class ReportSkeleton:
    """The shape of one kind of report."""

    key: str
    # The reader's question this report answers, in their words. Drives the tone
    # of the whole piece.
    question: str
    # Who the report is written for, and how it should sound.
    audience: str
    # What the executive summary must lead with.
    summary_brief: str
    summary_length: str
    highlights_brief: str
    sections: list[dict[str, str]] = field(default_factory=list)
    recommendations_brief: str = "3-5 条可执行建议，务实、面向行动，每条一个字符串。"

    def section_plan(self) -> list[dict[str, str]]:
        return [*self.sections, dict(_BOUNDARY_SECTION)]

    def output_format(self) -> dict[str, object]:
        """The ``输出格式`` block handed to the writing model."""
        plan = self.section_plan()
        return {
            "summary": f"{self.summary_brief} 长度 {self.summary_length}。",
            "highlights": self.highlights_brief,
            "analysis": [
                {
                    "title": item["heading"],
                    "text": f"{item['brief']} 长度 {item['length']}。",
                }
                for item in plan
            ],
            "recommendations": self.recommendations_brief,
            "章节要求": (
                f"必须严格输出上面 {len(plan)} 个小节，顺序与小标题都不要改，"
                "每节都是 {title: 小标题, text: 正文} 对象，键名只能用 title 和 text。"
            ),
        }


# --- Scenario skeletons --------------------------------------------------

_CHECKUP = ReportSkeleton(
    key="checkup",
    question="这个片区现在是什么状况？哪几项值得注意？",
    audience="片区管理者。他们要在一页内掌握家底，不关心模型细节。",
    summary_brief="第一句就给整个片区的定性结论（建设成熟 / 开发中 / 以自然地表为主），"
    "再点出最突出的一到两项指标。",
    summary_length="140-200字",
    highlights_brief="3-5 条，每条一句话，说清某一专题的水平和它意味着什么，最重要的排最前。",
    sections=[
        {
            "heading": "一句话结论",
            "brief": "先给整体判断，再用建筑、道路、水体、施工四项覆盖率支撑它。避免逐项罗列，要有主次。",
            "length": "150-220字",
        },
        {
            "heading": "四个专题横向对比",
            "brief": "把四项覆盖率放在一起比较，指出哪项偏高、哪项偏低、彼此关系说明了什么样的地表结构。"
            "只比较输入里真实存在的数字。",
            "length": "180-260字",
        },
        {
            "heading": "值得注意的信号",
            "brief": "挑出异常或需要跟进的点（施工占比、水体偏少、绿地不足等）。"
            "输入里没有异常信号时，如实说明本次未见异常，不要编造。",
            "length": "150-220字",
        },
    ],
    recommendations_brief="3-5 条针对该片区的后续动作，按紧急程度排序，每条一个字符串。",
)

_CHANGE = ReportSkeleton(
    key="change",
    question="这段时间里，这片区变了什么？变化在哪里？",
    audience="需要盯建设动态的业务人员。他们最关心「有没有变、变多少、在哪」。",
    summary_brief="第一句就回答变了还是没变、净变化多少公顷、什么趋势。"
    "净变化接近零时要明确说本期基本没有变化，不要含糊其辞。",
    summary_length="140-200字",
    highlights_brief="3-4 条，围绕净变化量、新增/减少的对比、变化最集中的位置。",
    sections=[
        {
            "heading": "变了多少",
            "brief": "用新增、减少、净变化三个数字讲清变化规模和方向。"
            "如果两期结果几乎一致，直接说明本期未监测到明显变化及其可能原因。",
            "length": "150-220字",
        },
        {
            "heading": "变化集中在哪里",
            "brief": "依据逐 patch 净变化清单，说明变化的空间分布是集中还是分散。"
            "没有 patch 级差异时不要臆测聚集性。",
            "length": "150-220字",
        },
        {
            "heading": "这些变化意味着什么",
            "brief": "把变化量翻译成业务判断：是否属于正常建设节奏、哪些需要实地核验。",
            "length": "150-220字",
        },
    ],
    recommendations_brief="3-4 条核验或跟踪动作，指明优先核查对象，每条一个字符串。",
)

_SCORE = ReportSkeleton(
    key="score",
    question="这片区哪里最该补绿？为什么是那几块？",
    audience="做绿化决策的规划人员。他们要的是一份可以直接落地的优先名单。",
    summary_brief="第一句就点名最该补绿的位置和它们的压力分区间，再说明整体压力水平。",
    summary_length="140-200字",
    highlights_brief="3-5 条，突出最高分地块、压力分布档位、硬化与绿地的反差。",
    sections=[
        {
            "heading": "最该补绿的地块",
            "brief": "按排名讲清前几个优先地块各自的压力分、硬化率与绿地率，说明它们为什么排在前面。",
            "length": "180-260字",
        },
        {
            "heading": "为什么是它们",
            "brief": "解释压力分的构成逻辑（硬化强度与绿地缺口各占一半），"
            "说明高分地块是硬化高、绿地少，还是两者兼有。",
            "length": "150-220字",
        },
        {
            "heading": "整体压力格局",
            "brief": "描述高压/中压/低压地块的数量分布，指出压力是普遍性的还是集中在少数地块。",
            "length": "150-220字",
        },
    ],
    recommendations_brief="3-5 条补绿行动建议，区分先做哪些、后做哪些，每条一个字符串。",
)


# --- Task-family skeletons -----------------------------------------------

_BINARY_EXTRACTION = ReportSkeleton(
    key="binary_extraction",
    question="这片区这类地物有多少？集中在哪？能拿来干什么？",
    audience="需要掌握单项地物分布的业务读者。用「大约三分之一的地面」这种说法，别堆术语。",
    summary_brief="第一句就给覆盖率和它对应的直观量级（如约三分之一地面），再说明这个水平意味着什么。",
    summary_length="140-200字",
    highlights_brief="3-4 条，围绕覆盖率水平、覆盖面积、空间分布特征。",
    sections=[
        {
            "heading": "总体水平",
            "brief": "讲清覆盖率和覆盖面积，并把百分比翻译成读者能直观理解的说法。"
            "不要引入输入中没有的行业基准或对比参照。",
            "length": "150-220字",
        },
        {
            "heading": "高值区在哪里",
            "brief": "依据逐 patch 结果说明分布是集中还是均匀、哪些位置更突出。"
            "只有一个 patch 或缺少 patch 级差异时，如实说明尚无法判断空间聚集性。",
            "length": "150-220字",
        },
        {
            "heading": "能用来做什么",
            "brief": "把这项结果落到具体用途上（建设强度评估、更新需求识别、专项排查等），"
            "说明可以支撑哪类判断。",
            "length": "150-220字",
        },
    ],
)

_MULTICLASS = ReportSkeleton(
    key="multiclass",
    question="这片区地表是由什么构成的？主要是哪几类？",
    audience="需要掌握地表构成的业务读者。关心「哪类占大头」而非分类算法。",
    summary_brief="第一句就点出占比最高的两三类及其比例，给出这片区的地表定性。",
    summary_length="140-200字",
    highlights_brief="3-4 条，围绕主导类别、次要类别、结构上值得注意的比例关系。",
    sections=[
        {
            "heading": "地表由什么构成",
            "brief": "按占比从高到低讲清主导类别，指出这个构成属于什么类型的区域。"
            "只使用输入类别分布里真实出现的类别与数字。",
            "length": "180-260字",
        },
        {
            "heading": "结构上值得注意的地方",
            "brief": "指出比例失衡或意外的类别关系（人工地表与植被的比例、某类明显偏少等）。",
            "length": "150-220字",
        },
        {
            "heading": "能用来做什么",
            "brief": "说明这份地表构成可以支撑哪些判断，以及它作为模型直接输出的参考性质。",
            "length": "150-220字",
        },
    ],
)

_TERRAIN = ReportSkeleton(
    key="terrain",
    question="这片区地形长什么样？高低起伏怎么分布？",
    audience="需要了解地形条件的业务读者。",
    summary_brief="第一句就给高程范围和地形的整体特征（平缓 / 起伏明显 / 高差悬殊）。",
    summary_length="140-200字",
    highlights_brief="3-4 条，围绕高程区间、起伏程度、坡度特征。",
    sections=[
        {
            "heading": "地形整体特征",
            "brief": "讲清高程范围、平均水平和起伏程度，给出直观的地形画面。",
            "length": "150-220字",
        },
        {
            "heading": "高低分布格局",
            "brief": "描述高值与低值区域的分布关系，以及坡度所反映的地形变化节奏。",
            "length": "150-220字",
        },
        {
            "heading": "对业务的影响",
            "brief": "说明这样的地形条件会影响哪些工作（选址、施工难度、汇水判断等）。",
            "length": "150-220字",
        },
    ],
)

_CUSTOM_OBJECT = ReportSkeleton(
    key="custom_object",
    question="我关心的这类地物，模型找到了多少？结果可信吗？",
    audience="刚训练完自定义模型、想验证效果的用户。对结果可靠性格外敏感。",
    summary_brief="第一句就给识别到的覆盖率与面积，并说明这是自定义模型的识别结果。",
    summary_length="140-200字",
    highlights_brief="3-4 条，围绕识别量级、分布特征、结果的参考性质。",
    sections=[
        {
            "heading": "识别到了多少",
            "brief": "讲清覆盖率与面积，并说明这是基于少量标注样本训练的模型输出。",
            "length": "150-220字",
        },
        {
            "heading": "分布在哪里",
            "brief": "依据逐 patch 结果说明分布特征；缺少 patch 级差异时如实说明。",
            "length": "150-220字",
        },
        {
            "heading": "结果可信度与下一步",
            "brief": "坦诚说明自定义模型受标注样本量影响，指出如何判断结果好坏、"
            "以及补充标注可以带来什么改善。",
            "length": "150-220字",
        },
    ],
    recommendations_brief="3-4 条建议，覆盖结果核验与模型改进方向，每条一个字符串。",
)

_GENERIC = ReportSkeleton(
    key="generic",
    question="这次分析得出了什么结论？",
    audience="业务与管理读者。",
    summary_brief="结论先行，讲清区域、时间、任务的核心结论与业务价值。",
    summary_length="160-240字",
    highlights_brief="3-5 条核心要点，每条一句话、可独立成立，最重要的排最前。",
    sections=[
        {
            "heading": "结果说明了什么",
            "brief": "先给核心结论，再用关键指标支撑。",
            "length": "180-260字",
        },
        {
            "heading": "空间格局与主导特征",
            "brief": "描述分布特征与主导现象；没有空间证据时不要推断聚集性。",
            "length": "180-260字",
        },
        {
            "heading": "对业务意味着什么",
            "brief": "把结果翻译成业务判断与可能的用途。",
            "length": "150-220字",
        },
    ],
)


_BY_SCENARIO: dict[str, ReportSkeleton] = {
    "checkup": _CHECKUP,
    "change": _CHANGE,
    "score": _SCORE,
}

# Task display name -> skeleton. Keyed on the user-facing task vocabulary in
# agent/taxonomy.py; matching is substring-based so decorated task names
# ("施工识别·建设扰动监测", "湿地识别") still land on the right skeleton.
_BY_TASK: tuple[tuple[str, ReportSkeleton], ...] = (
    ("建筑物提取", _BINARY_EXTRACTION),
    ("道路提取", _BINARY_EXTRACTION),
    ("施工识别", _BINARY_EXTRACTION),
    ("水体提取", _BINARY_EXTRACTION),
    ("水体分布", _BINARY_EXTRACTION),
    ("土地覆盖分类", _MULTICLASS),
    ("土地利用分类", _MULTICLASS),
    ("地物分类", _MULTICLASS),
    ("高程地形", _TERRAIN),
)


def resolve(*, task: str = "", scenario: str = "", custom_object: str = "") -> ReportSkeleton:
    """Pick the skeleton for one analysis.

    Scenario wins over task: a 片区综合体检 aggregates several tasks, so its own
    shape must not be overridden by whichever task name it happens to carry.
    A custom-model run gets the reliability-forward skeleton, since its reader is
    validating a freshly trained model rather than consuming a settled product.
    """
    scenario_key = str(scenario or "").strip()
    if scenario_key in _BY_SCENARIO:
        return _BY_SCENARIO[scenario_key]
    if str(custom_object or "").strip():
        return _CUSTOM_OBJECT
    task_text = str(task or "")
    for needle, skeleton in _BY_TASK:
        if needle in task_text:
            return skeleton
    return _GENERIC


def all_skeletons() -> tuple[ReportSkeleton, ...]:
    """Every distinct skeleton, for tests and introspection."""
    return (
        _CHECKUP, _CHANGE, _SCORE,
        _BINARY_EXTRACTION, _MULTICLASS, _TERRAIN, _CUSTOM_OBJECT, _GENERIC,
    )
