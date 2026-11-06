"""One source-membership scope for summaries, citations and impact scoring."""

from runtime.artifacts import read
from shared.storage import read_csv

POLICY = 'final-theme-members.v1'


def select_scoring_claims(raw, themes):
    indexed = {}
    for row in raw:
        identifier = str(row.get('raw_claim_id', '')).strip()
        if not identifier or identifier in indexed:
            raise ValueError('Missing or duplicate raw claim identity')
        indexed[identifier] = row
    members = set()
    for theme in themes:
        ids = [x.strip() for x in str(theme.get('source_raw_claim_ids', '')).split(';') if x.strip()]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError('Final theme has empty or duplicate source membership')
        members.update(ids)
    if members - indexed.keys():
        raise ValueError('Final themes reference unknown raw claims')
    selected = [indexed[i] for i in sorted(members)]
    for row in selected:
        if str(row.get('stage3a_accepted', '')).lower() != 'true' or not str(row.get('doc_id', '')).strip():
            raise ValueError('Final-theme member must have an accepted source and document identity')
    return selected


def describe_scope(claims):
    return {'policy': POLICY, 'raw_claim_ids': sorted(r['raw_claim_id'] for r in claims),
            'doc_ids': sorted({r['doc_id'] for r in claims})}


def validate_summary_scope(run):
    claims = select_scoring_claims(
        read_csv(run/'outputs/04_claims/01_raw_claim_candidates.csv'),
        read_csv(run/'outputs/07_themes/04_consensus_claims.csv'))
    path = run/'outputs/08_summaries/summary_scope.json'
    if not path.is_file():
        raise ValueError('Stage 8 summaries predate final-theme scoping; regenerate Stage 8')
    expected = describe_scope(claims)
    actual = read(path)
    if any(actual.get(key) != value for key, value in expected.items()):
        raise ValueError('Stage 8 summary scope does not match the current final themes')
    rows = read_csv(run/'outputs/08_summaries/document_summaries.csv')
    ids = [r['doc_id'] for r in rows]
    if len(ids) != len(set(ids)) or set(ids) != set(expected['doc_ids']):
        raise ValueError('Stage 8 summary documents do not exactly cover scoring sources')
    return claims
