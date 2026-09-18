"""Exact runtime73b structural boundary, without adding V5 field validators."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest

from test_arena_codex import (arena_operations, lab, ICP, PARAGRAPH, runtime, REAL_POPEN,
                             scenario, capture_accepted_review, review_findings,
                             incremental_checkpoint_scenario, confirmed_leads)
from tyche_arena.output import projected_payload, public_url, publish


@pytest.mark.parametrize('value,allowed', [
    ('x' * 4095, True), ('x' * 4096, True), ('x' * 4097, False),
    ('界' * 1365, True), ('界' * 1365 + 'a', True), ('界' * 1366, False),
    ('界' * 1665, False), ('with\tallowed\nwhitespace\r', True),
    ('back\bspace', False), ('null\x00byte', False), ('delete\x7f', False),
    ('\ud800', False), ('\udfff', False),
    (2 ** 53, True), (2 ** 53 + 1, False), (-(2 ** 53), True), (-(2 ** 53) - 1, False),
    (True, True), (None, True), (1e308, True), (float('nan'), False), (float('inf'), False),
    ([None] * 200, True), ([None] * 201, False), ((None,), True),
    ({str(i): 0 for i in range(64)}, True), ({str(i): 0 for i in range(65)}, False),
    ({1: 0}, False), (complex(1, 2), False),
])
def test_guard_matches_actual_output_limits(arena_operations, value, allowed):
    contracts = importlib.import_module('lab_arena.contracts')
    document = {'companies': [{'state': value}]}
    if allowed:
        payload = projected_payload(document['companies'], ['example.com'])
        contracts.check_strict_document(json.loads(payload), contracts.OUTPUT_LIMITS)
    else:
        with pytest.raises(contracts.ArenaContractError):
            contracts.check_strict_document(document, contracts.OUTPUT_LIMITS)
        with pytest.raises(ValueError, match='Arena output'):
            projected_payload(document['companies'], ['example.com'])


@pytest.mark.parametrize('depth,allowed', [(5, True), (6, False)])
def test_depth_includes_the_actual_output_envelope(arena_operations, depth, allowed):
    value = None
    for _ in range(depth): value = {'a': value}
    contracts = importlib.import_module('lab_arena.contracts')
    document = {'companies': [{'state': value}]}
    if allowed:
        contracts.check_strict_document(json.loads(projected_payload(document['companies'])), contracts.OUTPUT_LIMITS)
    else:
        with pytest.raises(contracts.ArenaContractError): contracts.check_strict_document(document, contracts.OUTPUT_LIMITS)
        with pytest.raises(ValueError, match='nesting depth 9 exceeds 8'): projected_payload(document['companies'])


def test_both_canonical_and_emitted_byte_limits(arena_operations):
    rows = [{'state': {str(i): 'x' * 4096 for i in range(64)}}] * 2
    with pytest.raises(ValueError, match='canonical document length .* exceeds 524288 bytes'):
        projected_payload(rows)
    escaped = [{'state': {str(i): '界' * 1000 for i in range(64)}}] * 2
    contracts = importlib.import_module('lab_arena.contracts')
    contracts.check_strict_document({'companies': escaped}, contracts.OUTPUT_LIMITS)
    with pytest.raises(ValueError, match='encoded output length .* exceeds 524288 bytes'):
        projected_payload(escaped)


def test_repair_error_identifies_target_path_and_size_without_evidence():
    secret = 'PRIVATE_EVIDENCE_SENTINEL'
    rows = [{'required_attribute': {'evidence_quote': secret + 'x' * (4097 - len(secret))}}]
    with pytest.raises(ValueError) as raised:
        projected_payload(rows, ['example.com'])
    message = str(raised.value)
    assert 'candidate 1 target "example.com"' in message
    assert '$.companies[0].required_attribute.evidence_quote' in message
    assert '4097 bytes exceeds 4096 bytes' in message
    assert 'shorter exact supported quote from the same saved source ref' in message
    assert secret not in message


def test_arbitrary_invalid_key_is_not_copied_into_error():
    secret = 'PRIVATE_EVIDENCE_SENTINEL' + 'x' * 4096
    with pytest.raises(ValueError) as raised:
        projected_payload([{'state': {secret: 1}}], ['example.com'])
    assert secret not in str(raised.value)
    assert '$.companies[0].state.[key0]' in str(raised.value)


@pytest.mark.parametrize('url', ['https://foo..com/about', 'https://例子..中国/about'])
def test_empty_hostname_label_matches_host_rejection(arena_operations, url):
    from qualification.competition_models import public_http_url
    with pytest.raises(ValueError): public_http_url(url)
    with pytest.raises(ValueError, match='IDNA-encodable'): public_url(url)


def test_valid_idn_is_preserved(arena_operations):
    from qualification.competition_models import public_http_url
    url = 'https://例子.中国/about'
    assert public_http_url(url) == url
    assert public_url(url) == url


def install_actual_writer(lab, monkeypatch):
    reference = Path(os.environ['LAB_ARENA_REFERENCE_SOURCE'])
    spec = importlib.util.spec_from_file_location(
        'boundary_checkpoint_reference', reference / 'lab_arena/lab_arena_checkpoint.py')
    checkpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checkpoint)
    current = sys.modules['lab_arena_checkpoint']
    monkeypatch.setitem(sys.modules, 'lab_arena_checkpoint', SimpleNamespace(
        write=lambda rows: checkpoint.write(rows, output_path=lab.output),
        quota_usage=current.quota_usage, QuotaUnavailable=current.QuotaUnavailable))


def assert_actual_host_accepts(path, count):
    reference = Path(os.environ['LAB_ARENA_REFERENCE_SOURCE'])
    validator = (
        'import json,sys; from pathlib import Path; '
        'from lab_arena.output import output_document_from_bytes; '
        "doc=output_document_from_bytes(Path(sys.argv[1]).read_bytes(), "
        "expected_schema_version='leadpoet.lab_arena.output.v5'); "
        "print(json.dumps({'count':len(doc['companies'])}))")
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(reference) + os.pathsep + environment.get('PYTHONPATH', '')
    with pytest.MonkeyPatch.context() as actual_process:
        actual_process.setattr(subprocess, 'Popen', REAL_POPEN)
        parsed = subprocess.run([sys.executable, '-c', validator, str(path)],
                                capture_output=True, text=True, env=environment, timeout=20)
    assert parsed.returncode == 0, parsed.stderr
    assert json.loads(parsed.stdout) == {'count': count}


@pytest.mark.parametrize('defect', ['quote4097', 'quote_control', 'unicode_intent',
                                   'nested_state', 'large_integer', 'signal4097'])
def test_native_review_repairs_saved_evidence_before_actual_host_write(lab, monkeypatch, defect):
    install_actual_writer(lab, monkeypatch)
    captured = []
    completed = []
    original = lab.provider
    short_quote = 'Example Products manufactures packaged goods, tools and accessories for retailers.'

    def provider(parameters):
        body = original(parameters)
        if parameters['tool'] == 'generic_http_request':
            if defect == 'quote4097':
                body['results'][0]['markdown'] = short_quote + 'x' * (4097 - len(short_quote))
            if defect == 'quote_control':
                body['results'][0]['markdown'] = short_quote + '\b'
        return body

    def program():
        native = capture_accepted_review(captured)
        command = next(native)
        while True:
            if command[0] == 'tyche_review':
                for row in command[1].get('companies', []):
                    if row.get('decision') == 'qualify_account':
                        if defect == 'unicode_intent': row['intent_details'] = '界' * 1665
                        if defect == 'signal4097':
                            row['qualification_checks'][2]['claim'] = 'Connected the acquired warehouse. ' + 'x' * (4097 - 33)
                    if row.get('decision') == 'accept':
                        if defect == 'large_integer': row['company'] = {'hq_state': 2 ** 53 + 1}
                        if defect == 'nested_state':
                            nested = 'fixture'
                            for _ in range(10): nested = {'a': nested}
                            row['company'] = {'hq_state': nested}
            result = yield command
            try: command = native.send(result)
            except StopIteration: return

    def repair_and_approve(tools):
        assert captured[0]['status'] == 'needs_repair', captured
        document = tools.research._document()
        assert confirmed_leads.status(tools.research.path, document)['confirmed_count'] == 0
        assert not lab.output.exists()
        errors = captured[0]['errors']
        assert any('example.com' in error for error in errors)
        assert 'PRIVATE_EVIDENCE' not in json.dumps(errors)
        calls = len(lab.frames)
        blocked = tools.call('tyche_checkpoint', {})
        assert blocked['status'] == 'needs_repair'
        row = document['accepted'][0]
        checks = []
        for index, check in enumerate(row['qualification_checks']):
            proof = check['evidence'][0]
            source = proof['source']
            selected = {'ref': source['route_id'] + ':' + str(1 if index == 2 else 0)}
            if index < 2: selected['text'] = short_quote
            else: selected['event_date'] = '2026-08-12'
            checks.append({'requirement_ref': ('icp:industries', 'attribute:0', 'signal:0')[index],
                           'status': 'pass', 'claim': ('Manufactures consumer products',
                           'Manufactures consumer products for retailers',
                           'Connected an acquired warehouse to a shared WMS')[index],
                           'evidence': [selected]})
        repaired = tools.call('tyche_review', {'companies': [{
            'target': 'example.com', 'decision': 'accept', 'reason': 'Repair saved output fields',
            'company': {'hq_state': 'Ohio'}, 'intent_details': PARAGRAPH,
            'qualification_checks': checks}]})
        assert repaired['status'] == 'review_required', repaired
        assert not lab.output.exists()
        saved = tools.call('tyche_review', {'review_ref': repaired['review_ref'],
            'review_findings': review_findings(repaired, tools)})
        assert saved['checkpoint_saved']
        assert len(lab.frames) == calls  # saved refs only: no paid replay
        assert_actual_host_accepts(lab.output, 1)
        assert json.loads(lab.output.read_text())['companies'][0]['required_attribute']['evidence_quote'] == short_quote
        completed.append(True)

    lab.provider = provider
    lab.program = program
    lab.after_program = repair_and_approve
    lab.mode = 'partial_timeout'
    monkeypatch.setenv('LAB_ARENA_COMPANY_LIMIT', '5')
    assert len(runtime.run(ICP)) == 1
    assert completed == [True]


def test_invalid_pending_keeps_unchanged_snapshot_and_revokes_changed_confirmed_row(lab, monkeypatch):
    install_actual_writer(lab, monkeypatch)
    lab.program = lambda: incremental_checkpoint_scenario(approve_count=1)
    lab.mode = 'partial_timeout'
    monkeypatch.setenv('LAB_ARENA_COMPANY_LIMIT', '5')
    completed = []

    def invalidate_pending_then_confirmed(tools):
        prior = lab.output.read_bytes()
        snapshots = {name: tools.research.path.with_name(name).read_bytes()
                     for name in ('companies.json', 'checkpoint-results.json')}
        assert_actual_host_accepts(lab.output, 1)
        calls = len(lab.frames)
        def invalidate(target):
            result = tools.call('tyche_review', {'companies': [{
                'target': target, 'decision': 'accept', 'reason': 'Fixture invalid pending field',
                'company': {'hq_state': 2 ** 53 + 1}}]})
            assert result['status'] == 'needs_repair', result
        invalidate('second.example')
        assert lab.output.read_bytes() == prior
        assert all(tools.research.path.with_name(name).read_bytes() == payload
                   for name, payload in snapshots.items())
        assert confirmed_leads.status(tools.research.path, tools.research._document())['confirmed_count'] == 1
        invalidate('example.com')
        assert confirmed_leads.status(tools.research.path, tools.research._document())['confirmed_count'] == 0
        assert json.loads(lab.output.read_text()) == {'companies': []}
        assert len(lab.frames) == calls
        assert_actual_host_accepts(lab.output, 0)
        completed.append(True)

    lab.after_program = invalidate_pending_then_confirmed
    assert runtime.run(ICP) == []
    assert completed == [True]


def test_writer_rechecks_wire_limits_before_any_checkpoint_or_snapshot(tmp_path):
    called = []
    rows = [{'required_attribute': {'evidence_quote': 'x' * 4097}}]
    with pytest.raises(ValueError, match='4097 bytes exceeds 4096 bytes'):
        publish(tmp_path / 'results.json', {'accepted': [{'company': {'domain': 'example.com'}}]},
                rows, {'valid': True}, called.append, partial=True)
    assert called == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('quote_bytes', [4095, 4096])
def test_valid_native_quote_boundary_reaches_actual_host_parser(lab, monkeypatch, quote_bytes):
    install_actual_writer(lab, monkeypatch)
    captured = []
    completed = []
    original = lab.provider
    def provider(parameters):
        body = original(parameters)
        if parameters['tool'] == 'generic_http_request':
            base = body['results'][0]['markdown']
            body['results'][0]['markdown'] = base + 'x' * (quote_bytes - len(base))
        return body
    def approve(tools):
        assert captured[0]['status'] == 'review_required'
        saved = tools.call('tyche_review', {'review_ref': captured[0]['review_ref'],
            'review_findings': review_findings(captured[0], tools)})
        assert saved['checkpoint_saved']
        assert_actual_host_accepts(lab.output, 1)
        completed.append(True)
    lab.provider = provider
    lab.program = lambda: capture_accepted_review(captured)
    lab.after_program = approve
    lab.mode = 'partial_timeout'
    monkeypatch.setenv('LAB_ARENA_COMPANY_LIMIT', '5')
    assert len(runtime.run(ICP)[0]['required_attribute']['evidence_quote'].encode()) == quote_bytes
    assert completed == [True]
