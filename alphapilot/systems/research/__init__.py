"""Research-campaign gates shared by mining, selection and deployment."""

from alphapilot.systems.research.gates import (
    EconomicGateConfig,
    FactorGateConfig,
    evaluate_economic_gate,
    evaluate_factor_gate,
    select_diverse_factors,
    validate_factor_expression,
)
from alphapilot.systems.research.execution_quality import (
    evaluate_implementation_shortfall,
)
from alphapilot.systems.research.inference_parity import (
    build_inference_snapshot,
    compare_inference_snapshots,
    factor_values_hash,
    numeric_mapping_hash,
)
from alphapilot.systems.research.selection import (
    preregister_candidate_sets,
    validate_development_evidence,
)
from alphapilot.systems.research.whitelist import (
    build_live_whitelist,
    freeze_whitelist,
    verify_whitelist,
)

__all__ = [
    "EconomicGateConfig",
    "FactorGateConfig",
    "build_live_whitelist",
    "build_inference_snapshot",
    "compare_inference_snapshots",
    "evaluate_economic_gate",
    "evaluate_factor_gate",
    "evaluate_implementation_shortfall",
    "factor_values_hash",
    "freeze_whitelist",
    "preregister_candidate_sets",
    "numeric_mapping_hash",
    "select_diverse_factors",
    "validate_factor_expression",
    "validate_development_evidence",
    "verify_whitelist",
]
