"""Offline Chinese retrieval comparison using real archive/import/index services.

Relatedness labels are explicit engineering judgments, not confirmed human gold.
Every evaluation owns a temporary instance; no production database or network is
used. Historical EMS bytes are manually registered, not claimed as live coverage.
"""

import argparse
import hashlib
import json
import platform
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "docs/validation/search-cases.json"
SOURCE_FILES = (
    "deploy/evaluate_search.py",
    "src/signalnest/search.py",
    "src/signalnest/contracts.py",
    "src/signalnest/schema.py",
    "src/signalnest/ingestion.py",
    "src/signalnest/rawstore.py",
    "src/signalnest/storage.py",
    "src/signalnest/parsing.py",
    "src/signalnest/ems_parsing.py",
    "src/signalnest/migrations/versions/0007_history_search.py",
    "src/signalnest/migrations/versions/0008_list_references.py",
)
K_VALUES = (1, 3, 5)


class EvaluationError(ValueError):
    """Finite input/consistency errors; no HTML or arbitrary exceptions."""


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def _fixture(path):
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise EvaluationError("search_fixture_path_invalid")
    resolved = (ROOT / candidate).resolve()
    if not resolved.is_relative_to(ROOT / "research/fixtures"):
        raise EvaluationError("search_fixture_path_invalid")
    return resolved


def _manifest(case_file):
    encoded = Path(case_file).read_bytes()
    try:
        manifest = json.loads(encoded)
        if manifest["schema_version"] != 1 or manifest["human_gold"] is not False:
            raise EvaluationError("search_manifest_invalid")
        corpus = manifest["corpus"]
        queries = manifest["queries"]
        corpus_ids = {item["corpus_id"] for item in corpus}
        query_ids = {item["query_id"] for item in queries}
        if (
            not corpus
            or not queries
            or len(corpus_ids) != len(corpus)
            or len(query_ids) != len(queries)
        ):
            raise EvaluationError("search_manifest_invalid")
        identities = {(item["source_id"], item["source_document_id"]) for item in corpus}
        if len(identities) != len(corpus):
            raise EvaluationError("search_manifest_invalid")
        for item in corpus:
            if item["parser"] not in {"whu-student-notices", "ems-notices"}:
                raise EvaluationError("search_parser_invalid")
            if type(item["fetched_at"]) is not int or item["fetched_at"] < 0:
                raise EvaluationError("search_manifest_invalid")
            body = _fixture(item["fixture"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != item["sha256"]:
                raise EvaluationError("search_fixture_digest_mismatch")
        for query in queries:
            related = query["relevant_corpus_ids"]
            if (
                query["label_origin"] != "engineering_relevance_judgment_unconfirmed"
                or not isinstance(query["query"], str)
                or not query["query"].strip()
                or not set(related).issubset(corpus_ids)
                or len(set(related)) != len(related)
            ):
                raise EvaluationError("search_manifest_invalid")
            dates = [
                date.fromisoformat(query[name])
                for name in ("date_from", "date_to")
                if name in query
            ]
            if len(dates) == 2 and dates[0] > dates[1]:
                raise EvaluationError("search_manifest_invalid")
        aliases = manifest["compared_aliases"]
        if not isinstance(aliases, dict) or any(
            not isinstance(term, str)
            or not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value for value in values)
            for term, values in aliases.items()
        ):
            raise EvaluationError("search_manifest_invalid")
    except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, EvaluationError):
            raise
        raise EvaluationError("search_manifest_invalid") from exc
    return manifest, hashlib.sha256(encoded).hexdigest()


def _normalize(text):
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def _baseline(connection, query, *, method, aliases=None):
    """Run actual SQLite comparison queries, not a retrieval call-count mock."""
    groups = [(aliases or {}).get(term, [term]) for term in _normalize(query["query"]).split()]
    parameters = {}
    filters = []
    title_hits = []
    match_groups = []
    for number, group in enumerate(groups):
        clauses = []
        title_clauses = []
        for position, term in enumerate(group):
            name = f"term_{number}_{position}"
            parameters[name] = term
            clauses.append(f"(instr(d.title,:{name})>0 OR instr(d.body,:{name})>0)")
            title_clauses.append(f"instr(d.title,:{name})>0")
        filters.append("(" + " OR ".join(clauses) + ")")
        title_hits.append("CASE WHEN (" + " OR ".join(title_clauses) + ") THEN 1 ELSE 0 END")
        if method == "unicode61" or all(len(term) >= 3 for term in group):
            match_groups.append(
                "(" + " OR ".join('"' + term.replace('"', '""') + '"' for term in group) + ")"
            )
    for field, operator in (("source_id", "="), ("date_from", ">="), ("date_to", "<=")):
        if field in query:
            column = "source_id" if field == "source_id" else "published_date"
            filters.append(f"d.{column}{operator}:{field}")
            parameters[field] = query[field]
    joins = " FROM corpus d "
    if method in {"unicode61", "trigram"} and match_groups:
        table = "unicode_index" if method == "unicode61" else "trigram_index"
        joins += f"JOIN {table} ON {table}.rowid=d.document_id "
        filters.append(f"{table} MATCH :match")
        parameters["match"] = " AND ".join(match_groups)
        order = f"bm25({table},5.0,1.0),d.published_date DESC,d.document_id"
    else:
        order = "(" + "+".join(title_hits) + ") DESC,d.published_date DESC,d.document_id"
    statement = (
        "SELECT d.corpus_id" + joins + "WHERE " + " AND ".join(filters) + " ORDER BY " + order
    )
    return [row[0] for row in connection.execute(statement, parameters)]


def _metrics(queries, results):
    relevant_queries = [query for query in queries if query["relevant_corpus_ids"]]
    recall = {}
    precision = {}
    returned_precision = {}
    for k in K_VALUES:
        hits = [
            len(set(results[query["query_id"]][:k]) & set(query["relevant_corpus_ids"]))
            for query in relevant_queries
        ]
        recall[str(k)] = round(
            sum(
                found / len(query["relevant_corpus_ids"])
                for query, found in zip(relevant_queries, hits, strict=True)
            )
            / len(relevant_queries),
            6,
        )
        precision[str(k)] = round(sum(found / k for found in hits) / len(relevant_queries), 6)
        returned_precision[str(k)] = round(
            sum(
                found / len(results[query["query_id"]][:k]) if results[query["query_id"]] else 0.0
                for query, found in zip(relevant_queries, hits, strict=True)
            )
            / len(relevant_queries),
            6,
        )
    empty_queries = [query for query in queries if not query["relevant_corpus_ids"]]
    return {
        "relevant_query_count": len(relevant_queries),
        "macro_recall_at_k": recall,
        "macro_precision_at_k": precision,
        "macro_precision_among_returned_at_k": returned_precision,
        "empty_query_count": len(empty_queries),
        "empty_query_correct": sum(not results[query["query_id"]] for query in empty_queries),
    }


def evaluate_cases(case_file=DEFAULT_CASES):
    """Import real bytes, compare SQLite methods, then query/rebuild production index."""
    before = source_hashes()
    manifest, manifest_digest = _manifest(case_file)
    from signalnest.config import StorageSettings
    from signalnest.contracts import PageInput
    from signalnest.ems_parsing import parse_ems_notice
    from signalnest.ingestion import ResponseInput, import_page
    from signalnest.instance_lock import writer_lock
    from signalnest.parsing import parse_notice
    from signalnest.rawstore import RawStore
    from signalnest.schema import documents
    from signalnest.search import SearchQuery, get_notice, rebuild_index, search_notices
    from signalnest.storage import initialize_storage, open_initialized_engine

    if source_hashes() != before:
        raise EvaluationError("search_source_changed")
    methods = {
        "sqlite_literal_and": {},
        "sqlite_fts5_unicode61": {},
        "sqlite_fts5_trigram_literal": {},
        "sqlite_fts5_trigram_fixed_aliases": {},
        "production": {},
    }
    production_pages = {}
    corpus_rows = []
    with tempfile.TemporaryDirectory(prefix="signalnest-search-evaluation-") as temporary:
        directory = Path(temporary).resolve()
        settings = StorageSettings(
            data_dir=str(directory / "data"), database=str(directory / "db.sqlite")
        )
        revision = initialize_storage(settings)
        engine = open_initialized_engine(settings.database)
        try:
            with writer_lock(settings.database):
                store = RawStore(settings.data_dir)
                for item in manifest["corpus"]:
                    body = _fixture(item["fixture"]).read_bytes()
                    parser = parse_ems_notice if item["parser"] == "ems-notices" else parse_notice
                    parsed = parser(PageInput(content=body, page_url=item["source_url"]))
                    if parsed.source_document_id != item["source_document_id"]:
                        raise EvaluationError("search_fixture_identity_mismatch")
                    with engine.begin() as connection:
                        document_id = connection.execute(
                            documents.insert().values(
                                source_id=item["source_id"],
                                source_document_id=item["source_document_id"],
                                detail_url=item["source_url"],
                                discovered_title=parsed.content.title,
                                discovered_at=item["fetched_at"],
                                discovery_origin="unknown",
                            )
                        ).inserted_primary_key[0]
                    result = import_page(
                        engine,
                        store,
                        ResponseInput(
                            page_type="notice",
                            source_id=item["source_id"],
                            source_document_id=item["source_document_id"],
                            requested_url=item["source_url"],
                            final_url=item["source_url"],
                            fetched_at=item["fetched_at"],
                            status_code=200,
                            body_state="complete",
                            content_type="text/html",
                        ),
                        body,
                        item["fetched_at"] + 1,
                        notice_parser=parser,
                        processing_origin="offline",
                    )
                    if result.outcome != "processed":
                        raise EvaluationError("search_import_failed")
                    detail = get_notice(engine, document_id)
                    corpus_rows.append(
                        {
                            **item,
                            "document_id": document_id,
                            "version_id": detail.version_id,
                            "title": detail.content.title,
                            "published_date": detail.content.published_date.isoformat(),
                            "content_sha256": detail.content_sha256,
                            "parser_version": detail.parser_version,
                            "body_text": detail.content.body_text,
                        }
                    )
                rebuild = rebuild_index(engine)
            identifiers = {row["document_id"]: row["corpus_id"] for row in corpus_rows}
            with sqlite3.connect(":memory:") as comparison:
                comparison.execute(
                    "CREATE TABLE corpus(document_id INTEGER PRIMARY KEY,corpus_id TEXT,"
                    "source_id TEXT,published_date TEXT,title TEXT,body TEXT)"
                )
                for tokenizer, table in (
                    ("unicode61", "unicode_index"),
                    ("trigram", "trigram_index"),
                ):
                    comparison.execute(
                        f"CREATE VIRTUAL TABLE {table} "
                        f"USING fts5(title,body,tokenize='{tokenizer}')"
                    )
                for row in corpus_rows:
                    title, body = _normalize(row["title"]), _normalize(row["body_text"])
                    comparison.execute(
                        "INSERT INTO corpus VALUES(?,?,?,?,?,?)",
                        (
                            row["document_id"],
                            row["corpus_id"],
                            row["source_id"],
                            row["published_date"],
                            title,
                            body,
                        ),
                    )
                    for table in ("unicode_index", "trigram_index"):
                        comparison.execute(
                            f"INSERT INTO {table}(rowid,title,body) VALUES(?,?,?)",
                            (row["document_id"], title, body),
                        )
                for query in manifest["queries"]:
                    identifier = query["query_id"]
                    for name, method, aliases in (
                        ("sqlite_literal_and", "literal", None),
                        ("sqlite_fts5_unicode61", "unicode61", None),
                        ("sqlite_fts5_trigram_literal", "trigram", None),
                        (
                            "sqlite_fts5_trigram_fixed_aliases",
                            "trigram",
                            manifest["compared_aliases"],
                        ),
                    ):
                        methods[name][identifier] = _baseline(
                            comparison, query, method=method, aliases=aliases
                        )
                    selected = SearchQuery(
                        **{key: query[key] for key in SearchQuery.model_fields if key in query},
                        limit=100,
                    )
                    page = search_notices(engine, selected)
                    methods["production"][identifier] = [
                        identifiers[hit.document_id] for hit in page.items
                    ]
                    production_pages[identifier] = page.model_dump(mode="json")
            with writer_lock(settings.database):
                rebuild_index(engine)
            for query in manifest["queries"]:
                selected = SearchQuery(
                    **{key: query[key] for key in SearchQuery.model_fields if key in query},
                    limit=100,
                )
                if (
                    search_notices(engine, selected).model_dump(mode="json")
                    != production_pages[query["query_id"]]
                ):
                    raise EvaluationError("search_rebuild_results_changed")
        finally:
            engine.dispose()
    if source_hashes() != before:
        raise EvaluationError("search_source_changed")
    queries = manifest["queries"]
    mismatches = [
        query["query_id"]
        for query in queries
        if (
            not set(query["relevant_corpus_ids"]).issubset(
                methods["production"][query["query_id"]][:5]
            )
            if query["relevant_corpus_ids"]
            else bool(methods["production"][query["query_id"]])
        )
    ]
    return {
        "schema_version": 1,
        "kind": "offline_production_lexical_search_evaluation",
        "recorded_at": datetime.now(UTC).isoformat(),
        "human_gold": False,
        "note": manifest["note"],
        "query_origin": manifest["query_origin"],
        "source_sha256": before,
        "manifest_sha256": manifest_digest,
        "environment": {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
        },
        "storage_revision": revision,
        "corpus": [
            {key: value for key, value in row.items() if key != "body_text"} for row in corpus_rows
        ],
        "methods": {
            name: {"metrics": _metrics(queries, results), "query_results": results}
            for name, results in methods.items()
        },
        "queries": queries,
        "production_pages": production_pages,
        "summary": {
            "corpus_count": len(corpus_rows),
            "query_count": len(queries),
            "indexed_count": rebuild.indexed_count,
            "rebuild_results_identical": True,
            "engineering_mismatch_queries": mismatches,
            "production_matches_fixed_alias_candidate": methods["production"]
            == methods["sqlite_fts5_trigram_fixed_aliases"],
        },
        "metric_definitions": {
            "macro_recall_at_k": (
                "非空相关集逐查询 |相关∩前K|/|相关| 的平均；空集另报，不计入Recall。"
            ),
            "macro_precision_at_k": "同一批非空查询逐查询 |相关∩前K|/K 的平均；不足K不补结果。",
            "macro_precision_among_returned_at_k": (
                "同一批非空查询逐查询 |相关∩前K|/|返回前K| 的平均；无返回为0。"
            ),
        },
        "limitations": [
            "9篇公开历史HTML及工程构造的真实查询类别，不是用户日志或人工确认gold，不能外推总体中文检索质量。",
            "仅正文标题与文本，不读取图片、附件文件或受登录保护的当期助教内容。",
            "查询不解析自然语言相对日期；日期是显式站点日历日期过滤，与获取时间无关。",
            "词法相关不表示当前可申请、具备资格或应该发邮件；固定别名可能召回顺带提及。",
            "未基准测试规模、延迟或磁盘成本，不宣称此小样本证明性能优势。",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, help="create a new JSON snapshot; never overwrite")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when engineering Recall@5/empty-query checks fail",
    )
    args = parser.parse_args(argv)
    try:
        result = evaluate_cases(args.cases)
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            print(payload, end="")
        else:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(payload)
    except EvaluationError as exc:
        print(f"search evaluation failed: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("search evaluation failed: search_evaluation_file_unavailable", file=sys.stderr)
        return 2
    return int(args.check and bool(result["summary"]["engineering_mismatch_queries"]))


if __name__ == "__main__":
    raise SystemExit(main())
