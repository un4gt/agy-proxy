"""Narrow, version-pinned cleanup of the restricted CLI agent's input envelope.

Does not change transport, routing, safetySettings, generationConfig, client
messages, client tool schemas, or the CLI's execution permissions.
"""
from copy import deepcopy
import hashlib
import json
import re

SUPPORTED_SHA256 = '60e72242f07c131f75afcd9a9e425d6ee097a1a1b3190ea98ae78fa4180bbfa8'
FINISH_DESCRIPTION = ('Submit the next client assistant turn, including requests for client tools. '
                      'The client executes requested tools and supplies their real results in a later turn. '
                      'Submitting a tool request does not mean the overall task is complete.')


class InputCleanError(ValueError):
    def __init__(self, message, *, code):
        super().__init__(message)
        self.code = code


def clean_request(doc, *, image_paths=()):
    if doc.get('requestType') != 'agent':
        return deepcopy(doc)
    result = deepcopy(doc)
    request = result['request']
    parts = request['systemInstruction']['parts']
    if len(parts) != 1 or set(parts[0]) != {'text'}:
        raise ValueError('Unsupported system instruction shape')
    text = parts[0]['text']
    boundary = '\n</System Prompt>\n'
    custom, separator, tail = text.rpartition(boundary)
    if not separator or not custom.startswith('<System Prompt>\nYou are the reasoning backend for an external coding client.'):
        raise ValueError('Not the restricted proxy agent')
    custom = custom.removeprefix('<System Prompt>\n')
    # Recognize the whole CLI suffix; never heuristically erase arbitrary text.
    blocks = re.fullmatch(
        r'<user_information>\n.*?\n</user_information>\n'
        r'<messaging>\n.*?\n</messaging>\n'
        r'<artifacts>\n.*?\n</artifacts>\n'
        r'(?P<guidelines><guidelines>\n.*?\n</guidelines>)\n'
        r'<communication_style>\n.*?\n</communication_style>', tail, re.DOTALL)
    if blocks is None:
        raise ValueError('Unknown CLI system suffix')
    parts[0]['text'] = custom + '\n' + blocks['guidelines']
    declarations = [f for group in request['tools'] for f in group['functionDeclarations']]
    if (not isinstance(image_paths, (list, tuple)) or len(image_paths) > 8
            or any(not isinstance(path, str) or not path.startswith('/') for path in image_paths)):
        raise ValueError('Invalid image attachment allowlist')
    expected = ['finish', 'manage_task', 'view_file'] if image_paths else ['finish', 'manage_task']
    if sorted(f['name'] for f in declarations) != expected:
        raise ValueError('Unexpected native tool declarations')
    finish = next(f for f in declarations if f['name'] == 'finish')
    finish['description'] = FINISH_DESCRIPTION
    properties = finish['parameters']['properties']
    # Retain required CLI metadata fields, but remove misleading local-tool examples.
    for key in ('toolAction', 'toolSummary'):
        properties[key]['description'] = 'Short label for this client response.'
    permitted = [finish]
    if image_paths:
        view = next(f for f in declarations if f['name'] == 'view_file')
        original = view['parameters']['properties']
        fields = ('AbsolutePath', 'toolAction', 'toolSummary')
        if any(key not in original for key in fields):
            raise ValueError('Unknown native image reader schema')
        view['description'] = 'Read the supplied image attachment. Only the listed paths are permitted.'
        view['parameters'] = {'type': 'OBJECT', 'properties': {key: original[key] for key in fields},
                              'required': list(fields)}
        view['parameters']['properties']['AbsolutePath'] = {
            'type': 'STRING', 'description': 'Exact supplied image attachment path.', 'enum': list(image_paths)}
        for key in ('toolAction', 'toolSummary'):
            view['parameters']['properties'][key]['description'] = 'Short label for this attachment read.'
        permitted.append(view)
    request['tools'] = [{'functionDeclarations': permitted}]
    # finish is the transport envelope for *every* client assistant response,
    # including plain answers and tool_choice=none. Request this envelope without
    # changing the client's choice of external tools. The Cloud Code route can
    # return text even under ANY; the response still needs independent validation.
    # See docs/native-backend.md for the upstream constraint limitations.
    calling = request.setdefault('toolConfig', {}).setdefault('functionCallingConfig', {})
    calling['mode'] = 'ANY'
    calling['allowedFunctionNames'] = [f['name'] for f in permitted]
    for content in request['contents']:
        if content.get('role') != 'user':
            continue
        for part in content.get('parts', []):
            value = part.get('text', '')
            if not value.startswith('<USER_REQUEST>\n'):
                continue
            user, end, metadata = value.rpartition('\n</USER_REQUEST>')
            if not end or re.fullmatch(
                r'(?:\n<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>\n.*?\n</\1>)*',
                metadata, re.DOTALL) is None:
                raise InputCleanError('Unknown CLI user metadata', code='native_input_user_metadata_invalid')
            part['text'] = user.removeprefix('<USER_REQUEST>\n')
    return result


def rewrite_buffer(data, *, image_paths=()):
    """Shrink JSON in-place and pad with legal whitespace; preserve Go slice ABI."""
    doc = json.loads(data)
    if not isinstance(doc, dict) or doc.get('requestType') != 'agent':
        return None
    encoded = json.dumps(clean_request(doc, image_paths=image_paths), ensure_ascii=False, separators=(',', ':')).encode()
    if len(encoded) > len(data):
        raise ValueError('Clean input exceeds original buffer')
    return encoded + b' ' * (len(data) - len(encoded))


def digest(doc):
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
