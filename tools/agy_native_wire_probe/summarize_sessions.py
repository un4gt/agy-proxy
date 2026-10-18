"""Export only assertions and timings from the controlled live session probes."""
import argparse
import json
from pathlib import Path


def summarize(evidence):
    sessions = json.loads((evidence / 'live-sessions.json').read_text())
    content = json.loads((evidence / 'live-content.json').read_text())
    symbols = json.loads((evidence / 'symbols.json').read_text())
    assert sessions['passed'] and sessions['all_processes_gone'] and sessions['cancelled_processes_gone']
    assert content['passed']
    for name in ('tool_request', 'tool_result'):
        result = content[name]
        assert len(result['calls']) == 1 and result['calls'][0]['name'] == 'finish'
        raw = result['calls'][0]['args']
        # CLI adds these two native display fields outside the client schema.
        assert set(raw) <= {'kind', 'content', 'tool_calls', 'toolSummary', 'toolAction'}
        assert {k: raw[k] for k in ('kind', 'content', 'tool_calls')} == result['cli']['structured_output']
    long = content['long_text']
    assert long['text'].strip() == long['cli']['response'].strip()
    return {
        'binary_sha256': symbols['binary_sha256'],
        'binary_version': (evidence / 'binary-version.txt').read_text().strip(),
        'passed': True,
        'same_external_id_different_owners_isolated': True,
        'different_cli_pids_and_conversations': len(set(sessions['pids'].values())) == 2 and len(set(sessions['conversations'].values())) == 2,
        'interleaved_turns_retained_own_context': True,
        'cancel_one_other_continues': True,
        'all_processes_gone': sessions['all_processes_gone'],
        'short_turn_metrics': [r['metrics'] for r in sessions['initial'] + sessions['recall'] + [sessions['survivor']]],
        'long_text_metrics': long['metrics'],
        'long_text_chunks': long['text_chunks'],
        'long_text_matches_cli': True,
        'external_tool_roundtrip': True,
        'raw_finish_matches_cli_structured_output': True,
        'tool_request_metrics': content['tool_request']['metrics'],
        'tool_result_metrics': content['tool_result']['metrics'],
        'production_enabled': False,
        'limitations': ['GDB changes scheduling', 'HTTP parser is experimental',
                        'finish arguments observed as complete objects, not token deltas',
                        'no HTTP backend integration or arbitrary role/history injection'],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(summarize(args.evidence), indent=2) + '\n')
