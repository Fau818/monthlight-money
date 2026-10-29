from html import unescape

from app import sync, tags
from tests.test_routes import import_sample


def tag_names(conn, tid):
    return [r[0] for r in conn.execute(
        "SELECT g.name FROM tags g JOIN transaction_tags tt ON tt.tag_id = g.id WHERE tt.transaction_id = ? ORDER BY g.name", (tid,))]


def test_tag_names_are_tidied():
    assert tags.parse_names(" Road  trip, road TRIP ,, Work ") == ["Road trip", "Work"]
    assert tags.clean_name("x" * 60) == "x" * tags.MAX_NAME


def test_tags_are_added_on_a_transactions_page_and_shown_in_the_list(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    shell = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]
    save = {"name": "Shell", "type": "regular", "date": "2026-10-03", "reviewed": "1"}
    client.post(f"/transactions/{shell}", data={**save, "tags_shown": "1", "new_tags": "Road trip, road TRIP , Work"})
    assert tag_names(conn, shell) == ["Road trip", "Work"]
    trip, work = (conn.execute("SELECT id FROM tags WHERE name = ?", (n,)).fetchone()[0] for n in ("Road trip", "Work"))

    page_text = unescape(client.get(f"/transactions/{shell}").text)
    assert f'value="{trip}" checked><span>Road trip</span>' in page_text
    listing = unescape(client.get("/transactions").text)
    assert f'href="/transactions?tag={trip}"' in listing and ">Road trip</a>" in listing
    tagged = unescape(client.get(f"/transactions?tag={trip}").text)
    assert "Shell" in tagged and "Trader Joe's" not in tagged and "1 transaction</span>" in tagged

    # The picks on the page are the tags it keeps; a form without them (an older page) leaves the tags alone.
    client.post(f"/transactions/{shell}", data={**save, "tags_shown": "1", "tag_ids": [str(work)]})
    assert tag_names(conn, shell) == ["Work"]
    client.post(f"/transactions/{shell}", data=save)
    assert tag_names(conn, shell) == ["Work"]
    conn.close()


def test_a_tag_comes_off_everything_listed_with_it(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    with conn:
        todo = tags.get_or_create(conn, "To check", "orange")
        tagged = [r[0] for r in conn.execute("SELECT id FROM transactions WHERE name IN ('Shell', 'Amazon')")]
        tags.add(conn, todo, tagged)
    listing = unescape(client.get(f"/transactions?tag={todo}").text)
    assert "Take “To check” off these 2" in listing
    response = client.post(f"/tags/{todo}/remove", data={"ids": [str(i) for i in tagged], "back": f"/transactions?tag={todo}"})
    assert "Took “To check” off 2 transactions." in unescape(response.text)
    assert conn.execute("SELECT count(*) FROM transaction_tags").fetchone()[0] == 0
    conn.close()


def test_tags_are_managed_on_the_categories_page(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    client.post("/tags", data={"name": "  Work  trip "})
    gid = conn.execute("SELECT id FROM tags WHERE name = 'Work trip'").fetchone()[0]
    shell = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]
    with conn:
        tags.add(conn, gid, [shell])
    page_text = unescape(client.get("/categories").text)
    assert 'id="tags"' in page_text and "Work trip" in page_text and "1 transaction" in page_text

    client.post(f"/tags/{gid}", data={"name": "Business", "color": "violet"})
    assert tuple(conn.execute("SELECT name, color FROM tags WHERE id = ?", (gid,)).fetchone()) == ("Business", "violet")
    client.post("/tags", data={"name": "Other"})
    assert "already a tag called other" in unescape(client.post(f"/tags/{gid}", data={"name": "other"}).text).lower()

    client.post(f"/tags/{gid}/delete")
    assert conn.execute("SELECT count(*) FROM transaction_tags").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM transactions WHERE id = ?", (shell,)).fetchone()[0] == 1  # the transaction stays
    conn.close()


def test_the_export_lists_each_transactions_tags(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    shell = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]
    with conn:
        for name in ("Road trip", "Car"):
            tags.add(conn, tags.get_or_create(conn, name), [shell])
    lines = client.get("/export.csv").text.strip().splitlines()
    assert lines[0].endswith(",tags")
    assert next(line for line in lines if ",Shell," in line).endswith(',"Car, Road trip"')
    conn.close()
