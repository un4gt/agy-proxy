import json
from pathlib import Path

root=Path('/evidence')
events=[json.loads(line) for line in (root/'hook-events.jsonl').read_text().splitlines()]
runs=json.loads((root/'runs.json').read_text())
fingerprints=json.loads((root/'tls-fingerprints.json').read_text())
primary=[event for event in events if event['kind']=='generation_request' and event['request_type']=='agent']
assert len(primary)==1
conn=primary[0]['conn']
parts=[event for event in events if event['kind']=='sse' and event['conn']==conn]
text=''.join(part.get('text') or '' for event in parts for candidate in event['candidates'] for part in candidate['parts'] if not part.get('thought'))
def result(path):
    return next(value['result'] for line in path.read_text().splitlines() if (value:=json.loads(line)).get('event')=='result')
baseline=result(root/'baseline.driver.log')
hooked=result(root/'hooked.stdout.jsonl')
timeline=json.loads((root/'hooked.timeline.json').read_text())
final_time=next(item['monotonic'] for item in timeline if item['event']=='result')
first_text=next(event['monotonic'] for event in parts if any(part.get('text') and not part.get('thought') for c in event['candidates'] for part in c['parts']))
summary={
    'binary_sha256':events[0]['binary_sha256'],
    'runs':runs,
    'baseline_status':baseline['status'], 'hooked_status':hooked['status'],
    'expected_text':'NATIVE_WIRE_OK_20261018',
    'raw_stream_matches_cli':text.strip()==hooked['response'].strip()==baseline['response'].strip()=='NATIVE_WIRE_OK_20261018',
    'main_sse_events':len(parts),
    'finish_reason':parts[-1]['candidates'][0]['finishReason'],
    'raw_usage':parts[-1]['usage'], 'cli_usage':hooked['usage'],
    'first_raw_text_before_cli_result_ms':round((final_time-first_text)*1000,2),
    'request_types':[e['request_type'] for e in events if e['kind']=='generation_request'],
    'http2_requests':sum(e['kind']=='request' and e.get('protocol')=='h2' for e in events),
    'http1_requests':sum(e['kind']=='request' and e.get('protocol')!='h2' for e in events),
    'hook_errors':[e for e in events if e['kind'] in ('hook_error','orphan_data','sse_invalid','request_parse_error','response_parse_error')],
    'tls_hello_counts':{mode:len(fingerprints[mode]) for mode in ('baseline','hooked')},
    'same_unique_client_hello_structures':fingerprints['same_unique_client_hello_structures'],
    'observed_exit_codes':[e['code'] for e in events if e['kind']=='cli_exit'],
}
summary['passed']=all([
    all(r['exit']==0 and (r['mode']=='baseline' or r['inferior_gone']) for r in runs),
    summary['observed_exit_codes']==[0],
    baseline['status']==hooked['status']=='SUCCESS',summary['raw_stream_matches_cli'],
    summary['finish_reason']=='STOP',not summary['hook_errors'],summary['same_unique_client_hello_structures'],
    summary['first_raw_text_before_cli_result_ms']>0,
    parts[-1]['usage']['totalTokenCount']==hooked['usage']['total_tokens'],
])
(root/'summary.json').write_text(json.dumps(summary,indent=2))
print(json.dumps(summary,indent=2))
assert summary['passed']
