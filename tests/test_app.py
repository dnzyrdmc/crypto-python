import threading
import pytest
import app as dashboard

@pytest.fixture
def config():
    return dict(symbols=['BTCUSDT'], interval='1m', binance_api_key='test-key', binance_api_secret='test-secret')

@pytest.fixture
def client():
    return dashboard.app.test_client()

def test_dashboard_and_idle_status(client):
    assert client.get('/').status_code == 200
    assert client.get('/static/panel.css').status_code == 200
    assert client.get('/trade-log').json == []
    assert client.get('/status').json['status'] == 'idle'

@pytest.mark.parametrize('patch', [dict(limit=1), dict(volume_multiplier=float('nan')), dict(max_trades_limit=1.5), dict(symbols=['BAD']), dict(interval='bad'), dict(usdt_amount=101), dict(binance_api_key=''), dict(testnet='false'), dict(telegram_token='token')])
def test_reject_bad_config(client, config, patch):
    config.update(patch)
    assert client.post('/start-bot', json=config).status_code == 400

def test_malformed_json(client):
    assert client.post('/start-bot', data='bad', content_type='application/json').status_code == 400

def test_defaults(config):
    normalized = dashboard.validate_config(config)
    assert normalized['testnet'] is True
    assert normalized['stop_loss_threshold'] == 0

def test_duplicate_start_and_failure_status(client, config, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def fake_bot(config, report):
        report('running', 'Test')
        entered.set()
        release.wait(3)
        report('error', 'Test connection failure')
    monkeypatch.setattr(dashboard, 'run_bot', fake_bot)
    assert client.post('/start-bot', json=config).status_code == 202
    assert entered.wait(2)
    assert client.get('/status').json['status'] == 'running'
    assert client.post('/start-bot', json=config).status_code == 409
    release.set()
    dashboard.worker.join(3)
    assert client.get('/status').json['status'] == 'error'
