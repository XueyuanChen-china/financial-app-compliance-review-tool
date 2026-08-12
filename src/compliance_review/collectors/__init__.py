"""Deterministic technical fact collectors."""

from compliance_review.collectors.api_documents import ApiDocumentCollector
from compliance_review.collectors.base import CollectorResult
from compliance_review.collectors.dependencies import DependencyCollector
from compliance_review.collectors.manifest import ManifestCollector

__all__ = [
    "ApiDocumentCollector",
    "CollectorResult",
    "DependencyCollector",
    "ManifestCollector",
]
