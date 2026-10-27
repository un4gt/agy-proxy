import copy
import json

import pytest

from tools.agy_native_wire_probe.input_clean import clean_request, rewrite_buffer


@pytest.fixture
def payload():
    suffix = '\n'.join(f'<{tag}>\nCLI {tag}\n</{tag}>' for tag in (
        'user_information', 'messaging', 'artifacts', 'guidelines', 'communication_style'))
    return {'requestType': 'agent', 'project': 'fixture-project', 'requestId': 'request-a',
            'request': {
                'systemInstruction': {'role': 'user', 'parts': [{'text':
                    '<System Prompt>\nYou are the reasoning backend for an external coding client.\n'
                    'Client tool schema and safety instructions remain intact.\n</System Prompt>\n' + suffix}]},
                'contents': [{'role': 'user', 'parts': [{'text':
                    '<USER_REQUEST>\n[Complete client conversation]\n'
                    '[{"role":"system","content":"client policy <user_information>keep me</user_information>"}]'
                    '\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nclock\n</ADDITIONAL_METADATA>'}]}],
                'tools': [{'functionDeclarations': [{'name': 'finish', 'description': 'CLI task completion',
                    'parameters': {'required': ['toolAction', 'toolSummary'], 'properties': {
                        'toolAction': {'type': 'STRING', 'description': 'CLI action ' * 30},
                        'toolSummary': {'type': 'STRING', 'description': 'CLI summary ' * 30}}}},
                    {'name': 'manage_task', 'description': 'CLI background tasks ' * 20}]}],
                'safetySettings': [{'category': 'fixture', 'threshold': 'unchanged'}],
                'generationConfig': {'temperature': 0.3}, 'toolConfig': {'functionCallingConfig': {'mode': 'AUTO'}}}}


def test_only_known_cli_wrapping_changes(payload):
    original = copy.deepcopy(payload)
    result = clean_request(payload)
    assert payload == original
    assert result['project'] == original['project'] and result['requestId'] == original['requestId']
    for key in ('safetySettings', 'generationConfig'):
        assert result['request'][key] == original['request'][key]
    text = result['request']['systemInstruction']['parts'][0]['text']
    assert 'Client tool schema and safety instructions remain intact.' in text
    assert '<guidelines>\nCLI guidelines\n</guidelines>' in text
    assert '<artifacts>' not in text and '<user_information>' not in text
    assert 'client policy <user_information>keep me</user_information>' in result['request']['contents'][0]['parts'][0]['text']
    assert [f['name'] for t in result['request']['tools'] for f in t['functionDeclarations']] == ['finish']


@pytest.mark.parametrize('mode', [None, 'AUTO', 'NONE', 'ANY'])
def test_every_client_response_requires_finish_envelope(payload, mode):
    if mode is None:
        payload['request'].pop('toolConfig')
    else:
        payload['request']['toolConfig']['functionCallingConfig'] = {
            'mode': mode, 'allowedFunctionNames': ['manage_task']}
    original = copy.deepcopy(payload)
    result = json.loads(rewrite_buffer(json.dumps(payload).encode()))
    assert result['request']['toolConfig']['functionCallingConfig'] == {
        'mode': 'ANY', 'allowedFunctionNames': ['finish']}
    assert payload == original
    # Auxiliary generations (titles/checkpoints) are not client turns.
    payload['requestType'] = 'checkpoint'
    assert clean_request(payload) == payload


def test_finish_constraint_preserves_other_tool_configuration(payload):
    payload['request']['toolConfig']['retrievalConfig'] = {'latLng': {'latitude': 1}}
    result = clean_request(payload)
    assert result['request']['toolConfig']['retrievalConfig'] == {'latLng': {'latitude': 1}}


def test_same_buffer_size_valid_json_and_auxiliary_untouched(payload):
    data = json.dumps(payload).encode()
    value = rewrite_buffer(data)
    assert len(value) == len(data)
    assert json.loads(value) == clean_request(payload)
    payload['requestType'] = 'checkpoint'
    assert rewrite_buffer(json.dumps(payload).encode()) is None
    assert clean_request(payload) == payload


@pytest.mark.parametrize('change', ['unknown_system', 'extra_tool', 'unknown_metadata', 'wrong_agent'])
def test_unknown_layout_fails_closed(payload, change):
    if change == 'unknown_system':
        payload['request']['systemInstruction']['parts'][0]['text'] += '\n<new_policy>preserve</new_policy>'
    elif change == 'extra_tool':
        payload['request']['tools'][0]['functionDeclarations'].append({'name': 'unexpected'})
    elif change == 'unknown_metadata':
        payload['request']['contents'][0]['parts'][0]['text'] += '\n<new_metadata>unknown</new_metadata>'
    else:
        payload['request']['systemInstruction']['parts'][0]['text'] = '<System Prompt>\nother agent'
    with pytest.raises(ValueError):
        clean_request(payload)


def test_client_sessions_do_not_share_or_modify_input(payload):
    second = copy.deepcopy(payload)
    second['requestId'] = 'request-b'
    second['request']['contents'][0]['parts'][0]['text'] = second['request']['contents'][0]['parts'][0]['text'].replace('keep me', 'owner B')
    a, b = clean_request(payload), clean_request(second)
    assert 'owner B' not in json.dumps(a)
    assert 'owner B' in json.dumps(b)
    assert a['requestId'] != b['requestId']


def test_image_reader_requires_explicit_allowlist_and_preserves_media(payload):
    view = {'name': 'view_file', 'description': 'Native reader ' * 80,
            'parameters': {'properties': {key: {'type': 'STRING'} for key in
                                         ('AbsolutePath', 'toolAction', 'toolSummary', 'StartLine')}}}
    payload['request']['tools'][0]['functionDeclarations'].append(view)
    parts = [{'inlineData': {'mimeType': 'image/png', 'data': 'fixture-pixels'}},
             {'functionResponse': {'name': 'view_file', 'response': {'output': 'image'}}}]
    payload['request']['contents'].append({'role': 'user', 'parts': parts})
    original = copy.deepcopy(payload)
    with pytest.raises(ValueError, match='tool declarations'):
        clean_request(payload)
    paths = ['/private/request/image-1.png', '/private/request/image-2.png']
    raw = json.dumps(payload).encode()
    rewritten = rewrite_buffer(raw, image_paths=paths)
    cleaned = json.loads(rewritten)['request']
    assert len(rewritten) == len(raw) and payload == original
    assert cleaned['contents'][-1]['parts'] == parts
    assert cleaned['toolConfig']['functionCallingConfig'] == {
        'mode': 'ANY', 'allowedFunctionNames': ['finish', 'view_file']}
    functions = cleaned['tools'][0]['functionDeclarations']
    assert [f['name'] for f in functions] == ['finish', 'view_file']
    schema = functions[1]['parameters']
    assert schema['properties']['AbsolutePath']['enum'] == paths
    assert set(schema['properties']) == {'AbsolutePath', 'toolAction', 'toolSummary'}
    with pytest.raises(ValueError, match='allowlist'):
        clean_request(payload, image_paths=['relative.png'])
    payload['request']['tools'][0]['functionDeclarations'].pop()
    with pytest.raises(ValueError, match='tool declarations'):
        clean_request(payload, image_paths=paths)


@pytest.mark.parametrize('mode', ['Complete client conversation', 'New client messages'])
def test_long_input_keeps_multibyte_content_and_recovery_tail(payload, mode):
    text = '[' + mode + ']\n' + json.dumps([{'role': 'tool', 'content': '中文🙂x' * 40000}], ensure_ascii=False)
    text += '\n[Validation feedback]\n{"required_submission":"finish"}'
    wrapped = '<USER_REQUEST>\n' + text + '\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nclock\n</ADDITIONAL_METADATA>'
    assert len(wrapped.encode()) > 192000
    payload['request']['contents'][0]['parts'][0]['text'] = wrapped
    data = json.dumps(payload, ensure_ascii=False).encode()
    cleaned = json.loads(rewrite_buffer(data))
    assert cleaned['request']['contents'][0]['parts'][0]['text'] == text
    assert len(rewrite_buffer(data)) == len(data)
    # The pre-fix CLI slice destroys both closing wrapper and history; never
    # accept this just because the leading envelope is recognized.
    payload['request']['contents'][0]['parts'][0]['text'] = wrapped.encode()[:192000].decode(errors='ignore') + '\n<truncated 22358 bytes>'
    with pytest.raises(ValueError) as caught:
        clean_request(payload)
    assert caught.value.code == 'native_input_user_metadata_invalid'
