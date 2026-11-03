"""The glossary's rows: a term, its texts, its links and its related ids (#375)."""

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, TimestampMixin


class GlossaryTerm(Base, TimestampMixin):
    """One term of an environment's glossary, owned by the repository that deployed it.

    Written only by `lakana_core.catalog.glossary` at a deploy and a rollback, and deleted at
    undeploy; never through the API. The id is kept while the key stays.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier; kept while the term's key stays.
    environment_id : uuid.UUID
        The environment; the rows go with it.
    repository_id : uuid.UUID
        The owning repository; the rows go with it.
    term_key : str
        The term id, unique in the environment.
    name : str
        The main-language name.
    definition : str
        The main-language definition.
    owner : str
        Who answers for the term.
    main_language : str or None
        The project's `main_language`; `None` means the instance's.
    """

    __tablename__ = "glossary_terms"
    __table_args__ = (UniqueConstraint("environment_id", "term_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    term_key: Mapped[str] = mapped_column(String(63))
    name: Mapped[str] = mapped_column(Text)
    definition: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(Text)
    main_language: Mapped[str | None] = mapped_column(String(35), nullable=True, default=None)


class GlossaryTermText(Base):
    """A synonym, or a translated name or definition, of a term.

    Attributes
    ----------
    term_id : uuid.UUID
        The term; the rows go with it.
    language : str
        ``""`` for the term's main language (its synonyms), else a canonical tag.
    field : str
        ``name``, ``definition`` or ``synonym``.
    position : int
        The synonym's place, 0 for a name or definition.
    text : str
        The text.
    """

    __tablename__ = "glossary_term_texts"

    term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("glossary_terms.id", ondelete="CASCADE"), primary_key=True
    )
    language: Mapped[str] = mapped_column(String(35), primary_key=True)
    field: Mapped[str] = mapped_column(String(16), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class GlossaryTermLink(Base):
    """A term's link to a table or a column, by name.

    No foreign key to `tables`: a dropped table or column never removes a link; it reads as
    missing.

    Attributes
    ----------
    term_id : uuid.UUID
        The term; the rows go with it.
    position : int
        The link's place in the term.
    table_name : str
        The dotted table name the file gave.
    column_name : str or None
        The column; `None` links the table itself.
    """

    __tablename__ = "glossary_term_links"
    __table_args__ = (Index("ix_glossary_term_links_table_name", "table_name"),)

    term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("glossary_terms.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_name: Mapped[str] = mapped_column(String(1024))
    column_name: Mapped[str | None] = mapped_column(String(1024), nullable=True, default=None)


class GlossaryTermRelated(Base):
    """A related term id a term names, by key.

    Attributes
    ----------
    term_id : uuid.UUID
        The term; the rows go with it.
    position : int
        The id's place in `related:`.
    related_key : str
        The related term's id; it may name a term no repository defines any more.
    """

    __tablename__ = "glossary_term_related"

    term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("glossary_terms.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    related_key: Mapped[str] = mapped_column(String(63))
