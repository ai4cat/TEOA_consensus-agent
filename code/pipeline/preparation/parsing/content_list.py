"""Normalize nested MinerU Content List V2 to the existing flat text schema."""

def normalize_content_list(value):
    if not (isinstance(value, list) and any(isinstance(page, list) for page in value)):
        return value
    if not all(isinstance(page, list) for page in value):
        raise ValueError('Mixed flat/nested MinerU content list')
    result = []

    def text(item):
        if isinstance(item, str): return item
        if isinstance(item, list): return ''.join(text(v) for v in item)
        if isinstance(item, dict): return text(item.get('content', item.get('item_content', '')))
        return ''

    for page, blocks in enumerate(value):
        for index, block in enumerate(blocks):
            if not isinstance(block, dict): continue
            kind = block.get('type')
            content = block.get('content', {})
            if kind not in {'paragraph', 'title', 'reference', 'list'}: continue
            if not isinstance(content, dict): continue
            common = dict(page_idx=page, bbox=block.get('bbox', []))
            if kind == 'list':
                reference = content.get('list_type') == 'reference_list'
                if reference:
                    # A local reference-list label must not open a global
                    # References section: MinerU can label author lists this way.
                    values = [text(item) for item in content.get('list_items', content.get('list_content', []))]
                    result.append(dict(type='list', sub_type='ref_text', list_items=[s for s in values if s.strip()], **common))
                    continue
                for item in content.get('list_items', content.get('list_content', [])):
                    value_text = text(item)
                    if value_text.strip():
                        result.append(dict(type='ref_text' if reference else 'text', text=value_text, **common))
                continue
            key = {'paragraph':'paragraph_content', 'title':'title_content', 'reference':'reference_content'}[kind]
            value_text = text(content.get(key, []))
            if not value_text.strip(): continue
            if kind == 'reference':
                result.append(dict(type='list',sub_type='ref_text',list_items=[value_text],**common))
                continue
            row = dict(type='text', text=value_text, **common)
            if kind == 'title': row['text_level'] = content.get('level', 1)
            result.append(row)
    if not result: raise ValueError('V2 content has no supported text blocks')
    return result
