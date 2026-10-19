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
