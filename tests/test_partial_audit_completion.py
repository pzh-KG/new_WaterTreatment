"""Synthetic report/delivery recovery contracts; no provider or scientific gold."""
from __future__ import annotations

import copy

import pytest

from watertreatment.partial_audit_completion import (
    POLICY, PartialAuditAddendum, PartialAuditCompletionRejected,
    build_partial_audit_completion_scope, merge_partial_audit_addendum,
    partial_audit_completion_prompt, validate_partial_audit_addendum,
)
from watertreatment.reading import AuditFinding, SourceAnchor
from watertreatment.storage import digest_json
from test_scientific_revision_audit import fixture, build, report_for


def _progress(scope, *, missing=True):
    pairs = sorted({(a['source_id'], a['block_id']) for a in scope['source_anchors']})
    requirements = []
    for sid, bid in pairs:
        requirements.append({'source_id': sid, 'block_id': bid, 'source_sha256': 'a' * 64,
            'physical_page': None, 'requires_full_view': True,
            'evidence_ids': sorted(a['evidence_id'] for a in scope['source_anchors']
                                   if (a['source_id'], a['block_id']) == (sid, bid))})
    delivered = [{'source_id': sid, 'block_id': bid} for sid, bid in pairs]
    full = copy.deepcopy(delivered[:-1] if missing else delivered)
    result = {'policy': 'bounded-source-delivery-progress-v1', 'call_id': 'synthetic-partial',
        'review_scope_sha256': scope['scope_sha256'], 'source_bundle_sha256': 'b' * 64,
        'requirements': requirements, 'tool_receipts': [], 'delivered': delivered,
        'full_views': full, 'missing_blocks': [],
        'missing_full_views': requirements[-1:] if missing else [], 'complete': not missing,
        'scientific_validation': 'not_established'}
    result['progress_sha256'] = digest_json(result)
    return result


def _inputs(*, source_missing=True, report_missing=True, native_policy=False):
    old, new, baseline = fixture()
    baseline.findings = []  # Do not erase an earlier blocker to manufacture a protocol-only case.
    for record in (old, new):
        record.observations[0].evidence_ids.append('plot')
        record.observations[0].value.evidence_ids.append('plot')
    full = build(old, new, baseline,
                 **({'source_requirement_policy': 'native-word-structure-v1'} if native_policy else {}))
    parent = report_for(full, baseline)
    if report_missing:
        parent.findings = [f for f in parent.findings if f.record_path not in
                           {'observations[0]', 'observations[0].value', 'observations[0].limitations'}]
    progress = _progress(full, missing=source_missing)
    kwargs = dict(original_scope=full, old_record=old, new_record=new,
                  baseline_audit=baseline, source_progress=progress)
    scope = build_partial_audit_completion_scope(full, parent, old_record=old,
        new_record=new, baseline_audit=baseline, source_progress=progress)
    anchors = {a['evidence_id']: a for a in full['source_anchors']}
    findings = []
    for target in scope['targets']:
        evidence = [SourceAnchor(source_id=anchors[eid]['source_id'], block_id=anchors[eid]['block_id'])
                    for eid in target['required_evidence_ids']]
        findings.append(AuditFinding(record_path=target['record_path'],
            record_paths=[target['record_path']], severity='minor', verdict='supported',
            needs_revision=False, explanation='Synthetic explicit disposition; not scientific gold.', evidence=evidence))
    addendum = PartialAuditAddendum(**{k: scope[k] for k in
        ('scope_sha256', 'parent_audit_hash', 'candidate_hash')}, findings=findings)
    return scope, parent, addendum, kwargs


def test_child_and_entity_omissions_are_combined_without_losing_exact_query_obligations():
    scope, parent, addition, kwargs = _inputs()
    target = next(x for x in scope['targets'] if x['record_path'] == 'observations[0]')
    assert {'observations[0].value', 'observations[0].limitations'} <= set(target['covered_paths'])
    assert {'observations[0]', 'observations[0].value', 'observations[0].limitations'} <= set(scope['required_query_paths'])
    assert scope['policy'] == POLICY
    assert validate_partial_audit_addendum(scope, addition, parent, **kwargs) == []


def test_only_undelivered_full_views_are_new_and_old_receipts_are_explicitly_inherited():
    scope, parent, addition, kwargs = _inputs()
    new_pairs = {(a['source_id'], a['block_id']) for a in scope['source_anchors']}
    inherited_pairs = {(a['source_id'], a['block_id']) for a in scope['inherited_source_anchors']}
    assert len(new_pairs) == 1 and new_pairs.isdisjoint(inherited_pairs)
    assert inherited_pairs
    assert scope['source_progress_sha256'] == kwargs['source_progress']['progress_sha256']
    text = partial_audit_completion_prompt(scope, parent)
    assert 'NOT newly read sources' in text and 'EVERY exact required_query_path' in text


def test_report_only_prompt_preserves_legacy_payload_and_does_not_imply_approval():
    scope, parent, _, _ = _inputs(source_missing=False)
    assert scope['source_anchors'] == []
    before = copy.deepcopy((scope, parent))
    legacy = partial_audit_completion_prompt(scope, parent)
    focused = partial_audit_completion_prompt(scope, parent, version=2)
    assert focused.endswith(legacy)
    assert 'ZERO new source delivery obligations' in focused
    assert 'blocking finding' in focused and 'NOT scientific approval' in focused
    assert 'Necessary targeted source checks remain available' in focused
    assert 'at most 20 exact paths' in focused
    assert (scope, parent) == before


def test_prompt_focus_does_not_skip_outstanding_source_delivery():
    scope, parent, _, _ = _inputs(source_missing=True)
    focused = partial_audit_completion_prompt(scope, parent, version=2)
    assert focused.startswith('MISSING-SOURCE AND REPORT COMPLETION.')
    assert 'ZERO new source delivery obligations' not in focused
    assert partial_audit_completion_prompt(scope, parent, version=1) == partial_audit_completion_prompt(scope, parent)
    with pytest.raises(ValueError, match='delivery-complete'):
        partial_audit_completion_prompt(scope, parent, version=3)


@pytest.mark.parametrize('version', [True, 0, 4, '2', None])
def test_unknown_prompt_versions_cannot_reinterpret_frozen_plans(version):
    scope, parent, _, _ = _inputs()
    with pytest.raises(ValueError, match='prompt version'):
        partial_audit_completion_prompt(scope, parent, version=version)


def test_merge_is_append_only_and_retains_candidate_parent_and_scope():
    scope, parent, addition, kwargs = _inputs()
    before = copy.deepcopy((scope, parent, kwargs))
    addition.coverage_limitations = ['Additional honest comparison restriction.']
    merged = merge_partial_audit_addendum(scope, addition, parent, **kwargs)
    assert merged.findings[:len(parent.findings)] == parent.findings
    assert merged.coverage_limitations[:len(parent.coverage_limitations)] == parent.coverage_limitations
    assert merged.review_scope_sha256 == parent.review_scope_sha256
    assert merged.quantitative_followups == parent.quantitative_followups
    assert (scope, parent, kwargs) == before
    assert validate_partial_audit_addendum(scope, addition, parent, **kwargs) == []


@pytest.mark.parametrize('field', ['scope_sha256', 'parent_audit_hash', 'candidate_hash'])
def test_addendum_identity_is_exact(field):
    scope, parent, addition, kwargs = _inputs()
    setattr(addition, field, '0' * 64)
    assert validate_partial_audit_addendum(scope, addition, parent, **kwargs)


@pytest.mark.parametrize('mutation', [
    lambda a: a.findings.clear(),
    lambda a: a.findings[0].evidence.clear(),
    lambda a: setattr(a.findings[0], 'record_paths', ['observations[2]']),
    lambda a: a.findings[0].evidence.append(SourceAnchor(source_id='forged', block_id='unknown')),
    lambda a: a.findings[0].omission_indices.append(0),
])
def test_missing_disposition_or_forged_evidence_cannot_complete(mutation):
    scope, parent, addition, kwargs = _inputs()
    mutation(addition)
    assert validate_partial_audit_addendum(scope, addition, parent, **kwargs)


@pytest.mark.parametrize('verdict', ['unsupported', 'not_checked', 'unresolved'])
def test_new_scientific_concern_is_retained_but_blocks_admission(verdict):
    scope, parent, addition, kwargs = _inputs()
    addition.findings[0].verdict = verdict
    addition.findings[0].needs_revision = verdict == 'unsupported'
    merged = merge_partial_audit_addendum(scope, addition, parent, **kwargs)
    assert merged.findings[-len(addition.findings):] == addition.findings
    assert validate_partial_audit_addendum(scope, addition, parent, **kwargs)


def test_free_text_scientific_concern_is_not_lost_even_when_dispositions_pass():
    scope, parent, addition, kwargs = _inputs()
    addition.scientific_concerns = ['A newly viewed panel raises a wider scientific concern.']
    merged = merge_partial_audit_addendum(scope, addition, parent, **kwargs)
    assert merged.potential_omissions == addition.scientific_concerns
    assert validate_partial_audit_addendum(scope, addition, parent, **kwargs)


@pytest.mark.parametrize('mutation', [
    lambda p: p['missing_full_views'].clear(),
    lambda p: p['requirements'].pop(),
    lambda p: p.__setitem__('complete', True),
    lambda p: p.__setitem__('review_scope_sha256', '0' * 64),
    lambda p: p['delivered'].clear(),
])
def test_inconsistent_progress_cannot_be_rehashed_into_valid_inheritance(mutation):
    scope, parent, addition, kwargs = _inputs()
    progress = kwargs['source_progress']
    mutation(progress)
    progress['progress_sha256'] = digest_json({k: v for k, v in progress.items() if k != 'progress_sha256'})
    with pytest.raises(PartialAuditCompletionRejected):
        build_partial_audit_completion_scope(kwargs['original_scope'], parent,
            **{k: v for k, v in kwargs.items() if k != 'original_scope'})


def test_parent_scientific_blocker_or_omission_is_not_a_report_completion():
    scope, parent, addition, kwargs = _inputs()
    for omission in (False, True):
        bad = parent.model_copy(deep=True)
        if omission:
            bad.potential_omissions = ['Unresolved scientific omission.']
        else:
            bad.findings[0].needs_revision = True
        with pytest.raises(PartialAuditCompletionRejected):
            build_partial_audit_completion_scope(kwargs['original_scope'], bad,
                **{k: v for k, v in kwargs.items() if k != 'original_scope'})


def test_other_original_contract_failures_are_not_waived():
    scope, parent, addition, kwargs = _inputs()
    parent.coverage_limitations = []
    with pytest.raises(PartialAuditCompletionRejected):
        build_partial_audit_completion_scope(kwargs['original_scope'], parent,
            **{k: v for k, v in kwargs.items() if k != 'original_scope'})


def test_source_only_and_report_only_failures_have_separate_honest_scopes():
    source_scope, parent, addition, kwargs = _inputs(report_missing=False)
    assert source_scope['parent_protocol_issues'] == []
    assert source_scope['source_anchors']
    assert validate_partial_audit_addendum(source_scope, addition, parent, **kwargs) == []
    report_scope, parent, addition, kwargs = _inputs(source_missing=False)
    assert report_scope['parent_protocol_issues']
    assert report_scope['source_anchors'] == []
    assert validate_partial_audit_addendum(report_scope, addition, parent, **kwargs) == []


def _native_amendment(kwargs):
    requirement = copy.deepcopy(kwargs['source_progress']['missing_full_views'][0])
    bid = requirement['block_id']
    amendment = {'policy': 'native-word-source-requirement-amendment-v1',
        'review_scope_sha256': kwargs['original_scope']['scope_sha256'],
        'source_bundle_sha256': kwargs['source_progress']['source_bundle_sha256'],
        'substitutions': [{'original_requirement': requirement,
            'native_word_structure': {'representation': 'native_word_table_xml',
                'parent_table_block_id': bid, 'part': 'word/document.xml', 'table_index': 0,
                'block_ids': [bid, 'native-child'], 'full_view_block_ids': [],
                'visual_status': 'native_table_has_no_registered_page_view',
                'limitations': ['Native table structure/text checked; no rendered Word page was available.']},
            'replacement_requires_full_view': False}],
        'scientific_validation': 'not_established'}
    amendment['amendment_sha256'] = digest_json(amendment)
    return amendment


def test_native_word_requirement_amendment_keeps_historical_progress_and_enforces_limits():
    scope, parent, addition, kwargs = _inputs()
    historical = copy.deepcopy(kwargs['source_progress'])
    kwargs['requirement_amendment'] = _native_amendment(kwargs)
    scope = build_partial_audit_completion_scope(kwargs['original_scope'], parent,
        **{k: v for k, v in kwargs.items() if k != 'original_scope'})
    addition.scope_sha256 = scope['scope_sha256']
    native = [r for r in scope['source_delivery_requirements'] if 'native_word_structure' in r]
    assert native and native[0]['requires_full_view'] is False
    assert historical == kwargs['source_progress']
    assert validate_partial_audit_addendum(scope, addition, parent, **kwargs)
    addition.coverage_limitations = list(scope['required_coverage_limitations'])
    assert validate_partial_audit_addendum(scope, addition, parent, **kwargs) == []


@pytest.mark.parametrize('mutation', [
    lambda a: a.__setitem__('source_bundle_sha256', 'c' * 64),
    lambda a: a['substitutions'][0].__setitem__('replacement_requires_full_view', True),
    lambda a: a['substitutions'][0]['native_word_structure'].__setitem__('block_ids', ['wrong']),
    lambda a: a['substitutions'][0]['native_word_structure'].__setitem__('visual_status', 'viewed'),
    lambda a: a['substitutions'][0]['native_word_structure']['limitations'].clear(),
])
def test_native_amendment_cannot_claim_false_visual_read_or_drop_actual_structure(mutation):
    scope, parent, addition, kwargs = _inputs()
    amendment = _native_amendment(kwargs)
    mutation(amendment)
    amendment['amendment_sha256'] = digest_json({k: v for k, v in amendment.items() if k != 'amendment_sha256'})
    with pytest.raises(PartialAuditCompletionRejected):
        build_partial_audit_completion_scope(kwargs['original_scope'], parent,
            **{k: v for k, v in kwargs.items() if k != 'original_scope'}, requirement_amendment=amendment)


def test_complete_prior_native_delivery_is_inherited_without_repeating_the_table_read():
    scope, parent, addition, kwargs = _inputs()
    amendment = _native_amendment(kwargs)
    native = amendment['substitutions'][0]['native_word_structure']
    sid = amendment['substitutions'][0]['original_requirement']['source_id']
    progress = kwargs['source_progress']
    progress['structured_blocks'] = [{'source_id': sid, 'block_id': bid} for bid in native['block_ids']]
    progress['progress_sha256'] = digest_json({k: v for k, v in progress.items() if k != 'progress_sha256'})
    kwargs['requirement_amendment'] = amendment
    scope = build_partial_audit_completion_scope(kwargs['original_scope'], parent,
        **{k: v for k, v in kwargs.items() if k != 'original_scope'})
    assert scope['source_anchors'] == []
    assert len(scope['inherited_native_source_dispositions']) == 1
    assert progress['missing_full_views'] and progress['complete'] is False  # The old proof remains honest.
    addition.scope_sha256 = scope['scope_sha256']
    addition.coverage_limitations = scope['required_coverage_limitations']
    assert validate_partial_audit_addendum(scope, addition, parent, **kwargs) == []


def test_new_native_scope_missing_structure_is_a_real_gap_even_when_target_text_was_delivered():
    scope, parent, addition, kwargs = _inputs(native_policy=True)
    native = _native_amendment(kwargs)['substitutions'][0]['native_word_structure']
    progress = kwargs['source_progress']
    requirement = progress['requirements'][-1]
    requirement['requires_full_view'] = False
    requirement['native_word_structure'] = native
    progress['missing_full_views'] = []
    progress['structured_blocks'] = [{'source_id': requirement['source_id'], 'block_id': requirement['block_id']}]
    progress['missing_native_structures'] = [copy.deepcopy(requirement)]
    progress['progress_sha256'] = digest_json({k: v for k, v in progress.items() if k != 'progress_sha256'})
    scope = build_partial_audit_completion_scope(kwargs['original_scope'], parent,
        **{k: v for k, v in kwargs.items() if k != 'original_scope'})
    assert scope['source_anchors']  # Missing child cells cannot disappear behind target delivery.
    assert not scope['inherited_native_source_dispositions']
    progress['missing_native_structures'] = []
    progress['progress_sha256'] = digest_json({k: v for k, v in progress.items() if k != 'progress_sha256'})
    with pytest.raises(PartialAuditCompletionRejected):
        build_partial_audit_completion_scope(kwargs['original_scope'], parent,
            **{k: v for k, v in kwargs.items() if k != 'original_scope'})
