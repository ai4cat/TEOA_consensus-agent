"""Scientific table descriptions for the declared input-to-output workflow."""
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class CsvArtifactDoc:
    purpose: str
    script: str

_DOCS = {
    "01_raw_claim_candidates.csv": CsvArtifactDoc("Raw claims with explicit model acceptance and local quote status.", "code/pipeline/claims/extraction.py"),
    "02_structured_claim_pool.csv": CsvArtifactDoc("Verified semantic clusters and their original raw members.", "code/pipeline/claims/clustering.py"),
    "03C_generalization_review.csv": CsvArtifactDoc("Structured claims with source occurrence and frequency fields.", "code/pipeline/claims/clustering.py"),
    "01_support_evidence_rows.csv": CsvArtifactDoc("Source occurrence rows; missing exact anchors remain explicitly labeled.", "code/pipeline/claims/provenance.py"),
    "03_claims_master.csv": CsvArtifactDoc("Final consensus claims with source membership and metadata.", "code/pipeline/claims/clustering.py"),
    "04_consensus_claims.csv": CsvArtifactDoc("Final consensus themes and distinct-document frequencies.", "code/pipeline/claims/clustering.py"),
    "01_raw_claim_candidates_scores.csv": CsvArtifactDoc("Primary/reference evidence and impact scores for raw claims.", "code/stages/s10_scores.py"),
    "themes_ranking.csv": CsvArtifactDoc("Exact rational frequency times evidence-gap times top-impact ranking.", "code/pipeline/selection/ranking.py"),
}

def get_csv_artifact_doc(path: Path) -> CsvArtifactDoc | None:
    return _DOCS.get(path.name)

def render_csv_comment_block(doc: CsvArtifactDoc) -> list[str]:
    return ["Purpose: " + doc.purpose, "Implementation: " + doc.script,
            "Input definitions and computation scope: inputs/00_workflow_definition.json."]
