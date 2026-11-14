from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd


EXPERIMENTS = Path(__file__).parents[1] / "experiments" / "html_labels"
sys.path.insert(0, str(EXPERIMENTS))

from metadata_features import MetadataFeatureTransformer  # noqa: E402


def sample() -> pd.DataFrame:
    return pd.DataFrame({
        "doi": ["10.1016/j.test.2025.123456", "10.1016/S0001-0001(25)00001-2"],
        "crossref_title": ["EDITORIAL: In This Issue", "A clinical case report?"],
        "container_title": ["Test Journal", "A clinical case report?"],
        "publisher": ["Elsevier BV", None],
        "member": ["78", None], "prefix": ["10.1016", None],
        "type": ["journal-article", None], "language": ["en", None],
        "reference_count": [0, None], "cited_by_count": [3, 0],
        "year": [2025, 2024], "author_count": [None, 4],
        "title_length": [24, 23], "link_count": [2, None],
        "license_count": [1, 0], "volume": ["1", None],
        "issue": [None, "2"], "page": [None, "10-12"],
        "article_number": ["123456", None],
        # Leakage columns must never appear in transformed output.
        "has_abstract_label": [1, 0], "html_abstract_length": [100, 0],
        "abstract_source": ["meta:citation_abstract", None],
    })


def test_engineered_features_are_deterministic_and_leakage_free() -> None:
    transformer = MetadataFeatureTransformer("engineered", reference_year=2026)
    first = transformer.fit_transform(sample())
    second = transformer.transform(sample())
    pd.testing.assert_frame_equal(first, second)
    assert "has_abstract_label" not in first
    assert "html_abstract_length" not in first
    assert "abstract_source" not in first
    assert first.loc[0, "is_editorial"] == 1
    assert first.loc[0, "is_cover_contents"] == 1
    assert first.loc[1, "is_case_report"] == 1
    assert first.loc[1, "title_container_same"] == 1


def test_doi_and_missing_value_features() -> None:
    transformed = MetadataFeatureTransformer("engineered").fit_transform(sample())
    assert transformed.loc[0, "doi_stem"] == "j"
    assert transformed.loc[0, "doi_suffix_style"] == "year_segment"
    assert transformed.loc[1, "doi_suffix_style"] == "legacy_paren"
    assert transformed.loc[0, "reference_count_zero"] == 1
    assert transformed.loc[1, "reference_count_missing"] == 1
    assert transformed.loc[1, "publisher"] == "__missing__"
    assert np.isnan(transformed.loc[1, "reference_count_log1p"])


def test_base_features_keep_original_contract() -> None:
    transformed = MetadataFeatureTransformer("base").fit_transform(sample())
    assert transformed.columns.tolist() == (
        MetadataFeatureTransformer("base").categorical_features
        + MetadataFeatureTransformer("base").numeric_features
    )
    assert "doi_stem" not in transformed


def test_no_title_feature_set_excludes_article_title_signal() -> None:
    transformed = MetadataFeatureTransformer("engineered_no_title").fit_transform(sample())
    assert "doi_stem" in transformed
    assert transformed.loc[0, "doi_stem"] == "j"
    assert "reference_count_log1p" in transformed
    assert transformed.loc[0, "reference_count_zero"] == 1
    assert "title_length" not in transformed
    assert "title_word_count" not in transformed
    assert "is_withdrawn" not in transformed



