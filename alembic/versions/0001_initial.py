"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scryfall_cards",
        sa.Column("scryfall_id", sa.String(length=36), primary_key=True),
        sa.Column("oracle_id", sa.String(length=36)),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("set_code", sa.String(length=10), nullable=False),
        sa.Column("set_name", sa.String(), nullable=False),
        sa.Column("collector_number", sa.String(length=16), nullable=False),
        sa.Column("rarity", sa.String(length=16), nullable=False),
        sa.Column("lang", sa.String(length=8), nullable=False, server_default="en"),
        sa.Column("type_line", sa.Text()),
        sa.Column("oracle_text", sa.Text()),
        sa.Column("mana_cost", sa.String(length=64)),
        sa.Column("cmc", sa.Float()),
        sa.Column("colors", sa.String(length=32)),
        sa.Column("color_identity", sa.String(length=32)),
        sa.Column("finishes", sa.String(length=64)),
        sa.Column("image_normal_uri", sa.Text()),
        sa.Column("image_small_uri", sa.Text()),
        sa.Column("image_art_crop_uri", sa.Text()),
        sa.Column("card_faces_json", sa.Text()),
        sa.Column("rulings_uri", sa.Text()),
        sa.Column("tcgplayer_id", sa.Integer()),
        sa.Column("tcgplayer_etched_id", sa.Integer()),
        sa.Column("legalities_json", sa.Text()),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scryfall_set_num", "scryfall_cards", ["set_code", "collector_number"])
    op.create_index("ix_scryfall_cards_oracle_id", "scryfall_cards", ["oracle_id"])
    op.create_index("ix_scryfall_cards_tcgplayer_id", "scryfall_cards", ["tcgplayer_id"])
    op.create_index("ix_scryfall_name_nocase", "scryfall_cards", ["name"])

    op.create_table(
        "card_rulings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "scryfall_id",
            sa.String(length=36),
            sa.ForeignKey("scryfall_cards.scryfall_id", ondelete="CASCADE"),
        ),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.Date()),
        sa.Column("comment", sa.Text(), nullable=False),
    )
    op.create_index("ix_card_rulings_scryfall_id", "card_rulings", ["scryfall_id"])

    op.create_table(
        "collection_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "scryfall_id",
            sa.String(length=36),
            sa.ForeignKey("scryfall_cards.scryfall_id"),
            nullable=False,
        ),
        sa.Column("finish", sa.String(length=16), nullable=False, server_default="nonfoil"),
        sa.Column("condition", sa.String(length=8), nullable=False, server_default="NM"),
        sa.Column("language", sa.String(length=8), nullable=False, server_default="en"),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("purchase_price", sa.Float()),
        sa.Column("purchase_currency", sa.String(length=8)),
        sa.Column("purchase_date", sa.Date()),
        sa.Column("notes", sa.Text()),
        sa.Column("for_trade", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("altered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("misprint", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("scryfall_id", "finish", "condition", "language", name="uq_entry_key"),
        sa.CheckConstraint("quantity > 0", name="ck_entry_qty_pos"),
    )
    op.create_index("ix_collection_entries_scryfall_id", "collection_entries", ["scryfall_id"])
    op.create_index("ix_entries_for_trade", "collection_entries", ["for_trade"])

    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("color", sa.String(length=16)),
    )

    op.create_table(
        "entry_tags",
        sa.Column(
            "entry_id",
            sa.Integer(),
            sa.ForeignKey("collection_entries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            sa.Integer(),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    op.create_table(
        "price_history",
        sa.Column("tcgplayer_id", sa.Integer(), primary_key=True),
        sa.Column("sub_type", sa.String(length=16), primary_key=True),
        sa.Column("as_of", sa.Date(), primary_key=True),
        sa.Column("low_price", sa.Float()),
        sa.Column("mid_price", sa.Float()),
        sa.Column("high_price", sa.Float()),
        sa.Column("market_price", sa.Float()),
        sa.Column("direct_low_price", sa.Float()),
    )
    op.create_index("ix_price_recent", "price_history", ["tcgplayer_id", "sub_type", "as_of"])

    op.create_table(
        "import_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.String()),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("rows_total", sa.Integer()),
        sa.Column("rows_imported", sa.Integer()),
        sa.Column("rows_skipped", sa.Integer()),
        sa.Column("rows_unmatched", sa.Integer()),
        sa.Column("error", sa.Text()),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("import_runs")
    op.drop_index("ix_price_recent", table_name="price_history")
    op.drop_table("price_history")
    op.drop_table("entry_tags")
    op.drop_table("tags")
    op.drop_index("ix_entries_for_trade", table_name="collection_entries")
    op.drop_index("ix_collection_entries_scryfall_id", table_name="collection_entries")
    op.drop_table("collection_entries")
    op.drop_index("ix_card_rulings_scryfall_id", table_name="card_rulings")
    op.drop_table("card_rulings")
    op.drop_index("ix_scryfall_name_nocase", table_name="scryfall_cards")
    op.drop_index("ix_scryfall_cards_tcgplayer_id", table_name="scryfall_cards")
    op.drop_index("ix_scryfall_cards_oracle_id", table_name="scryfall_cards")
    op.drop_index("ix_scryfall_set_num", table_name="scryfall_cards")
    op.drop_table("scryfall_cards")
