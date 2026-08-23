#!/usr/bin/env python3
"""Immunology Dataset Quality API.

Serves the SQLite database built by api_preprocess/build_db.py. Search is
rapidfuzz over the dataset names, which are held in memory; everything else is
an indexed SQLite lookup, so the tables never have to fit in RAM.

Routes use dataset_key, a short hash of dataset_id, because a dataset_id can
contain '#', '/' or a space and would break the URL path.
"""

from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from rapidfuzz import fuzz, process, utils

DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).resolve().parent / "api.db"))
APP_NAME = os.getenv("API_DOCS_NAME", "Immunology Dataset Quality")

SEARCH_SCORER = fuzz.WRatio
# Without a processor rapidfuzz compares raw strings, so "Immport" scores 77
# against "ImmPort (Immunology Database and Analysis Portal)" and loses to
# unrelated long names. Lowercasing and stripping punctuation lifts it to 90.
SEARCH_PROCESSOR = utils.default_process
SEARCH_CUTOFF = 70.0
# rapidfuzz returns ties often, so pull a wider slice and re-sort by evidence.
SEARCH_POOL_MULTIPLIER = 10
SEARCH_POOL_MINIMUM = 200

ASPECT_NUMERIC = (
    "quality_score",
    "n_effective",
    "dispersion",
    "standard_error",
    "ci_95_lower",
    "ci_95_upper",
)
ASPECT_INTEGER = (
    "contributing_paper_count",
    "non_neutral_mention_count",
    "positive_mention_count",
    "negative_mention_count",
    "total_mention_count",
    "neutral_mention_count",
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_state: dict[str, Any] = {}


def connect() -> sqlite3.Connection:
    assert DB_PATH.is_file(), f"Database not found: {DB_PATH}. Run build_db.py first."
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def as_float(value: Any) -> float | None:
    """Blank cells and NaN both mean "no value" and become JSON null."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def as_int(value: Any) -> int | None:
    number = as_float(value)
    return None if number is None else int(number)


def as_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    text = str(value).strip().casefold()
    if text in {"true", "1", "1.0", "yes"}:
        return True
    if text in {"false", "0", "0.0", "no"}:
        return False
    return None


def split_list(value: Any) -> list[str]:
    """Keywords and aspects are pipe-separated; comma still parses older rows."""
    if not value:
        return []
    return [part.strip() for part in re.split(r"[|,]", str(value)) if part.strip()]


@asynccontextmanager
async def lifespan(_: FastAPI):
    connection = connect()
    rows = connection.execute(
        "SELECT dataset_key, dataset_name, dataset_type, mentioned_paper_count"
        " FROM datasets"
    ).fetchall()
    _state["connection"] = connection
    # rapidfuzz needs a plain list; the parallel lists keep the index alignment.
    _state["names"] = [row["dataset_name"] or "" for row in rows]
    _state["keys"] = [row["dataset_key"] for row in rows]
    _state["types"] = [row["dataset_type"] for row in rows]
    _state["papers"] = [as_int(row["mentioned_paper_count"]) or 0 for row in rows]
    logger.info("Loaded %d dataset names from %s", len(rows), DB_PATH)
    yield
    connection.close()


class TimingMiddleware:
    """Pure-ASGI timing log, kept from the original API."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                raw_query = scope.get("query_string", b"")
                query = f"?{raw_query.decode('latin-1')}" if raw_query else ""
                logger.info(
                    "[timing] %s %s%s -> %s in %.1fms",
                    scope.get("method", "-"),
                    scope.get("path", "-"),
                    query,
                    message["status"],
                    (time.perf_counter() - started) * 1000,
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)


app = FastAPI(
    title=f"{APP_NAME} API",
    description=(
        "Literature-derived quality profiles for immunology datasets. Search by "
        "dataset name, then follow dataset_key to the supporting papers and sentences."
    ),
    version="2.0.0",
    lifespan=lifespan,
    root_path=os.getenv("ROUTE_PATH", ""),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TimingMiddleware)


def dataset_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "dataset_key": row["dataset_key"],
        "dataset_id": row["dataset_id"],
        "dataset_name": row["dataset_name"],
        "dataset_type": row["dataset_type"],
        "overall_score": as_float(row["overall_score"]),
        "overall_class": row["overall_class"] or None,
        "overall_uncertain": as_bool(row["overall_uncertain"]),
        "mentioned_paper_count": as_int(row["mentioned_paper_count"]),
        "total_mention_count": as_int(row["total_mention_count"]),
    }


def fetch_dataset(dataset_key: str) -> sqlite3.Row:
    row = _state["connection"].execute(
        "SELECT * FROM datasets WHERE dataset_key = ?", (dataset_key,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown dataset_key: {dataset_key}")
    return row


def count_rows(table: str, dataset_key: str) -> int:
    # table is one of two literals chosen below, never user input.
    return _state["connection"].execute(
        f"SELECT COUNT(*) FROM {table} WHERE dataset_key = ?", (dataset_key,)
    ).fetchone()[0]


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def root() -> dict[str, Any]:
    return {
        "name": f"{APP_NAME} API",
        "datasets": len(_state.get("names", [])),
        "endpoints": [
            "/datasets/search?q=&top_n=",
            "/datasets/{dataset_key}",
            "/datasets/{dataset_key}/papers",
            "/datasets/{dataset_key}/mentions",
        ],
        "docs": "/docs",
    }


@app.get("/datasets/search", tags=["datasets"])
def search_datasets(
    q: Annotated[str, Query(description="Dataset name to match; blank browses by evidence")] = "",
    top_n: Annotated[int, Query(ge=1, le=50, description="Results to return")] = 10,
    dataset_type: Annotated[
        str | None, Query(pattern="^(dataset|accession)$", description="Optional filter")
    ] = None,
) -> dict[str, Any]:
    names, keys, types, papers = (
        _state["names"], _state["keys"], _state["types"], _state["papers"]
    )
    allowed = [i for i in range(len(names)) if dataset_type in (None, types[i])]

    query = q.strip()
    if not query:
        # No query: browse the best-evidenced datasets instead of erroring.
        chosen = sorted(allowed, key=lambda i: -papers[i])[:top_n]
        scored = [(i, None) for i in chosen]
    else:
        pool = max(SEARCH_POOL_MINIMUM, top_n * SEARCH_POOL_MULTIPLIER)
        matches = process.extract(
            query,
            {i: names[i] for i in allowed},
            scorer=SEARCH_SCORER,
            processor=SEARCH_PROCESSOR,
            score_cutoff=SEARCH_CUTOFF,
            limit=pool,
        )
        # Similarity first, evidence as the tie-break: rapidfuzz gives many exact
        # ties, and the dataset seen in more papers is the more useful answer.
        # Similarity first, evidence as the tie-break: rapidfuzz gives many exact
        # ties and the dataset seen in more papers is the more useful answer.
        matches.sort(key=lambda match: (-match[1], -papers[match[2]]))
        scored = [(index, score) for _, score, index in matches[:top_n]]

    connection = _state["connection"]
    results = []
    for index, score in scored:
        row = connection.execute(
            "SELECT * FROM datasets WHERE dataset_key = ?", (keys[index],)
        ).fetchone()
        summary = dataset_summary(row)
        summary["similarity_score"] = None if score is None else round(float(score), 2)
        results.append(summary)

    return {"query": query, "dataset_type": dataset_type, "total": len(results), "results": results}


@app.get("/datasets/lookup", tags=["datasets"])
def lookup_dataset(
    dataset_id: Annotated[str, Query(description="Semantic id, e.g. repo:216 or acc:GSE114037")],
) -> dict[str, Any]:
    """Resolve a dataset_id to its dataset_key.

    dataset_id cannot go in a URL path ('#' truncates, '/' adds segments), but as
    a query value it encodes normally, so this is the way in for a caller that
    only has the semantic id.
    """
    row = _state["connection"].execute(
        "SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id.strip(),)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown dataset_id: {dataset_id}")
    return dataset_summary(row)


@app.get("/datasets/{dataset_key}", tags=["datasets"])
def get_dataset(dataset_key: str) -> dict[str, Any]:
    row = fetch_dataset(dataset_key)
    aspects = _state["connection"].execute(
        "SELECT * FROM dataset_aspects WHERE dataset_key = ? ORDER BY rowid", (dataset_key,)
    ).fetchall()

    return {
        "dataset_key": row["dataset_key"],
        "dataset_id": row["dataset_id"],
        "dataset_name": row["dataset_name"],
        "dataset_type": row["dataset_type"],
        "coverage": {
            "mentioned_paper_count": as_int(row["mentioned_paper_count"]),
            "total_mention_count": as_int(row["total_mention_count"]),
            "positive_mention_count": as_int(row["positive_mention_count"]),
            "negative_mention_count": as_int(row["negative_mention_count"]),
            "neutral_mention_count": as_int(row["neutral_mention_count"]),
        },
        "overall": {
            "score": as_float(row["overall_score"]),
            "class": row["overall_class"] or None,
            "uncertain": as_bool(row["overall_uncertain"]),
            "uncertain_fraction": as_float(row["uncertain_fraction"]),
            "scored_aspect_count": as_int(row["scored_aspect_count"]),
        },
        "aspects": [
            {
                "aspect": aspect["aspect"],
                "score": as_float(aspect["quality_score"]),
                **{field: as_int(aspect[field]) for field in ASPECT_INTEGER},
                **{
                    field: as_float(aspect[field])
                    for field in ASPECT_NUMERIC
                    if field != "quality_score"
                },
                "ci_95": [as_float(aspect["ci_95_lower"]), as_float(aspect["ci_95_upper"])],
                "opposing_paper_evidence": as_bool(aspect["opposing_paper_evidence"]),
                "uncertain": as_bool(aspect["uncertain"]),
            }
            for aspect in aspects
        ],
    }


@app.get("/datasets/{dataset_key}/papers", tags=["datasets"])
def get_dataset_papers(
    dataset_key: str,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    row = fetch_dataset(dataset_key)
    rows = _state["connection"].execute(
        "SELECT * FROM papers WHERE dataset_key = ?"
        " ORDER BY CAST(authority_weight AS REAL) DESC, PMCID"
        " LIMIT ? OFFSET ?",
        (dataset_key, page_size, (page - 1) * page_size),
    ).fetchall()

    return {
        "dataset_key": dataset_key,
        "dataset_id": row["dataset_id"],
        "total": count_rows("papers", dataset_key),
        "page": page,
        "page_size": page_size,
        "results": [
            {
                "pmcid": paper["PMCID"],
                "pmid": paper["pmid"] or None,
                "journal": paper["journal"] or None,
                "citation_count": as_int(paper["citation_count"]),
                "journal_score": as_float(paper["journal_score"]),
                "authority_weight": as_float(paper["authority_weight"]),
                "mention_count": as_int(paper["mention_count"]),
                "positive_mention_count": as_int(paper["positive_mention_count"]),
                "negative_mention_count": as_int(paper["negative_mention_count"]),
                "neutral_mention_count": as_int(paper["neutral_mention_count"]),
                "sentiment_score": as_float(paper["sentiment_score"]),
            }
            for paper in rows
        ],
    }


@app.get("/datasets/{dataset_key}/mentions", tags=["datasets"])
def get_dataset_mentions(
    dataset_key: str,
    sentiment: Annotated[
        str | None, Query(pattern="^(Positive|Negative|Neutral)$")
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    row = fetch_dataset(dataset_key)
    connection = _state["connection"]

    where = "dataset_key = ?"
    params: list[Any] = [dataset_key]
    if sentiment:
        where += " AND sentiment = ?"
        params.append(sentiment)

    total = connection.execute(f"SELECT COUNT(*) FROM mentions WHERE {where}", params).fetchone()[0]
    rows = connection.execute(
        f"SELECT * FROM mentions WHERE {where} ORDER BY PMCID, rowid LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()

    return {
        "dataset_key": dataset_key,
        "dataset_id": row["dataset_id"],
        "sentiment": sentiment,
        "total": total,
        "page": page,
        "page_size": page_size,
        "results": [
            {
                "pmcid": mention["PMCID"],
                "pmid": mention["pmid"] or None,
                "sentence": mention["sentence"],
                "original_mention": mention["original_mention"] or None,
                "positive_keywords": split_list(mention["positive_keywords"]),
                "negative_keywords": split_list(mention["negative_keywords"]),
                "aspects": split_list(mention["aspects"]),
                "sentiment": mention["sentiment"],
                "sentiment_score": as_int(mention["sentiment_score"]),
            }
            for mention in rows
        ],
    }
