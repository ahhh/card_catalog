"""Tests for services.collection: filter / sort / paginate + edit."""

from __future__ import annotations

import json

import pytest

from card_catalog.db.models import CollectionEntry, ScryfallCard
from card_catalog.services import collection as svc


# ---- seeding helpers -------------------------------------------------------


def _card(
    db,
    *,
    sid,
    name,
    set_code="lea",
    colors=None,
    rarity="common",
    cmc=1.0,
    tcgplayer_id=None,
    finishes=("nonfoil",),
):
    c = ScryfallCard(
        scryfall_id=sid,
        oracle_id=f"oracle-{sid}",
        name=name,
        set_code=set_code,
        set_name=f"Set {set_code}",
        collector_number="1",
        rarity=rarity,
        lang="en",
        type_line="Instant",
        oracle_text="text",
        cmc=cmc,
        colors=json.dumps(list(colors or [])),
        color_identity=json.dumps(list(colors or [])),
        finishes=json.dumps(list(finishes)),
        raw_json="{}",
        tcgplayer_id=tcgplayer_id,
    )
    db.add(c)
    db.commit()
    return c


def _entry(db, card, qty=1, finish="nonfoil", condition="NM", language="en"):
    e = CollectionEntry(
        scryfall_id=card.scryfall_id,
        finish=finish,
        condition=condition,
        language=language,
        quantity=qty,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


@pytest.fixture
def seeded(db):
    """10 entries across colors/rarities/cmc. Returns dict of named entries."""
    cards = {}
    cards["bolt"] = _card(
        db, sid="c-bolt", name="Lightning Bolt", colors=["R"], rarity="common", cmc=1.0
    )
    cards["counter"] = _card(
        db, sid="c-counter", name="Counterspell", colors=["U"], rarity="common", cmc=2.0
    )
    cards["wrath"] = _card(
        db, sid="c-wrath", name="Wrath of God", colors=["W"], rarity="rare", cmc=4.0
    )
    cards["mox"] = _card(
        db, sid="c-mox", name="Mox Pearl", colors=[], rarity="mythic", cmc=0.0
    )
    cards["river"] = _card(
        db, sid="c-river", name="Steam Vents", colors=[], rarity="rare", cmc=0.0
    )

    entries = {}
    entries["bolt"] = _entry(db, cards["bolt"], qty=4)
    entries["bolt_foil"] = _entry(db, cards["bolt"], qty=1, finish="foil")
    entries["counter"] = _entry(db, cards["counter"], qty=2)
    entries["wrath"] = _entry(db, cards["wrath"], qty=3)
    entries["mox"] = _entry(db, cards["mox"], qty=1)
    entries["river"] = _entry(db, cards["river"], qty=2)
    return {"cards": cards, "entries": entries}


# ---- FilterSpec coercion ---------------------------------------------------


def test_filterspec_csv_lists():
    spec = svc.FilterSpec(set_code="lea,leb", colors=["R", "U"])
    assert spec.set_code == ["lea", "leb"]
    assert spec.colors == ["R", "U"]


def test_filterspec_bad_page_defaults_to_1():
    spec = svc.FilterSpec(page="bad")  # type: ignore[arg-type]
    assert spec.page == 1


def test_filterspec_page_size_clamped():
    assert svc.FilterSpec(page_size=999).page_size == 240
    assert svc.FilterSpec(page_size=0).page_size == 1


def test_filterspec_for_trade_parses_strings():
    assert svc.FilterSpec(for_trade="1").for_trade is True
    assert svc.FilterSpec(for_trade="off").for_trade is False
    assert svc.FilterSpec(for_trade="").for_trade is None


def test_filterspec_querystring_minimal():
    qs = svc.FilterSpec().querystring()
    assert qs == ""


def test_filterspec_querystring_has_filters():
    spec = svc.FilterSpec(q="Bolt", set_code=["lea"], page=2, sort="-cmc")
    qs = spec.querystring()
    assert "q=Bolt" in qs
    assert "set_code=lea" in qs
    assert "sort=-cmc" in qs
    assert "page=2" in qs


# ---- search ----------------------------------------------------------------


def test_search_returns_all_entries(db, seeded):
    result = svc.search(db, svc.FilterSpec())
    assert result.total == 6
    assert len(result.entries) == 6


def test_search_text_query(db, seeded):
    result = svc.search(db, svc.FilterSpec(q="Bolt"))
    names = [e.card.name for e in result.entries]
    assert names == ["Lightning Bolt", "Lightning Bolt"]


def test_search_color_filter_includes(db, seeded):
    # Known limitation: colors filter is JSON-LIKE on a stored string.
    result = svc.search(db, svc.FilterSpec(colors=["R"]))
    assert all("R" in e.card.colors for e in result.entries)
    assert {e.card.name for e in result.entries} == {"Lightning Bolt"}


def test_search_rarity_filter(db, seeded):
    result = svc.search(db, svc.FilterSpec(rarity=["mythic"]))
    assert [e.card.name for e in result.entries] == ["Mox Pearl"]


def test_search_cmc_range(db, seeded):
    result = svc.search(db, svc.FilterSpec(cmc_min=2.0, cmc_max=4.0))
    names = sorted(e.card.name for e in result.entries)
    assert names == ["Counterspell", "Wrath of God"]


def test_search_finish_filter(db, seeded):
    result = svc.search(db, svc.FilterSpec(finish=["foil"]))
    assert len(result.entries) == 1
    assert result.entries[0].finish == "foil"


def test_search_qty_range(db, seeded):
    result = svc.search(db, svc.FilterSpec(qty_min=3))
    qtys = [e.quantity for e in result.entries]
    assert all(q >= 3 for q in qtys)
    assert len(qtys) == 2  # bolt(4) + wrath(3)


def test_search_sort_by_cmc_desc(db, seeded):
    result = svc.search(db, svc.FilterSpec(sort="-cmc"))
    cmcs = [e.card.cmc for e in result.entries]
    assert cmcs == sorted(cmcs, reverse=True)


def test_search_pagination(db, seeded):
    page1 = svc.search(db, svc.FilterSpec(page=1, page_size=2))
    page2 = svc.search(db, svc.FilterSpec(page=2, page_size=2))
    assert page1.total == 6
    assert len(page1.entries) == 2
    assert len(page2.entries) == 2
    # Pages should be disjoint.
    ids_seen = {e.id for e in page1.entries} | {e.id for e in page2.entries}
    assert len(ids_seen) == 4


def test_search_total_quantity_aggregates(db, seeded):
    result = svc.search(db, svc.FilterSpec())
    assert result.total_quantity == 4 + 1 + 2 + 3 + 1 + 2  # 13


# ---- update_entry ----------------------------------------------------------


def test_update_entry_simple_field(db, seeded):
    e = seeded["entries"]["counter"]
    out = svc.update_entry(db, e.id, {"quantity": 5})
    assert out is not None
    assert out.quantity == 5


def test_update_entry_quantity_zero_deletes(db, seeded):
    e = seeded["entries"]["counter"]
    out = svc.update_entry(db, e.id, {"quantity": 0})
    assert out is None
    assert db.get(CollectionEntry, e.id) is None


def test_update_entry_unknown_id_raises(db):
    with pytest.raises(KeyError):
        svc.update_entry(db, 99999, {"quantity": 1})


def test_update_entry_invalid_condition_raises(db, seeded):
    e = seeded["entries"]["counter"]
    with pytest.raises(svc.EditError):
        svc.update_entry(db, e.id, {"condition": "BAD"})


def test_update_entry_invalid_finish_raises(db, seeded):
    e = seeded["entries"]["counter"]
    with pytest.raises(svc.EditError):
        svc.update_entry(db, e.id, {"finish": "shiny"})


def test_update_entry_negative_quantity_raises(db, seeded):
    e = seeded["entries"]["counter"]
    with pytest.raises(svc.EditError):
        svc.update_entry(db, e.id, {"quantity": -3})


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("condition", "LP", "LP"),
        ("finish", "foil", "foil"),
        ("language", "JA", "ja"),
        ("notes", "neat card", "neat card"),
        ("for_trade", "true", 1),
        ("altered", "no", 0),
        ("purchase_price", "12.50", 12.5),
        ("purchase_currency", "usd", "USD"),
        ("purchase_date", "2024-01-15", "2024-01-15"),
    ],
)
def test_update_entry_every_editable(db, seeded, field, value, expected):
    e = seeded["entries"]["wrath"]
    out = svc.update_entry(db, e.id, {field: value})
    actual = getattr(out, field)
    if field == "purchase_date":
        assert actual.isoformat() == expected
    else:
        assert actual == expected


# ---- bulk_update -----------------------------------------------------------


def test_bulk_update_applies_patch(db, seeded):
    ids = [seeded["entries"]["counter"].id, seeded["entries"]["wrath"].id]
    n = svc.bulk_update(db, ids, {"for_trade": "1"})
    assert n == 2
    refreshed = [db.get(CollectionEntry, i) for i in ids]
    assert all(e.for_trade == 1 for e in refreshed)


def test_bulk_update_strips_quantity(db, seeded):
    """bulk_update never mass-deletes via quantity=0."""
    ids = [seeded["entries"]["counter"].id]
    n = svc.bulk_update(db, ids, {"quantity": 0})
    assert n == 0
    assert db.get(CollectionEntry, ids[0]) is not None


# ---- delete + bulk_delete --------------------------------------------------


def test_delete_entry(db, seeded):
    e = seeded["entries"]["bolt"]
    svc.delete_entry(db, e.id)
    assert db.get(CollectionEntry, e.id) is None


def test_delete_entry_unknown_raises(db):
    with pytest.raises(KeyError):
        svc.delete_entry(db, 99999)


def test_bulk_delete_returns_count(db, seeded):
    ids = [seeded["entries"]["bolt"].id, seeded["entries"]["counter"].id]
    n = svc.bulk_delete(db, ids)
    assert n == 2


# ---- tags ------------------------------------------------------------------


def test_add_and_remove_tag(db, seeded):
    e = seeded["entries"]["bolt"]
    tag = svc.add_tag(db, e.id, "burn")
    assert tag.name == "burn"
    refreshed = db.get(CollectionEntry, e.id)
    assert any(t.name == "burn" for t in refreshed.tags)
    svc.remove_tag(db, e.id, tag.id)
    db.expire_all()
    refreshed = db.get(CollectionEntry, e.id)
    assert all(t.id != tag.id for t in refreshed.tags)


def test_bulk_add_tag(db, seeded):
    ids = [seeded["entries"]["bolt"].id, seeded["entries"]["counter"].id]
    n = svc.bulk_add_tag(db, ids, "important")
    assert n == 2
    for i in ids:
        e = db.get(CollectionEntry, i)
        assert any(t.name == "important" for t in e.tags)


# ---- facet helpers ---------------------------------------------------------


def test_distinct_sets(db, seeded):
    out = svc.distinct_sets(db)
    # All seeded cards share set_code "lea".
    codes = {row[0] for row in out}
    assert codes == {"lea"}


def test_distinct_languages(db, seeded):
    langs = svc.distinct_languages(db)
    assert langs == ["en"]


def test_total_collection_size(db, seeded):
    assert svc.total_collection_size(db) == 6


# ---- get_card / detail helpers --------------------------------------------


def test_get_card_and_entries_for_card(db, seeded):
    card = svc.get_card(db, "c-bolt")
    assert card is not None
    entries = svc.get_entries_for_card(db, "c-bolt")
    assert len(entries) == 2  # nonfoil + foil


def test_get_card_missing(db):
    assert svc.get_card(db, "nope") is None
