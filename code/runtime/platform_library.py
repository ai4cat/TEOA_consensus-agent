"""Independent full-Lib2 inputs and platform result correspondence."""

from collections import Counter, defaultdict
from pathlib import Path
import json

import jsonschema
from runtime.artifacts import read, save
from shared.storage import read_csv
from stages.s01_corpus import system_payloads
from platforms.support import ELEMENTS, NONMETALS

POLICY = 'independent-full-lib2.v1'
DOCUMENTS = 'inputs/03_platform_documents'
PAYLOAD_ROOT = 'outputs/12_systems/input_tasks'
REQUEST_DEFINITION = 'communications/12_systems/input_definition/prompt_schema.json'


def local_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Missing or invalid platform input: ' + str(relative))
    return path


def platform_plan(root):
    root = Path(root)
    source = root / DOCUMENTS
    documents = read_csv(source / 'catalog.csv')
    ids = [row['doc_id'] for row in documents]
    files = [row['relative_path'] for row in documents]
    if not ids or len(set(ids)) != len(ids) or len(set(files)) != len(files):
        raise ValueError('Platform catalog requires unique document and file identities')
    if set(files) != {p.relative_to(source).as_posix() for p in source.rglob('*.json')}:
        raise ValueError('Platform catalog must cover every Lib2 paper')
    tasks, payloads = [], {}
    for row in sorted(documents, key=lambda r: r['doc_id']):
        doc_id = row['doc_id']
        if not doc_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in doc_id):
            raise ValueError('Invalid platform document identity')
        blocks = read(local_path(source, row['relative_path']))
        if not isinstance(blocks, list) or not blocks:
            raise ValueError('Platform paper has no normalized text blocks')
        parts = list(system_payloads(doc_id, blocks))
        if not parts:
            raise ValueError('Platform paper has no extractable text: ' + doc_id)
        for payload in parts:
            task_id = f"{doc_id}_{payload['part']:03}"
            relative = 'tasks/' + task_id + '.json'
            payloads[relative] = payload
            tasks.append({'task_id': task_id, 'doc_id': doc_id,
                          'blocks': len(payload['blocks']),
                          'characters': sum(len(b['text']) for b in payload['blocks']),
                          'response_file': 'responses/' + task_id + '.json',
                          'payload_file': relative})
    definition = {'policy': POLICY, 'model': 'gpt-5.4-mini',
                  'schema': read(root / 'code/config/system_schema.json'),
                  'documents': sorted(ids), 'tasks': tasks}
    return definition, payloads


def prepare_platform_inputs(root, destination):
    definition, payloads = platform_plan(root)
    destination = Path(destination)
    for name, value in payloads.items():
        save(destination / name, value)
    save(destination / 'extraction_definition.json', definition)
    return definition


def validate_platform_results(ctx):
    planned, payloads = platform_plan(ctx.root)
    source = ctx.upstream(12, 'input_tasks')
    if read(source / 'extraction_definition.json') != planned:
        raise ValueError('Platform task definition differs from full Lib2 inputs')
    if {p.relative_to(source).as_posix() for p in (source / 'tasks').glob('*.json')} != set(payloads):
        raise ValueError('Platform task files differ from full Lib2 coverage')
    for name, payload in payloads.items():
        if read(local_path(source, name)) != payload:
            raise ValueError('Platform task text differs from its Lib2 paper')
    manifest = read(ctx.upstream(12, 'extractions/manifest.json'))
    if manifest.get('tasks') != planned['tasks'] or manifest.get('model') != planned['model']:
        raise ValueError('Platform responses differ from full Lib2 task inventory')
    if (manifest.get('payload_scope') != 'run' or manifest.get('payload_root') != PAYLOAD_ROOT
            or manifest.get('request_definition') != REQUEST_DEFINITION):
        raise ValueError('Platform source or request definition location differs')
    request = read(ctx.run / REQUEST_DEFINITION)
    if request['schema'] != planned['schema']:
        raise ValueError('Platform response schema differs')
    target = json.loads(request['prompt'].splitlines()[0].split(': ', 1)[1])
    selection = read(ctx.upstream(3, 'entity_selection.json'))
    if (target['name'] != selection['selected_entity']
            or set(target['aliases']) != set(selection['selected_aliases'])):
        raise ValueError('Platform target differs from upstream entity selection')
    validator = jsonschema.Draft7Validator(planned['schema'])
    counts = Counter()
    centers, own = defaultdict(set), defaultdict(set)
    response_root = ctx.upstream(12, 'extractions')
    for task in planned['tasks']:
        value = read(local_path(response_root, task['response_file']))
        validator.validate(value)
        counts[task['doc_id']] += 1
        for entity in value['entities']:
            if entity['role'] not in ('catalyst', 'both'):
                continue
            for center in set(entity['active_center_elements']):
                if center in ELEMENTS - NONMETALS:
                    centers[center].add(task['doc_id'])
                    if entity['mention_context'] == 'authors_experiment':
                        own[center].add(task['doc_id'])
    expected_counts = Counter(t['doc_id'] for t in planned['tasks'])
    if counts != expected_counts:
        raise ValueError('Platform response coverage incomplete')
    summary = read(ctx.upstream(12, 'statistics/summary.json'))
    if (summary.get('state') != 'completed' or summary.get('documents') != len(counts)
            or summary.get('fully_processed_documents') != len(counts)
            or summary.get('total_tasks') != len(planned['tasks'])
            or summary.get('completed_tasks') != len(planned['tasks'])):
        raise ValueError('Platform summary coverage differs from full Lib2')
    expected_ranking = [{'element': element, 'documents': len(docs),
                         'authors_experiment_documents': len(own[element]), 'kind': 'metal', 'rank': rank}
                        for rank, (element, docs) in enumerate(
                            sorted(centers.items(), key=lambda x: (-len(x[1]), x[0])), 1)]
    if summary['metal_ranking'] != expected_ranking:
        raise ValueError('Platform ranking differs from source responses')
    actual = read_csv(ctx.upstream(12, 'statistics/metal_center_ranking.csv'))
    if actual != [{k: str(v) for k, v in row.items()} for row in expected_ranking]:
        raise ValueError('Platform ranking table differs from source responses')
    return {'policy': POLICY, 'documents': len(counts), 'tasks': len(planned['tasks']), 'model': planned['model']}
