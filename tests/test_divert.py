FAULT = {'id': 'F-01', 'leg': 'M-02', 'at': '2026-11-20T09:04:00+08:00',
         'target': '002', 'note': '临时管制'}


def options(client, run, fault=FAULT):
    return client.post('/api/divert/options', json={'run': run, 'fault': fault})


def preview(client, run, fault=FAULT):
    return client.post('/api/divert/preview', json={'run': run, 'fault': fault})


def publish(client, run, fault=FAULT):
    return client.post('/api/divert/publish', json={'expected': run, 'fault': fault})


def test_options_expose_nearby_and_origin_with_blocked_full_station(client, run_id):
    response = options(client, run_id, {key: value for key, value in FAULT.items() if key != 'target'})
    assert response.status_code == 200
    cells = {row['target']: row for row in response.json['options']}
    assert cells['002']['role'] == 'nearby' and cells['002']['status'] == 'ok'
    assert cells['003']['role'] == 'origin' and cells['003']['status'] == 'ok'
    assert cells['004']['status'] == 'blocked' and cells['004']['code'] == 'DROP_FULL'
    assert cells['002']['unload']['station'] == '002'


def test_preview_receipt_states_landing_stock_and_missed_change(client, run_id):
    response = preview(client, run_id)
    assert response.status_code == 200
    copy = response.json['receipt']
    assert copy['target'] == '002' and copy['role'] == 'nearby'
    assert copy['unload'] == [{'leg': 'M-02', 'station': '002', 'at': '2026-11-20T01:08:00Z', 'count': 2}]
    assert copy['effective_from'] == '2026-11-20T01:08:00Z'
    assert copy['invalidated_at'] == '2026-11-20T01:04:00Z'
    stock = {row['station']: row for row in copy['stock']}
    assert stock['002']['docked'] == {'before': 0, 'after': 1}
    assert stock['002']['free'] == {'before': 7, 'after': 6}
    assert stock['003']['docked'] == {'before': 1, 'after': 1}
    assert copy['comparison']['delta'] == -1 and copy['comparison']['recovered'] == 1
    assert copy['comparison']['changes'][0]['id'] == '0047'
    assert copy['fleet']['ok'] and copy['fleet']['closing_after'] == 33


def test_origin_diversion_keeps_stock_at_origin(client, run_id):
    fault = dict(FAULT, target='003')
    copy = preview(client, run_id, fault).json['receipt']
    assert copy['role'] == 'origin' and copy['target'] == '003'
    stock = {row['station']: row for row in copy['stock']}
    assert stock['002']['docked'] == {'before': 0, 'after': 0}
    assert stock['003']['docked'] == {'before': 1, 'after': 2}


def test_publish_archives_fault_and_switches_plan(client, run_id):
    original = client.get(f'/api/dispatch?run={run_id}').json['data']['legs']
    response = publish(client, run_id)
    assert response.status_code == 200 and response.json['created'] is True
    new = response.json['id']
    assert new != run_id and client.get('/health').json['run'] == new
    plan = client.get(f'/api/dispatch?run={new}').json['data']['legs']
    assert [(row['id'], row['from'], row['to']) for row in plan] == [('M-01', '001', '002'), ('M-02', '003', '002')]
    assert [(row['id'], row['to']) for row in client.get(f'/api/dispatch?run={run_id}').json['data']['legs']] == [
        ('M-01', '002'), ('M-02', '001')]
    report = client.get('/api/divert/faults/F-01').json['report']
    assert report['plan'] == original and report['to'] == '001' and report['at'] == '2026-11-20T01:04:00Z'
    receipt = client.get(f'/api/divert/receipts/{new}.json')
    assert receipt.status_code == 200 and receipt.json['divert']['receipt']['target'] == '002'


def test_retrying_same_fault_never_creates_a_second_diversion(client, run_id):
    first = publish(client, run_id).json
    before = len(client.get('/api/runs').json['history'])
    for expected in (run_id, first['id'], first['id']):
        again = publish(client, expected)
        assert again.status_code == 200
        assert again.json['id'] == first['id'] and again.json['created'] is False and again.json['reused'] is True
    assert len(client.get('/api/runs').json['history']) == before == 2
    with client.application.extensions['shift_store'].connect() as db:
        assert db.execute('SELECT count(*) FROM diversions').fetchone() == (1,)
        assert db.execute('SELECT count(*) FROM faults').fetchone() == (1,)


def test_same_fault_id_with_different_content_is_rejected(client, run_id):
    publish(client, run_id)
    clash = dict(FAULT, target='003')
    response = publish(client, run_id, clash)
    assert (response.status_code, response.json['code']) == (409, 'FAULT_CONFLICT')


def test_failed_diversion_keeps_old_plan_and_adds_no_archive(client, run_id, app):
    blocked = dict(FAULT, target='004')
    response = publish(client, run_id, blocked)
    assert (response.status_code, response.json['code']) == (422, 'DROP_FULL')
    assert app.extensions['shift_store'].active() == run_id
    assert len(client.get('/api/runs').json['history']) == 1
    with app.extensions['shift_store'].connect() as db:
        assert db.execute('SELECT count(*) FROM faults').fetchone() == (0,)
    # 同一故障换个能落地的去向仍可发布，旧方案始终在档。
    assert publish(client, run_id).json['id'] != run_id


def test_storage_failure_rolls_back_diversion_archive(app, client, run_id):
    db = app.extensions['shift_store']
    with db.connect() as conn:
        conn.execute("CREATE TRIGGER stop_active BEFORE UPDATE ON active BEGIN SELECT RAISE(ABORT, 'test'); END")
    response = publish(client, run_id)
    assert (response.status_code, response.json['code']) == (503, 'STORE_UNAVAILABLE')
    assert db.active() == run_id
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM runs').fetchone() == (1,)
        assert conn.execute('SELECT count(*) FROM faults').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM diversions').fetchone() == (0,)


def test_fault_must_sit_inside_the_transport_window(client, run_id):
    early = dict(FAULT, at='2026-11-20T09:00:00+08:00')
    assert options(client, run_id, early).json['code'] == 'FAULT_OUTSIDE_LEG'
    assert options(client, run_id, dict(FAULT, leg='M-99')).json['code'] == 'FAULT_LEG_UNKNOWN'
    assert preview(client, run_id, dict(FAULT, target='001')).json['code'] == 'DIVERT_NO_CHANGE'
    assert preview(client, run_id, dict(FAULT, target='009')).json['code'] == 'UNKNOWN_STATION'
    assert options(client, run_id, {**FAULT, 'id': ''}).json['code'] == 'BAD_FAULT'


def test_diverted_plan_can_still_be_rerun_and_restored(client, run_id):
    new = publish(client, run_id).json['id']
    assert client.post('/api/runs/rerun', json={'expected': new}).json == {'id': new, 'created': False, 'changed': False}
    back = client.post('/api/runs/restore', json={'target': run_id, 'expected': new}).json
    assert back == {'id': run_id, 'created': False, 'changed': True}
    assert client.get(f'/api/runs?run={run_id}').json['manifest']['id'] == run_id


def test_diverted_run_keeps_receipt_through_rerun_and_evidence(client, run_id):
    original = client.get(f'/api/dispatch?run={run_id}').json['data']['legs']
    new = publish(client, run_id).json['id']
    evidence = client.get(f'/api/runs/{new}/evidence.json').json['evidence']
    assert evidence['divert']['receipt']['target'] == '002'
    assert evidence['divert']['report']['plan'] == original


def test_retry_after_rollback_reactivates_same_diversion(client, run_id):
    new = publish(client, run_id).json['id']
    client.post('/api/runs/restore', json={'target': run_id, 'expected': new})
    assert client.get('/health').json['run'] == run_id
    again = publish(client, run_id)
    assert again.status_code == 200 and again.json['id'] == new and again.json['reused'] is True
    assert client.get('/health').json['run'] == new
    assert len(client.get('/api/runs').json['history']) == 2


def test_concurrent_same_fault_publishes_once(app, run_id):
    from concurrent.futures import ThreadPoolExecutor

    def submit(_):
        return publish(app.test_client(), run_id).json
    with ThreadPoolExecutor(2) as workers:
        one, two = workers.map(submit, range(2))
    assert one['id'] == two['id'] and sorted([one['created'], two['created']]) == [False, True]
    assert sorted([one['reused'], two['reused']]) == [False, True]
    with app.extensions['shift_store'].connect() as db:
        assert db.execute('SELECT count(*) FROM diversions').fetchone() == (1,)
        assert db.execute('SELECT count(*) FROM runs').fetchone() == (2,)


def test_second_fault_can_be_diverted_on_top_of_first(client, run_id):
    first = publish(client, run_id).json['id']
    second_fault = {'id': 'F-02', 'leg': 'M-01', 'at': '2026-11-20T08:10:00+08:00',
                    'target': '004', 'note': '第二处管制'}
    response = publish(client, first, second_fault)
    assert response.status_code == 200 and response.json['created'] is True
    second = response.json['id']
    plan = client.get(f'/api/dispatch?run={second}').json['data']['legs']
    assert [(row['id'], row['to']) for row in plan] == [('M-01', '004'), ('M-02', '002')]
    assert len(client.get('/api/runs').json['history']) == 3
    assert client.get('/api/divert').json['data']['divert']['report']['id'] == 'F-02'


def test_retry_on_a_later_run_does_not_roll_back_newer_plan(client, run_id):
    first = publish(client, run_id).json['id']
    second = publish(client, first, {'id': 'F-02', 'leg': 'M-01', 'at': '2026-11-20T08:10:00+08:00',
                                     'target': '004'}).json['id']
    retry = publish(client, second, FAULT)
    assert (retry.status_code, retry.json['code']) == (409, 'DUPLICATE_FAULT')
    assert client.get('/health').json['run'] == second
    assert len(client.get('/api/runs').json['history']) == 3


# TRAP-30
def test_receipt_lists_superseded_follow_on_legs(client, run_id):
    two_legs = [
        {'id': 'M-01', 'truck': 'T-01', 'from': '001', 'to': '004',
         'depart': '2026-11-20T08:09:00+08:00', 'arrive': '2026-11-20T08:14:00+08:00', 'count': 2},
        {'id': 'M-02', 'truck': 'T-01', 'from': '004', 'to': '002',
         'depart': '2026-11-20T08:40:00+08:00', 'arrive': '2026-11-20T08:46:00+08:00', 'count': 2},
    ]
    base = client.post('/api/dispatch/publish', json={'expected': run_id, 'legs': two_legs}).json['id']
    fault = {'id': 'F-02', 'leg': 'M-01', 'at': '2026-11-20T08:11:00+08:00', 'target': '002'}
    copy = preview(client, base, fault).json['receipt']
    assert copy['superseded_legs'] == ['M-02']
    # 被作废的后续行程不再出现在替换方案里，但原方案仍留档。
    assert [row['id'] for row in preview(client, base, fault).json['dispatch']['legs']] == ['M-01']
    assert [row['id'] for row in client.get(f'/api/dispatch?run={base}').json['data']['legs']] == ['M-01', 'M-02']
