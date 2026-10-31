"""Reading a table in the reader's language: ?lang=, the profile, Accept-Language, the markers."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.catalog.descriptions import sync_project_descriptions
from lakana_core.models import Environment, Principal
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"
_TABLE = f"{_TABLES}/t"


def _setup(client: TestClient) -> None:
    """Create `gold.t (id, note)` with English header texts and French translations.

    Parameters
    ----------
    client
        The test client; its app's database is written directly, as a deploy would.
    """
    create_environment(client)
    created = client.post(
        _TABLES,
        headers=AUTH_HEADER,
        json={
            "name": "t",
            "schema": [
                {"name": "id", "type": "long", "nullable": False},
                {"name": "note", "type": "string", "nullable": True},
            ],
        },
    )
    assert created.status_code == 201, created.text
    with Session(client.app.state.engine) as session:
        env = session.scalars(select(Environment.id).where(Environment.name == "dev")).one()
        sync_project_descriptions(
            session,
            env,
            {
                "version": 11,
                "main_language": "en",
                "assets": [
                    {
                        "table": "gold.t",
                        "metadata": {
                            "label": "Sales",
                            "description": "One row per sale.",
                            "columns": {"note": "A note."},
                            "column_labels": {"note": "Note"},
                            "translations": {
                                "fr": {
                                    "label": "Ventes",
                                    "columns": {"note": {"label": "Remarque"}},
                                }
                            },
                        },
                    }
                ],
            },
        )
        session.commit()


def test_without_any_choice_the_texts_are_in_the_main_language(client: TestClient) -> None:
    _setup(client)
    body = client.get(_TABLE, headers=AUTH_HEADER).json()
    assert (body["label"], body["label_language"], body["language"], body["main_language"]) == (
        "Sales",
        "en",
        "en",
        "en",
    )
    assert body["translations"] == [{"language": "fr", "translated": 2, "total": 4}]


def test_lang_chooses_and_every_text_says_its_language(client: TestClient) -> None:
    _setup(client)
    body = client.get(_TABLE, params={"lang": "FR-ch"}, headers=AUTH_HEADER).json()
    assert (body["label"], body["label_language"]) == ("Ventes", "fr")
    assert (body["description"], body["description_language"]) == ("One row per sale.", "en")
    assert body["language"] == "fr-CH"
    note = next(c for c in body["schema"] if c["name"] == "note")
    assert (note["label"], note["label_language"], note["description_language"]) == (
        "Remarque",
        "fr",
        "en",
    )
    id_column = next(c for c in body["schema"] if c["name"] == "id")
    assert (id_column["label"], id_column["label_language"]) == (None, None)


def test_accept_language_orders_the_texts_and_each_says_its_language(client: TestClient) -> None:
    _setup(client)
    body = client.get(_TABLE, headers={**AUTH_HEADER, "Accept-Language": "it, fr;q=0.8"}).json()
    assert (body["label"], body["label_language"], body["language"]) == ("Ventes", "fr", "it")


def test_a_malformed_accept_language_is_ignored(client: TestClient) -> None:
    _setup(client)
    response = client.get(_TABLE, headers={**AUTH_HEADER, "Accept-Language": "de_DE;q=x, !!"})
    assert response.status_code == 200, response.text
    assert (response.json()["label"], response.json()["language"]) == ("Sales", "en")


def test_the_profile_beats_accept_language_and_lang_beats_both(client: TestClient) -> None:
    _setup(client)
    with Session(client.app.state.engine) as session:
        session.execute(update(Principal).values(language="fr"))
        session.commit()
    headers = {**AUTH_HEADER, "Accept-Language": "en"}
    assert client.get(_TABLE, headers=headers).json()["label"] == "Ventes"
    assert client.get(_TABLE, params={"lang": "en"}, headers=headers).json()["label"] == "Sales"


def test_an_invalid_lang_is_422_with_the_tag_text(client: TestClient) -> None:
    _setup(client)
    response = client.get(_TABLE, params={"lang": "de_DE"}, headers=AUTH_HEADER)
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "lang: `de_DE` is not a language tag Lakana takes: a language, then optionally a script "
        "and a region (BCP 47), such as en, de, fr-CH or zh-Hant; ask for a language such as de, "
        "or leave lang out"
    )


def test_the_listing_takes_lang_too(client: TestClient) -> None:
    _setup(client)
    (body,) = client.get(_TABLES, params={"lang": "fr"}, headers=AUTH_HEADER).json()
    assert body["label"] == "Ventes"
