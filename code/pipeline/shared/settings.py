from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(slots=True)
class PipelineConfig:
    pipeline_root: Path
    corpus_pdf_dir: Path
    candidate_claims_path: Path
    doc_weight_review: float = 1.5
    doc_weight_original_article: float = 1.0
    top_citing_docs_limit: int = 10
    claim_discovery_chunk_max_claims: int = 0
    claim_discovery_doc_max_claims: int = 0
    claim_discovery_chunk_budget: int = 0
    claim_discovery_doc_budget: int = 0
    claim_discovery_batch_chunks: int = 4
    claim_discovery_batch_chars: int = 12000
    claim_discovery_chunk_workers: int = 8
    claim_discovery_doc_workers: int = 4
    skip_document_claim_extraction: bool = True
    claim_discovery_provider_max_attempts: int = 3
    claim_discovery_provider_retry_backoff_seconds: float = 2.0
    step3b_llm_judge_batch_size: int = 30
    step3b_llm_cluster_batch_size: int = 120
    step3b_llm_verifier_batch_size: int = 40
    step3b_llm_verifier_workers: int = 1
    step3b_llm_verifier_max_attempts: int = 3
    step3b_llm_verifier_retry_sleep_seconds: float = 8.0
    claim_pool_max_size: int = 0
    claim_min_df: int = 2

    @property
    def corpus_stage_dir(self) -> Path:
        return self.pipeline_root / "02_corpus_index"

    @property
    def corpus_index_dir(self) -> Path:
        return self.corpus_stage_dir / "文献索引"

    @property
    def doc_text_index_path(self) -> Path:
        return self.corpus_index_dir / "全文索引.csv"

    @property
    def text_chunks_dir(self) -> Path:
        return self.pipeline_root / "03_text_chunks"

    @property
    def chunk_manifest_path(self) -> Path:
        return self.text_chunks_dir / "检索块清单.csv"

    @property
    def semantic_index_dir(self) -> Path:
        return self.pipeline_root / "04_semantic_index"

    @property
    def document_abstraction_path(self) -> Path:
        return self.semantic_index_dir / "文档抽象索引.csv"

    @property
    def claim_generation_dir(self) -> Path:
        return self.pipeline_root / "05_claim_generation"

    @property
    def raw_claim_candidates_path(self) -> Path:
        return self.claim_generation_dir / "01_raw_claim_candidates.csv"

    @property
    def structured_claim_pool_path(self) -> Path:
        return self.claim_generation_dir / "02_structured_claim_pool.csv"

    @property
    def claims_master_path(self) -> Path:
        return self.claim_generation_dir / "03_claims_master.csv"

    @property
    def consensus_claims_path(self) -> Path:
        return self.claim_generation_dir / "04_consensus_claims.csv"

    @property
    def claim_scope_review_path(self) -> Path:
        return self.claim_generation_dir / "03A_claim_scope_review.csv"

    @property
    def quarantined_claim_candidates_path(self) -> Path:
        return self.claim_generation_dir / "03A_quarantined_candidates.csv"

    @property
    def step3c_generalization_review_path(self) -> Path:
        return self.claim_generation_dir / "03C_generalization_review.csv"

    @property
    def evidence_retrieval_dir(self) -> Path:
        return self.pipeline_root / "07_evidence_retrieval"

    @property
    def support_evidence_rows_path(self) -> Path:
        return self.evidence_retrieval_dir / "01_support_evidence_rows.csv"

    @classmethod
    def for_project_root(
        cls,
        pipeline_root: Path,
        corpus_pdf_dir: Path | None = None,
        candidate_claims_path: Path | None = None,
    ) -> "PipelineConfig":
        resolved_pipeline_root = pipeline_root.resolve()
        config = cls(
            pipeline_root=resolved_pipeline_root,
            corpus_pdf_dir=_resolve_path_under_root(
                corpus_pdf_dir or _resolve_corpus_pdf_dir(resolved_pipeline_root),
                resolved_pipeline_root,
            ),
            candidate_claims_path=_resolve_path_under_root(
                candidate_claims_path
                or _resolve_candidate_claims_path(resolved_pipeline_root),
                resolved_pipeline_root,
            ),
        )
        return _apply_env_overrides(config)


def _resolve_corpus_pdf_dir(pipeline_root: Path) -> Path:
    preferred_names = (
        "corpus",
        "pdfs",
        "PDFs",
        "mineru_json",
        "source_pdfs",
        "source_json",
    )
    for name in preferred_names:
        candidate = pipeline_root / name
        if candidate.exists():
            return candidate
    return pipeline_root / "corpus"


def _resolve_candidate_claims_path(pipeline_root: Path) -> Path:
    preferred_names = (
        "候选命题清单.txt",
        "seed_claims_input.txt",
        "candidate_claims.txt",
        "claims.txt",
        "高频共识候选清单.txt",
    )
    for name in preferred_names:
        candidate = pipeline_root / name
        if candidate.exists():
            return candidate

    txt_candidates = sorted(
        path for path in pipeline_root.glob("*.txt") if path.is_file()
    )
    for candidate in txt_candidates:
        lowered = candidate.name.lower()
        if "claim" in lowered or "命题" in candidate.name or "共识" in candidate.name:
            return candidate
    if txt_candidates:
        return txt_candidates[0]
    return pipeline_root / "候选命题清单.txt"


def _resolve_path_under_root(path: Path, pipeline_root: Path) -> Path:
    return path if path.is_absolute() else pipeline_root / path


def _apply_env_overrides(config: PipelineConfig) -> PipelineConfig:
    overridden = config
    int_overrides = {
        "PIPELINE_CLAIM_DISCOVERY_CHUNK_WORKERS": "claim_discovery_chunk_workers",
        "PIPELINE_CLAIM_DISCOVERY_DOC_WORKERS": "claim_discovery_doc_workers",
        "PIPELINE_CLAIM_DISCOVERY_PROVIDER_MAX_ATTEMPTS": "claim_discovery_provider_max_attempts",
        "PIPELINE_STEP3B_LLM_JUDGE_BATCH_SIZE": "step3b_llm_judge_batch_size",
        "PIPELINE_STEP3B_LLM_CLUSTER_BATCH_SIZE": "step3b_llm_cluster_batch_size",
        "PIPELINE_STEP3B_LLM_VERIFIER_BATCH_SIZE": "step3b_llm_verifier_batch_size",
        "PIPELINE_STEP3B_LLM_VERIFIER_WORKERS": "step3b_llm_verifier_workers",
        "PIPELINE_STEP3B_LLM_VERIFIER_MAX_ATTEMPTS": "step3b_llm_verifier_max_attempts",
        "PIPELINE_CLAIM_DISCOVERY_BATCH_CHUNKS": "claim_discovery_batch_chunks",
        "PIPELINE_CLAIM_DISCOVERY_BATCH_CHARS": "claim_discovery_batch_chars",
        "PIPELINE_CLAIM_DISCOVERY_CHUNK_BUDGET": "claim_discovery_chunk_budget",
        "PIPELINE_CLAIM_DISCOVERY_DOC_BUDGET": "claim_discovery_doc_budget",
        "PIPELINE_CLAIM_DISCOVERY_CHUNK_MAX_CLAIMS": "claim_discovery_chunk_max_claims",
        "PIPELINE_CLAIM_DISCOVERY_DOC_MAX_CLAIMS": "claim_discovery_doc_max_claims",
    }
    for env_name, field_name in int_overrides.items():
        raw_value = os.getenv(env_name)
        if raw_value is None or not raw_value.strip():
            continue
        try:
            parsed = int(raw_value.strip())
        except ValueError:
            continue
        overridden = replace(overridden, **{field_name: parsed})

    float_overrides = {
        "PIPELINE_CLAIM_DISCOVERY_PROVIDER_RETRY_BACKOFF_SECONDS": "claim_discovery_provider_retry_backoff_seconds",
        "PIPELINE_STEP3B_LLM_VERIFIER_RETRY_SLEEP_SECONDS": "step3b_llm_verifier_retry_sleep_seconds",
    }
    for env_name, field_name in float_overrides.items():
        raw_value = os.getenv(env_name)
        if raw_value is None or not raw_value.strip():
            continue
        try:
            parsed = float(raw_value.strip())
        except ValueError:
            continue
        overridden = replace(overridden, **{field_name: parsed})

    bool_overrides = {
        "PIPELINE_SKIP_DOCUMENT_CLAIM_EXTRACTION": "skip_document_claim_extraction",
    }
    for env_name, field_name in bool_overrides.items():
        raw_value = os.getenv(env_name)
        if raw_value is None or not raw_value.strip():
            continue
        parsed = raw_value.strip().lower() in {"1", "true", "yes", "on"}
        overridden = replace(overridden, **{field_name: parsed})
    return overridden
