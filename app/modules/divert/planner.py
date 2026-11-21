import re
from datetime import timedelta

from app.clock import instant, stamp
from app.errors import DomainError
from app.modules.dispatch.compare import compare

KEY = re.compile(r'[A-Za-z0-9_-]{1,32}')


def read_fault(raw, with_target=False):
    """校验故障报告的形状并归一化时点。"""
    if not isinstance(raw, dict):
        raise DomainError('BAD_FAULT')
    required = {'id', 'leg', 'at'} | ({'target'} if with_target else set())
    if not required <= set(raw) or not set(raw) <= required | {'note', 'target'}:
        raise DomainError('BAD_FAULT', fields=sorted(required))
    for field in sorted(required):
        if not isinstance(raw[field], str) or not raw[field]:
            raise DomainError('BAD_FAULT', field=field)
    for field in ('id', 'leg'):
        if not KEY.fullmatch(raw[field]):
            raise DomainError('BAD_FAULT', field=field)
    at = instant(raw['at'])
    if at.microsecond % 1000:
        raise DomainError('BAD_FAULT', field='at', precision='millisecond')
    note = raw.get('note', '')
    if not isinstance(note, str):
        raise DomainError('BAD_FAULT', field='note')
    fault = {'id': raw['id'], 'leg': raw['leg'], 'at': stamp(at), 'note': note}
    if with_target:
        fault['target'] = raw['target']
    return fault


def locate(plan, shift, fault):
    """故障必须发生在方案中某一趟车的装车与卸车之间。"""
    leg = next((row for row in plan if row['id'] == fault['leg']), None)
    if leg is None:
        raise DomainError('FAULT_LEG_UNKNOWN', leg=fault['leg'])
    at = instant(fault['at'])
    if not instant(leg['depart']) < at < instant(leg['arrive']):
        raise DomainError('FAULT_OUTSIDE_LEG', leg=leg['id'], depart=leg['depart'],
                          arrive=leg['arrive'], at=fault['at'])
    check_target(shift, leg, fault)
    return leg


def check_target(shift, leg, fault):
    """校验落地去向；比较去向时不带 target，逐站自行校验。"""
    target = fault.get('target')
    if target is None:
        return
    if target not in shift.stations:
        raise DomainError('UNKNOWN_STATION', station=target)
    if target == leg['to']:
        raise DomainError('DIVERT_NO_CHANGE', leg=leg['id'], station=target)


def report(plan, source_run, fault, leg):
    """留档用故障报告，连同当时原方案一起封存。"""
    return {'id': fault['id'], 'source_run': source_run, 'leg': leg['id'], 'truck': leg['truck'],
            'from': leg['from'], 'to': leg['to'], 'count': leg['count'],
            'depart': leg['depart'], 'arrive': leg['arrive'], 'at': fault['at'], 'note': fault['note'],
            'plan': [dict(row) for row in plan]}


def identity(source_run, fault, target):
    return {'fault': fault['id'], 'leg': fault['leg'], 'at': fault['at'],
            'target': target, 'source_run': source_run}


def replacement(shift, plan, leg, fault, target):
    """把受阻的一趟改成落地行程；该车其后各趟一并作废。"""
    landing = instant(fault['at']) + timedelta(minutes=shift.min_travel_minutes)
    replaced = dict(leg, to=target, arrive=stamp(landing))
    index = plan.index(leg)
    kept = [row for position, row in enumerate(plan) if position < index or row['truck'] != leg['truck']]
    superseded = [row['id'] for row in plan[index + 1:] if row['truck'] == leg['truck']]
    return kept + [replaced], replaced, superseded


def role(leg, target):
    return 'origin' if target == leg['from'] else 'nearby'


def candidates(shift, leg):
    """候选落地站：其余各站按站号排列，出发站排在最后作为保守选项。"""
    others = [code for code in shift.stations if code not in (leg['to'], leg['from'])]
    return others + [leg['from']] if leg['from'] != leg['to'] else others


def _closing(result, station):
    row = result['closing'][station]
    return {'docked': row['docked'], 'held': row['held'], 'free': row['free'], 'capacity': row['capacity']}


def stock(shift, before, after):
    rows = []
    for code in shift.stations:
        previous, current = _closing(before, code), _closing(after, code)
        rows.append({'station': code,
                     **{key: {'before': previous[key], 'after': current[key]}
                        for key in ('docked', 'held', 'free', 'capacity')}})
    return rows


def fleet(shift, before, after):
    opening = sum(row['opening'] for row in shift.stations.values()) + shift.opening_road
    count = lambda result: sum(row['docked'] + row['held'] for row in result['closing'].values()) + result['road'] + result['transit']
    closing_before, closing_after = count(before), count(after)
    return {'opening': opening, 'closing_before': closing_before, 'closing_after': closing_after,
            'transit_before': before['transit'], 'transit_after': after['transit'],
            'ok': closing_before == closing_after == opening}


def receipt(shift, divert, previous, new_plan, before, after):
    """改派回执：最终卸车点、逐站库存与未成交变化、旧方案失效时点。"""
    old = {row['id']: row for row in previous}
    new = {row['id']: row for row in new_plan}
    replaced = [key for key in new if key in old
                and (old[key]['to'], old[key]['arrive']) != (new[key]['to'], new[key]['arrive'])]
    superseded = [key for key in old if key not in new]
    unload = [{'leg': row['id'], 'station': row['station'], 'at': row['at'], 'count': row['quantity']}
              for row in after['timeline'] if row['kind'] == 'unload' and row['id'] in replaced]
    return {
        'fault': {'id': divert['fault'], 'leg': divert['leg'], 'at': divert['at']},
        'source_run': divert['source_run'],
        'target': divert['target'],
        'role': role(next(row for row in previous if row['id'] == divert['leg']), divert['target']),
        'invalidated_at': divert['at'],
        'effective_from': unload[0]['at'] if unload else None,
        'replaced_legs': replaced,
        'superseded_legs': superseded,
        'unload': unload,
        'comparison': compare(shift, before, after),
        'stock': stock(shift, before, after),
        'fleet': fleet(shift, before, after),
    }
