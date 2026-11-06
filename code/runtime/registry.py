STAGE_LAYOUT = 'linear-stages.v2'
NAMES = {1: 'corpus', 2: 'candidates', 3: 'entity', 4: 'claims', 5: 'clusters', 6: 'support', 7: 'themes', 8: 'summaries', 9: 'library', 10: 'scores', 11: 'ranking', 12: 'systems', 13: 'export'}
DEPENDENCIES = {1: [], 2: [1], 3: [1, 2], 4: [1, 3], 5: [1, 3, 4], 6: [1, 3, 4, 5], 7: [1, 3, 4, 5, 6], 8: [1, 3, 4, 7], 9: [1, 3, 4, 7], 10: [1, 3, 4, 7, 8, 9], 11: [4, 7, 10], 12: [3], 13: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]}
EXECUTION_ORDER = tuple(range(1, 14))
MODEL_STAGES = {2, 4, 5, 8, 10, 12}
NETWORK_STAGES = MODEL_STAGES | {9}
INTERNAL = {8: 'document-abstractions', 4: 'claims-3A', 5: 'claims-3B', 10: 'audit', 12: 'systems'}

def folder(number):
    return f'{number:02d}_{NAMES[number]}'

def artifact_directory(run, number):
    return run / 'outputs' / folder(number)
