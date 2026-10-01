def test_draft_saved_without_publishing(client, make_page, make_post, topics):
    page = make_page()
    post = make_post(page['id'], topics[0], status='ready', with_metrics=False)
    response = client.patch(f'/api/posts/{post.id}', json={'visual_title': 'Updated title', 'caption': 'Edited caption'})
    assert response.status_code == 200
    saved = client.get(f'/api/posts/{post.id}').json()
    assert saved['caption'] == 'Edited caption'
    assert saved['visual_title'] == 'Updated title'
    assert saved['status'] == 'ready'
    assert client.get('/api/posts?search=Updated&status=ready').json()['total'] == 1
    assert client.get('/api/posts?search=missing').json()['total'] == 0


def test_published_post_cannot_be_edited(client, make_page, make_post, topics):
    page = make_page()
    post = make_post(page['id'], topics[0])
    assert client.patch(f'/api/posts/{post.id}', json={'visual_title': 'Other', 'caption': 'Changed'}).status_code == 409
    assert client.post(f'/api/posts/{post.id}/regenerate-image').status_code == 409


def test_empty_analytics_not_a_winner(client):
    data = client.get('/api/analytics/summary').json()
    assert data['winning_topic'] == 'Belum cukup data'
    assert data['has_metrics'] is False
    assert data['last_updated'] is None


def test_draft_requires_title(client, make_page, make_post, topics):
    page = make_page()
    post = make_post(page['id'], topics[0], status='ready')
    assert client.patch(f'/api/posts/{post.id}', json={'visual_title': ' ', 'caption': 'ok'}).status_code == 422
