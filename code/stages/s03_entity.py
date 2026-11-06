from runtime.artifacts import read,table
from shared.storage import read_csv
from preparation.entity_selection import rank_candidates

def run(ctx):
    ranked,recount=rank_candidates(read_csv(ctx.upstream(1,'body_paragraphs.csv')),read(ctx.upstream(2,'candidates.json')))
    if recount['status']!='nominated':raise ValueError('Unresolved entity nomination')
    table(ctx.pending/'entity_ranking.csv',ranked)
    ctx.emit('entity_recount.json',recount)
    ctx.emit('entity_selection.json',recount)
