from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


EXPERIMENTS = Path(__file__).parents[1] / "experiments" / "html_labels"
sys.path.insert(0, str(EXPERIMENTS))

from title_risk import assess_title, assess_titles  # noqa: E402


def test_high_risk_cover_and_withdrawn_titles_are_explainable() -> None:
    cover = assess_title("In This Issue")
    assert cover["title_risk_tier"] == "high"
    assert "cover_or_contents" in cover["title_risk_reasons"]
    withdrawn = assess_title("This abstract has been withdrawn")
    assert withdrawn["title_risk_tier"] == "high"
    assert "withdrawn" in withdrawn["title_risk_reasons"]


def test_title_equal_to_container_is_high_risk() -> None:
    result = assess_title("Journal of Testing", "Journal of Testing")
    assert result["title_risk_score"] >= 6
    assert "title_equals_container" in result["title_risk_reasons"]


def test_normal_research_title_remains_low_risk() -> None:
    result = assess_title("Association between treatment duration and long-term survival")
    assert result == {
        "title_risk_score": 0, "title_risk_tier": "low", "title_risk_reasons": ""
    }


def test_dataframe_assessment_preserves_index() -> None:
    frame = pd.DataFrame({
        "crossref_title": ["Cover", "A sufficiently descriptive research title"],
        "container_title": ["Journal", "Journal"],
    }, index=[10, 20])
    result = assess_titles(frame)
    assert result.index.tolist() == [10, 20]
    assert result.loc[10, "title_risk_tier"] == "high"
