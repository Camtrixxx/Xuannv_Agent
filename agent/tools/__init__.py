"""Pure-function analysis tools (the composable atoms of the skill/planner layer).

Tools here are deliberately side-effect free and independently unit-testable —
they take plain data (pixel counts, bounds) and return plain data (ratios,
hectares). Region services and, later, the ReAct planner call them; the tools
never call the network or touch config themselves.
"""

from agent.tools.aoi import aggregate_binary_coverage, aggregate_class_distribution
from agent.tools.change import aggregate_change, binary_change, foreground_mask, mask_for_task
from agent.tools.classmap import class_distribution, normalize_legend
from agent.tools.raster import (
    BINARY_TASK_BACKGROUND,
    area_ha_from_bounds,
    binary_coverage,
    binary_foreground_ratio,
)
from agent.tools.scoring import pressure_score, rank_patches, summarize_scores

__all__ = [
    "BINARY_TASK_BACKGROUND",
    "aggregate_binary_coverage",
    "aggregate_change",
    "aggregate_class_distribution",
    "area_ha_from_bounds",
    "binary_change",
    "binary_coverage",
    "binary_foreground_ratio",
    "class_distribution",
    "foreground_mask",
    "mask_for_task",
    "normalize_legend",
    "pressure_score",
    "rank_patches",
    "summarize_scores",
]
