"""Home feed: every post, newest first, paged by offset with full captions."""


def test_feed_pages_through_all_posts_newest_first_with_captions(client, make_page, make_post, topics):
    page_a, page_b = make_page("100"), make_page("200")
    for day in range(1, 8):
        make_post(page_a["id"] if day % 2 else page_b["id"], topics[day % len(topics)], days_ago=day)

    seen = []
    offset = 0
    while True:
        data = client.get(f"/api/posts?with_caption=true&limit=3&offset={offset}").json()
        assert all(item["caption"] == "caption" for item in data["items"])
        seen += data["items"]
        offset += len(data["items"])
        if not data["has_more"]:
            break

    assert len(seen) == 7 == data["total"]
    assert len({p["id"] for p in seen}) == 7
    dates = [p["created_at"] for p in seen]
    assert dates == sorted(dates, reverse=True)
    assert {p["page_id"] for p in seen} == {page_a["id"], page_b["id"]}


def test_post_list_stays_light_without_with_caption(client, make_page, make_post, topics):
    make_post(make_page("100")["id"], topics[0])
    item = client.get("/api/posts").json()["items"][0]
    assert "caption" not in item and "prompt_used" not in item
    assert item["caption_preview"] == "caption"
