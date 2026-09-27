"""Adversarial cohort boundaries: same topic, distinct owners and cohorts."""
import pytest
from sqlalchemy import select, update
from studylink import cohorts, store
from studylink.errors import NotFoundError, CrossUserAccessError
from studylink.schema import notes


@pytest.fixture
def pool(conn, two_users):
    a, b = two_users['alice'], two_users['bob']
    cohorts.set_display_name(conn, a, 'Alice')
    cohorts.set_display_name(conn, b, 'Bob')
    x = cohorts.create_cohort(conn, a, 'CS 101 section 1', 'Fall 2026')
    cohorts.join_cohort(conn, b, x['invite_code'])
    outsider = store.create_user(conn, email='eve@school.edu', display_name='Eve')
    y = cohorts.create_cohort(conn, outsider, 'CS 101 section 2', 'Fall 2026')
    return {**two_users, 'x': x, 'y': y, 'eve': outsider}


def test_join_never_shares_and_private_read_stays_private(conn, pool):
    assert cohorts.list_cohort_notes(conn, pool['x']['id'], pool['bob']) == []
    assert store.get_note(conn, pool['alice_note'], pool['bob']) is None
    rows = conn.execute(select(notes.c.visibility, notes.c.shared_cohort_id)).all()
    assert all(r == ('private', None) for r in rows)


def test_sharing_requires_ownership_membership_and_name(conn, pool):
    with pytest.raises(CrossUserAccessError):
        cohorts.share_note_to_cohort(conn, pool['alice_note'], pool['bob'], pool['x']['id'])
    with pytest.raises(NotFoundError):
        cohorts.share_note_to_cohort(conn, pool['alice_note'], pool['alice'], pool['y']['id'])
    cohorts.share_note_to_cohort(conn, pool['alice_note'], pool['alice'], pool['x']['id'])
    note = cohorts.get_shared_note(conn, pool['x']['id'], pool['alice_note'], pool['bob'])
    assert note.contributor_name == 'Alice'
    assert note.course_id is None and note.course_name == ''
    with pytest.raises(NotFoundError):
        cohorts.get_shared_note(conn, pool['x']['id'], pool['alice_note'], pool['eve'])
    with pytest.raises(NotFoundError):
        cohorts.get_shared_note(conn, pool['x']['id'], pool['bob_note'], pool['bob'])


def test_empty_name_blocks_sharing(conn, two_users):
    x = cohorts.create_cohort(conn, two_users['alice'], 'CS', 'Fall')
    with pytest.raises(ValueError, match='display name'):
        cohorts.share_note_to_cohort(conn, two_users['alice_note'], two_users['alice'], x['id'])


def test_invites_rotation_removal_and_transfer(conn, pool):
    x, a, b = pool['x'], pool['alice'], pool['bob']
    with pytest.raises(ValueError):
        cohorts.rotate_invite(conn, x['id'], b)
    with pytest.raises(ValueError, match='Transfer'):
        cohorts.remove_member(conn, x['id'], a, a)
    new = cohorts.rotate_invite(conn, x['id'], a)
    with pytest.raises(NotFoundError):
        cohorts.join_cohort(conn, pool['eve'], x['invite_code'])
    cohorts.share_note_to_cohort(conn, pool['bob_note'], b, x['id'])
    cohorts.remove_member(conn, x['id'], a, b)
    assert store.get_note(conn, pool['bob_note'], b).visibility == 'private'
    with pytest.raises(NotFoundError):
        cohorts.join_cohort(conn, b, new['invite_code'])
    cohorts.join_cohort(conn, pool['eve'], new['invite_code'])
    cohorts.transfer_admin(conn, x['id'], a, pool['eve'])
    cohorts.remove_member(conn, x['id'], a, a)
    assert cohorts.get_cohort(conn, x['id'], pool['eve'])['role'] == 'instructor'


def test_leave_and_moderate_preserve_notes(conn, pool):
    cid, a, b = pool['x']['id'], pool['alice'], pool['bob']
    cohorts.share_note_to_cohort(conn, pool['bob_note'], b, cid)
    cohorts.moderate_note(conn, cid, pool['bob_note'], a)
    assert store.get_note(conn, pool['bob_note'], b).body
    cohorts.share_note_to_cohort(conn, pool['bob_note'], b, cid)
    cohorts.remove_member(conn, cid, b, b)
    cohorts.join_cohort(conn, b, pool['x']['invite_code'])
    assert cohorts.list_cohort_notes(conn, cid, a) == []


def test_migration_backfill_and_downgrade(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from studylink.db import make_engine
    from sqlalchemy import text, inspect
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    monkeypatch.setenv('DATABASE_URL', url)
    config = Config('alembic.ini')
    command.upgrade(config, '0015')
    engine = make_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id, email, created_at) VALUES (1, 'old@school.edu', CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO notes(id, user_id, title, body, source_type, created_at) VALUES (1, 1, 'old', 'secret', 'note', CURRENT_TIMESTAMP)"))
    command.upgrade(config, 'head')
    with engine.connect() as c:
        assert c.execute(text('SELECT visibility, shared_cohort_id FROM notes')).one() == ('private', None)
    command.downgrade(config, '0015')
    assert 'cohorts' not in inspect(engine).get_table_names()
    command.upgrade(config, 'head')
    with engine.connect() as c:
        assert c.execute(text('SELECT body FROM notes')).scalar() == 'secret'
    engine.dispose()


def test_retrieval_scope_at_every_stage_and_revocation(conn, provider, config, pool):
    from studylink.retrieval import Retriever
    from studylink.vectorstore import VectorStore
    cid, a, b = pool['x']['id'], pool['alice'], pool['bob']
    query = 'gradient descent learning rate'
    pooled = Retriever(conn, provider, config, b, cohort_id=cid)
    assert {m.note.id for m in pooled.search_notes(query)} == {pool['bob_note']}
    cohorts.share_note_to_cohort(conn, pool['alice_note'], a, cid)
    assert {m.note.id for m in pooled.search_notes(query)} == {pool['alice_note'], pool['bob_note']}
    assert {m.note.id for m in Retriever(conn, provider, config, b).search_notes(query)} == {pool['bob_note']}
    # Deliberately selecting a cohort the caller does not belong to is a 404.
    with pytest.raises(NotFoundError):
        Retriever(conn, provider, config, pool['eve'], cohort_id=cid).search_notes(query)
    with pytest.raises(NotFoundError):
        VectorStore(conn).matrix('chunk', provider.name, pool['eve'], cohort_id=cid)
    # Joining two cohorts must never broaden the selected cohort into both.
    cohorts.join_cohort(conn, a, pool['y']['invite_code'])
    cohorts.share_note_to_cohort(conn, pool['alice_note'], a, pool['y']['id'])
    assert {m.note.id for m in pooled.search_notes(query)} == {pool['bob_note']}
    cohorts.share_note_to_cohort(conn, pool['alice_note'], a, cid)
    ranked = pooled._best_chunk_per_note(provider.embed([query])[0], 10)
    cohorts.share_note_to_cohort(conn, pool['alice_note'], a, None)
    assert {m.note.id for m in pooled._build_matches(query, ranked, 10, False)} == {pool['bob_note']}
    with pytest.raises(NotFoundError):
        cohorts.get_shared_note(conn, cid, pool['alice_note'], b)


def test_attribution_in_chat_and_agent_context(conn, provider, config, pool):
    from studylink.retrieval import Retriever
    from studylink.chat import Answer, build_context
    from studylink.agent import format_note_context
    cohorts.share_note_to_cohort(conn, pool['alice_note'], pool['alice'], pool['x']['id'])
    matches = Retriever(conn, provider, config, pool['bob'], cohort_id=pool['x']['id']).search_notes('gradient descent')
    source = next(s for s in Answer(matches=matches, text='answer').as_dict()['sources'] if s['note_id'] == pool['alice_note'])
    assert source['contributor_name'] == 'Alice'
    assert source['cohort_id'] == pool['x']['id']
    assert 'contributor="Alice"' in build_context(matches)
    assert 'contributed by Alice' in format_note_context(matches)


def test_vectors_and_chunks_never_load_private_or_other_cohort_rows(conn, provider, config, pool):
    from studylink.vectorstore import VectorStore
    cid, a, b = pool['x']['id'], pool['alice'], pool['bob']
    vectors = VectorStore(conn)
    own = {c.id for c in store.list_chunks(conn, b)}
    assert set(vectors.matrix('chunk', provider.name, b, cohort_id=cid)[0]) == own
    assert set(cohorts.readable_chunks(conn, b, cid)) == own
    cohorts.share_note_to_cohort(conn, pool['alice_note'], a, cid)
    both = own | {c.id for c in store.list_chunks(conn, a)}
    assert set(vectors.matrix('chunk', provider.name, b, cohort_id=cid)[0]) == both
    cohorts.share_note_to_cohort(conn, pool['alice_note'], a, None)
    assert set(vectors.matrix('chunk', provider.name, b, cohort_id=cid)[0]) == own
    # A removed reader cannot keep using a previously constructed retriever.
    cohorts.remove_member(conn, cid, a, b)
    with pytest.raises(NotFoundError):
        cohorts.readable_chunks(conn, b, cid)


def test_cohort_evaluation_labels_and_rankings(conn, provider, config, pool, tmp_path):
    from studylink.evaluation.dataset import LabeledPair, save_labels
    from studylink.evaluation.runner import evaluate_config
    cid, a, b = pool['x']['id'], pool['alice'], pool['bob']
    cohorts.share_note_to_cohort(conn, pool['alice_note'], a, cid)
    labels = tmp_path / 'labels.json'
    save_labels([LabeledPair('Problem Set: Gradient Descent', "Alice's gradient descent notes", True),
                 LabeledPair('Problem Set: Gradient Descent', "Bob's gradient descent notes", True)], labels)
    report = evaluate_config(conn, provider, config, labels, b, reindex=False, cohort_id=cid)
    assert report.unresolved_labels == []
    assert report.macro['recall@3'] == 1.0
    assert set(report.per_assignment[0].ranked_note_ids) == {pool['alice_note'], pool['bob_note']}


def test_native_and_numpy_cohort_scopes_agree(provider, config):
    import os
    import uuid
    from sqlalchemy import delete
    from studylink.db import connect
    from studylink.schema import users, cohorts as cohort_table
    from studylink.indexing import Indexer
    from studylink.vectorstore import VectorStore
    from studylink.pgvector_support import has_vector_column
    url = os.environ.get('STUDYLINK_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('set STUDYLINK_TEST_POSTGRES_URL for native cohort search')
    c = connect(url)
    created_users, created_cohorts = [], []
    try:
        for name in ('Alice', 'Bob', 'Eve'):
            created_users.append(store.create_user(c, email=f'{uuid.uuid4()}@test.edu', display_name=name))
        a, b, e = created_users
        x = cohorts.create_cohort(c, a, 'Native cohort', 'Fall')
        created_cohorts.append(x['id'])
        cohorts.join_cohort(c, b, x['invite_code'])
        note = store.create_note(c, 'Gradient descent', 'Gradient descent learning rate alpha.', user_id=a)
        store.create_note(c, 'Private gradient descent', 'Gradient descent learning rate alpha.', user_id=e)
        for uid in created_users:
            Indexer(c, provider, config, uid).reindex()
        cohorts.share_note_to_cohort(c, note, a, x['id'])
        assert has_vector_column(c)
        v = VectorStore(c)
        query = provider.embed(['gradient descent'])[0]
        native = v._search_native(query, 'chunk', provider.name, b, 10, (), x['id'])
        portable = v._search_numpy(query, 'chunk', provider.name, b, 10, (), x['id'])
        assert [row[0] for row in native] == [row[0] for row in portable]
        assert native
        cohorts.share_note_to_cohort(c, note, a, None)
        assert v._search_native(query, 'chunk', provider.name, b, 10, (), x['id']) == []
        with pytest.raises(NotFoundError):
            v._search_native(query, 'chunk', provider.name, e, 10, (), x['id'])
    finally:
        for uid in created_users:
            for n in store.list_notes(c, uid):
                store.delete_note(c, n.id, uid)
        for cid in created_cohorts:
            c.execute(delete(cohort_table).where(cohort_table.c.id == cid))
        c.execute(delete(users).where(users.c.id.in_(created_users)))
        c.commit()
        c.close()


def test_agent_tool_attribution_and_supplied_citation_boundary(conn, provider, config, pool):
    from studylink.agent import WorkSessionAgent, resolve_citations
    from studylink.retrieval import Retriever
    cid, a, b = pool['x']['id'], pool['alice'], pool['bob']
    nid = pool['alice_note']
    cohorts.share_note_to_cohort(conn, nid, a, cid)
    agent = WorkSessionAgent(conn, Retriever(conn, provider, config, b, cohort_id=cid))
    result = agent._run_search({'query': 'gradient descent'})
    assert 'contributed by Alice' in result
    assert nid in agent.supplied_matches
    resolved = resolve_citations(conn, [nid], b, cid, list(agent.supplied_matches))
    assert resolved[0]['valid'] and resolved[0]['contributor_name'] == 'Alice'
    assert not resolve_citations(conn, [nid], b, cid, [])[0]['valid']
    cohorts.share_note_to_cohort(conn, nid, a, None)
    assert not resolve_citations(conn, [nid], b, cid, [nid])[0]['valid']
