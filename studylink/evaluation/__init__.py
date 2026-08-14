"""Retrieval quality evaluation for StudyLink.

Three pieces:

  dataset.py  the labelled note<->assignment pairs, stored as JSON so they can be
              reviewed in a diff and version-controlled alongside the code
  metrics.py  precision@k / recall@k / MRR / nDCG / MAP over a ranking
  judge.py    an LLM-as-judge second opinion, for scaling past hand labels
  runner.py   runs the retriever over the labelled set and sweeps configs
"""

from .metrics import RankingMetrics, evaluate_ranking  # noqa: F401
from .dataset import LabeledPair, load_labels, save_labels  # noqa: F401
