from flask import Blueprint, Response, jsonify

from app.errors import DomainError
from app.manifest import canonical
from app.modules.divert.service import options, preview, publish, stage
from app.web import body, selected, store

bp = Blueprint('divert', __name__)


@bp.get('/api/divert')
def show():
    snapshot = selected()
    return jsonify(id=snapshot['manifest']['id'], data=stage(store(), snapshot['manifest']['id']))


@bp.post('/api/divert/options')
def compare_options():
    data = body('run', 'fault')
    return jsonify(options(store(), data['run'], data['fault']))


@bp.post('/api/divert/preview')
def trial():
    data = body('run', 'fault')
    return jsonify(preview(store(), data['run'], data['fault']))


@bp.post('/api/divert/publish')
def accept():
    data = body('expected', 'fault')
    return jsonify(publish(store(), data['expected'], data['fault']))


@bp.get('/api/divert/faults/<fault_id>')
def fault(fault_id):
    return jsonify(store().fault(fault_id))


@bp.get('/api/divert/receipts/<run_id>.json')
def receipt(run_id):
    snapshot = store().load(run_id)
    divert = snapshot.get('divert')
    if divert is None:
        raise DomainError('RECEIPT_NOT_FOUND', 404)
    payload = {'manifest': snapshot['manifest'], 'divert': divert}
    return Response(canonical(payload) + '\n', mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="divert-receipt-{run_id[:12]}.json"'})
