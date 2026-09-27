import json
import pytest
from studylink import chat
from tests.test_chat import FakeClient


@pytest.fixture
def setup_pool(client, signup):
    a = {'Authorization': f'Bearer {signup(client, "alice@school.edu")}'}
    b = {'Authorization': f'Bearer {signup(client, "bob@school.edu")}'}
    e = {'Authorization': f'Bearer {signup(client, "eve@school.edu")}'}
    client.patch('/auth/profile', headers=a, json={'display_name': 'Alice'})
    c = client.post('/cohorts', headers=a, json={'name': 'CS101', 'term': 'Fall 2026'}).json()
    assert client.post('/cohort-invites/preview', headers=b, json={'code': c['invite_code']}).status_code == 200
    assert client.get('/cohorts', headers=b).json() == []
    assert client.post('/cohort-invites/join', headers=b, json={'code': c['invite_code'], 'role': 'instructor'}).json()['role'] == 'member'
    n = client.post('/notes', headers=a, json={'title': 'Gradient descent', 'body': 'Gradient descent learning rate alpha sets the step size. ALICE SHARED.'}).json()['id']
    return a, b, e, c, n


def test_cohort_api_private_shared_and_revoked(client, setup_pool):
    a, b, e, c, n = setup_pool
    cid = c['id']
    path = f'/cohorts/{cid}/notes/{n}'
    assert client.get(path, headers=b).status_code == 404
    assert client.patch(f'/notes/{n}/sharing', headers=b, json={'cohort_id': cid}).status_code == 404
    assert client.patch(f'/notes/{n}/sharing', headers=a, json={'cohort_id': cid}).status_code == 200
    assert client.get(path, headers=b).json()['contributor_name'] == 'Alice'
    assert client.get(path, headers=e).status_code == 404
    assert client.get(f'/notes/{n}', headers=b).status_code == 404
    assert client.patch(f'/notes/{n}', headers=b, json={'body':'stolen'}).status_code == 404
    assert client.get('/search?q=gradient+descent', headers=b).json() == []
    assert [m['note_id'] for m in client.get(f'/search?q=gradient+descent&cohort_id={cid}', headers=b).json()] == [n]
    assert client.get(f'/search?q=gradient&cohort_id={cid}', headers=e).status_code == 404
    assert client.post('/ask/stream', headers=e, json={'question':'gradient', 'cohort_id': cid}).status_code == 404
    assert client.patch(f'/notes/{n}/sharing', headers=a, json={'cohort_id': None}).status_code == 200
    assert client.get(path, headers=b).status_code == 404
    assert client.get(f'/search?q=gradient+descent&cohort_id={cid}', headers=b).json() == []


def test_attribution_stream_and_answer(client, setup_pool, monkeypatch):
    a, b, e, c, n = setup_pool
    real = chat.NoteChat.__init__
    def init(self, conn, retriever, user_id, **kwargs):
        real(self, conn, retriever, user_id, client=FakeClient(f'Alice explains gradient descent [N{n}].'))
    monkeypatch.setattr(chat.NoteChat, '__init__', init)
    client.patch(f'/notes/{n}/sharing', headers=a, json={'cohort_id': c['id']})
    payload = {'question': 'gradient descent', 'cohort_id': c['id']}
    result = client.post('/ask', headers=b, json=payload)
    assert result.status_code == 200, result.text
    assert result.json()['sources'][0]['contributor_name'] == 'Alice'
    assert result.json()['grounded'] is True
    streamed = client.post('/ask/stream', headers=b, json=payload)
    events = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith('data: ')]
    assert next(x for x in events if x['type'] == 'sources')['sources'][0]['contributor_name'] == 'Alice'
    assert next(x for x in events if x['type'] == 'done')['answer']['sources'][0]['cohort_id'] == c['id']


def test_membership_and_moderation_api(client, setup_pool):
    a, b, e, c, n = setup_pool
    cid = c['id']
    bob = client.get('/auth/me', headers=b).json()['id']
    assert 'invite_code' not in client.get(f'/cohorts/{cid}', headers=b).json()
    assert client.post(f'/cohorts/{cid}/invite', headers=b).status_code == 400
    assert client.get(f'/cohorts/{cid}/members', headers=e).status_code == 404
    assert client.delete(f'/cohorts/{cid}/membership', headers=a).status_code == 400
    client.patch(f'/notes/{n}/sharing', headers=a, json={'cohort_id':cid})
    assert client.delete(f'/cohorts/{cid}/notes/{n}', headers=b).status_code == 400
    assert client.delete(f'/cohorts/{cid}/notes/{n}', headers=a).status_code == 204
    assert client.get(f'/notes/{n}', headers=a).json()['body']
    assert client.delete(f'/cohorts/{cid}/members/{bob}', headers=a).status_code == 204
    assert client.post('/cohort-invites/join', headers=b, json={'code':c['invite_code']}).status_code == 404


@pytest.mark.parametrize('method,path,payload', [
    ('get','/cohorts',None), ('post','/cohorts',{'name':'x','term':'y'}),
    ('post','/cohort-invites/preview',{'code':'x'}), ('post','/cohort-invites/join',{'code':'x'}),
    ('get','/cohorts/1',None), ('get','/cohorts/1/members',None),
    ('post','/cohorts/1/invite',{}), ('post','/cohorts/1/transfer',{'user_id':2}),
    ('delete','/cohorts/1/membership',None), ('delete','/cohorts/1/members/2',None),
    ('get','/cohorts/1/notes',None), ('get','/cohorts/1/notes/1',None),
    ('delete','/cohorts/1/notes/1',None), ('patch','/notes/1/sharing',{'cohort_id':1}),
    ('patch','/auth/profile',{'display_name':'Alice'}),
])
def test_new_routes_require_auth(client, method, path, payload):
    assert client.request(method, path, json=payload).status_code == 401


def test_work_session_attributes_pooled_sources(client, setup_pool, monkeypatch):
    from studylink.agent import WorkSessionAgent, AgentResult
    a, b, e, c, n = setup_pool
    client.patch(f'/notes/{n}/sharing', headers=a, json={'cohort_id':c['id']})
    target = client.post('/targets', headers=b, json={'name':'Gradient descent', 'description':'learning rate alpha'}).json()
    def loop(self, *args, **kwargs):
        self._run_search({'query':'gradient descent'})
        return AgentResult(text=f'Alice explains this [N{n}].', cited_note_ids=[n], stop_reason='end_turn')
    monkeypatch.setattr(WorkSessionAgent, '_run_loop', loop)
    assignment_id = target['assignment']['id'] if 'assignment' in target else target['id']
    response = client.post('/work-session', headers=b, json={'assignment_id':assignment_id, 'cohort_id':c['id']})
    assert response.status_code == 200, response.text
    assert response.json()['sources'][0]['contributor_name'] == 'Alice'
    assert response.json()['sources'][0]['valid'] is True
    assert response.json()['traceability']['hallucinated_ids'] == []
