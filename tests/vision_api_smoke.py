"""Live multi-image, format, URL, Responses continuation and isolation checks.

uv run python tests/vision_api_smoke.py
"""
import asyncio
import argparse
import base64
import json
from pathlib import Path
import time

import httpx
from PIL import Image
import mcp_smoke as common
from vision_smoke import picture


def url(path, fmt):
    import io
    buffer = io.BytesIO()
    with Image.open(path) as image:
        image.save(buffer, format=fmt)
    mime = {'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp','GIF':'image/gif'}[fmt]
    return 'data:' + mime + ';base64,' + base64.b64encode(buffer.getvalue()).decode()


def body(protocol, urls):
    content = [{'type':'input_text' if protocol == 'responses' else 'text',
                'text':'Transcribe the four-character code at the bottom of each image, in image order. Return only the codes separated by commas.'}]
    for address in urls:
        if protocol == 'responses':
            content.append({'type':'input_image','image_url':address})
        elif protocol == 'messages':
            header, data = address.split(',',1)
            content.append({'type':'image','source':{'type':'base64','media_type':header[5:].split(';')[0],'data':data}})
        else:
            content.append({'type':'image_url','image_url':{'url':address}})
    return {'model':common.MODEL, 'max_tokens':2000, 'input' if protocol == 'responses' else 'messages':[{'role':'user','content':content}]}


def text_of(value, protocol):
    if protocol == 'chat':
        return value.get('choices',[{}])[0].get('message',{}).get('content','') or ''
    if protocol == 'messages':
        return ''.join(p.get('text','') for p in value.get('content',[]))
    return ''.join(p.get('text','') for o in value.get('output',[]) for p in o.get('content',[]))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--structured-only', action='store_true')
    args = parser.parse_args()
    folder = common.ROOT / '.preview' / time.strftime('vision-api-%Y%m%d-%H%M%S')
    folder.mkdir(parents=True, exist_ok=False)
    values = {}
    for line in (common.ROOT / '.test.env').read_text(encoding='utf-8-sig').splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            k,v = line.split('=',1)
            values[k.strip()] = v.strip().strip('\"\'')
    base, key = values['BASE_URL'].rstrip('/').removesuffix('/v1'), values['API_KEY']
    fixtures = [picture(folder / f'image-{i}.png', i) for i in (0,1)]
    codes = [v['code'] for v in fixtures]
    rows = []
    async with httpx.AsyncClient(timeout=660, trust_env=False, headers={'Authorization':'Bearer '+key}) as client:
        async def check(name, protocol, payload, expected, absent=(), status=200):
            endpoint = '/v1/' + ('chat/completions' if protocol == 'chat' else protocol)
            started = time.monotonic()
            response = await client.post(base + endpoint, json=payload)
            value = response.json()
            output = text_of(value, protocol)
            positions = [output.find(code) for code in expected]
            passed = (response.status_code == status and all(p >= 0 for p in positions) and
                      positions == sorted(positions) and all(code not in output for code in absent))
            rows.append({'name':name,'protocol':protocol,'status':response.status_code,'passed':passed,
                         'seconds':round(time.monotonic()-started,1),'text':output,'error':value.get('error')})
            common.write_json(folder / 'results.json', rows)
            print(name, response.status_code, passed, output[:200], flush=True)
            return value
        for protocol, formats in [('chat',('JPEG','GIF')),('responses',('PNG','WEBP')),('messages',('PNG','PNG'))]:
            urls = [url(folder / f'image-{i}.png', fmt) for i,fmt in enumerate(formats)]
            if not args.structured_only:
                parent = await check('multi_image_' + '_'.join(formats), protocol, body(protocol, urls), codes)
                if protocol == 'responses' and parent.get('id'):
                    await check('previous_response_id_images', protocol, {'model':common.MODEL,'previous_response_id':parent['id'],
                                'input':'Transcribe only the code from the SECOND image in the earlier message.'}, [codes[1]], [codes[0]])
            if protocol in ('chat', 'responses'):
                schema = {'name':'image_codes','strict':True,'schema':{'type':'object','properties':{
                    'codes':{'type':'array','items':{'type':'string'},'minItems':2,'maxItems':2}},'required':['codes'],'additionalProperties':False}}
                payload = body(protocol, urls)
                if protocol == 'chat':
                    payload['response_format'] = {'type':'json_schema','json_schema':schema}
                else:
                    payload['text'] = {'format':{'type':'json_schema',**schema}}
                await check('images_json_schema', protocol, payload, codes)
        if args.structured_only:
            print('Artifacts:', folder, flush=True)
            assert all(row['passed'] for row in rows)
            return
        await asyncio.gather(*[check('parallel_isolation_' + str(i), 'chat', body('chat',[url(folder / f'image-{i}.png','PNG')]),
                                  [codes[i]], [codes[1-i]]) for i in (0,1)])
        public = 'https://raw.githubusercontent.com/github/explore/main/topics/python/python.png'
        payload = body('chat',[public])
        payload['messages'][0]['content'][0]['text'] = 'Which programming language is represented by this logo? Reply only with the language name.'
        await check('public_https_url', 'chat', payload, ['Python'])
        await check('private_url_rejected', 'chat', body('chat',['http://127.0.0.1:80/private']), [], status=400)
    print('Artifacts:', folder, flush=True)
    assert all(row['passed'] for row in rows)


if __name__ == '__main__':
    asyncio.run(main())
