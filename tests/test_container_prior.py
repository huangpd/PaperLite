from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


EXPERIMENTS = Path(__file__).parents[1] / "experiments" / "html_labels"
sys.path.insert(0, str(EXPERIMENTS))

from container_prior import (  # noqa: E402
    apply_container_priors, build_container_priors, normalize_container,
)


def test_container_normalization_handles_html_and_case() -> None:
    assert normalize_container("  Parkinsonism &amp; Related Disorders ") == (
        "parkinsonism & related disorders"
    )


def test_smoothed_prior_and_unseen_fallback_marker() -> None:
    training = pd.DataFrame({
        "container_title": ["Journal A", "journal a", "Journal B"],
        "has_abstract_label": [0, 0, 1],
    })
    priors, global_rate = build_container_priors(training, smoothing=2)
    assert round(global_rate, 6) == round(2 / 3, 6)
    predicted = apply_container_priors(pd.DataFrame({
        "container_title": ["JOURNAL A", "Unknown Journal"]
    }), priors)
    assert predicted.loc[0, "container_history_count"] == 2
    assert bool(predicted.loc[0, "container_prior_available"])
    assert predicted.loc[1, "container_history_count"] == 0
    assert not bool(predicted.loc[1, "container_prior_available"])
    assert pd.isna(predicted.loc[1, "container_no_abstract_prior"])
