from app.errors import DomainError
from app.modules.archive.pipeline import run_shift
from app.modules.dispatch.compare import compare
from app.shift import load_shift
from .planner import candidates, identity, locate, read_fault, receipt, report, replacement, role


def _prepare(store, run_id, fault_raw, need_target=True):
    active = store.load(run_id)
    sources = store.sources(run_id)
    shift = load_shift(sources['shift'])
    plan = active['dispatch']['legs']
    fault = read_fault(fault_raw, with_target=need_target)
    leg = locate(plan, shift, fault)
    return active, sources, shift, plan, fault, leg


def _replacement(shift, plan, leg, fault, target):
    new_plan, replaced, superseded = replacement(shift, plan, leg, fault, target)
    return new_plan, replaced, superseded


def _snapshot(sources, plan, run_id, fault, target):
    return run_shift(sources, plan, identity(run_id, fault, target))


def _receipt(shift, run_id, fault, target, previous_legs, new_plan, before, after):
    divert = {'fault': fault['id'], 'leg': fault['leg'], 'at': fault['at'],
              'target': target, 'source_run': run_id}
    return receipt(shift, divert, previous_legs, new_plan, before, after)


def options(store, run_id, fault_raw):
    """对每个可落地站试算一次，供调度员比较就近与退回出发站。"""
    active, sources, shift, plan, fault, leg = _prepare(store, run_id, fault_raw, need_target=False)
    result = []
    for target in candidates(shift, leg):
        new_plan, _, superseded = _replacement(shift, plan, leg, fault, target)
        item = {'target': target, 'role': role(leg, target), 'superseded_legs': superseded}
        try:
            snapshot = _snapshot(sources, new_plan, run_id, fault, target)
            after = snapshot['replay']['plan']
            cell = snapshot['demand']['plan']
            stop = next(row for row in after['timeline']
                        if row['kind'] == 'unload' and row['id'] == leg['id'])
            item.update(status='ok', delta=compare(shift, active['replay']['plan'], after)['delta'],
                        missed=len(cell['missed']),
                        unload={'at': stop['at'], 'station': stop['station'], 'count': stop['quantity']},
                        landing={'at': stop['at'], 'free_before': stop['before']['free'],
                                 'free_after': stop['after']['free'], 'docked_before': stop['before']['docked'],
                                 'docked_after': stop['after']['docked'], 'capacity': stop['after']['capacity']},
                        closing={code: {'docked': row['docked'], 'free': row['free'], 'held': row['held']}
                                 for code, row in after['closing'].items()})
        except DomainError as error:
            item.update(status='blocked', code=error.code, details=error.details)
        result.append(item)
    return {'run': run_id, 'fault': fault, 'leg': leg, 'options': result}


def preview(store, run_id, fault_raw):
    active, sources, shift, plan, fault, leg = _prepare(store, run_id, fault_raw)
    new_plan, replaced, superseded = _replacement(shift, plan, leg, fault, fault['target'])
    snapshot = _snapshot(sources, new_plan, run_id, fault, fault['target'])
    return {'id': snapshot['manifest']['id'], 'stored': False, 'dispatch': snapshot['dispatch'],
            'inventory': snapshot['inventory']['plan'], 'slots': snapshot['demand']['plan']['slots'],
            'receipt': _receipt(shift, run_id, fault, fault['target'], plan, new_plan,
                                active['replay']['plan'], snapshot['replay']['plan']),
            'replaced_leg': replaced, 'superseded_legs': superseded,
            'options': options(store, run_id, fault_raw)['options']}


def publish(store, expected, fault_raw):
    fault_probe = read_fault(fault_raw, with_target=True)
    published = store.diversion(fault_probe['id'])
    if published is not None:
        record = store.fault(fault_probe['id'])['report']
        if (record['leg'], record['at'], published['option']) != (fault_probe['leg'], fault_probe['at'], fault_probe['target']):
            raise DomainError('FAULT_CONFLICT', 409, fault=fault_probe['id'], published=published['run_id'])
        reused = store.reuse_diversion(fault_probe['id'], expected)
        return {'id': reused['run_id'], 'created': False, 'changed': reused['run_id'] != expected,
                'reused': True, 'stored': True, 'receipt': store.load(reused['run_id'])['divert']['receipt']}
    active = store.load(expected)
    sources = store.sources(expected)
    shift = load_shift(sources['shift'])
    fault = read_fault(fault_raw, with_target=True)
    leg = locate(active['dispatch']['legs'], shift, fault)
    new_plan, replaced, superseded = _replacement(shift, active['dispatch']['legs'], leg, fault, fault['target'])
    snapshot = _snapshot(sources, new_plan, expected, fault, fault['target'])
    divert = {'report': report(active['dispatch']['legs'], expected, fault, leg),
              'identity': identity(expected, fault, fault['target'])}
    divert['receipt'] = _receipt(shift, expected, fault, fault['target'], active['dispatch']['legs'], new_plan,
                                 active['replay']['plan'], snapshot['replay']['plan'])
    snapshot['divert'] = divert
    created = store.publish_diversion(snapshot, sources,
                                      {'id': fault['id'], 'received_at': snapshot['manifest']['generated_at'],
                                       'option': fault['target'], 'report': divert['report']}, expected)
    return {'id': snapshot['manifest']['id'], 'created': created['created'],
            'changed': snapshot['manifest']['id'] != expected, 'reused': created['reused'], 'stored': True,
            'receipt': divert['receipt']}


def stage(store, run_id):
    active = store.load(run_id)
    plan = active['dispatch']['legs']
    faults = store.faults()
    published = {}
    for row in faults:
        record = store.diversion(row['id'])
        if record is not None:
            published[row['id']] = record
    return {'run': active['manifest']['id'], 'plan': plan, 'shift': active['shift'],
            'divert': active.get('divert'), 'faults': faults, 'published': published,
            'history': store.history()}
