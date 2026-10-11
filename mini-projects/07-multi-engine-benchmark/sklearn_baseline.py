# mini-projects/07-multi-engine-benchmark/sklearn_baseline.py
from __future__ import annotations

from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline


def train(texts: list[str], labels: list[str]):
    base = make_pipeline(TfidfVectorizer(ngram_range=(1, 2), min_df=2), LogisticRegression(max_iter=2000))
    return CalibratedClassifierCV(base, method="isotonic", cv=5).fit(texts, labels)
