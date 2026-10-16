"""Freeze an existing five-question replay for human review; no application imports.

Read only the explicitly supplied static knowledge snapshot and its public notice
tables. Never read credentials, run a query/model, fetch a page, or write a database.
Full input copies, notice bodies and answers stay in a new private directory.
"""

import argparse
import hashlib
import json
import os
import platform
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


SELECTED = {
    "uc": (*range(1, 10), 15, 27, 114, 130, 422),
    "cs": (1, 2, 5, 6, 8, 11, 14, 18, 24, 31, 41),
}
CASE_IDS = ("research", "cs-ta", "applicant", "ambiguous-deadline", "no-match")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = data.encode() if isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def write_json(path, value):
    write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare(source, output, manifest):
    source = source.resolve(strict=True)
    output = output.absolute()
    root = Path(__file__).resolve().parents[3]
    if output.is_relative_to(root) or output.exists():
        raise ValueError("output must be a new private directory outside the repository")
    if manifest.exists():
        raise ValueError("public manifest already exists")
    report_path = source / "model-five-final.json"
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    if tuple(r["case_id"] for r in report["results"]) != CASE_IDS:
        raise ValueError("unexpected five-question replay")
    if report["query_version"] != "campus-query-v3":
        raise ValueError("wrong query version")
    inputs = {}
    knowledge = source / "knowledge"
    for role, expected in report["input_sha256"].items():
        if role == "query_config":
            matches = [p for p in knowledge.glob("ask*.toml") if digest(p.read_bytes()) == expected]
            if len(matches) != 1:
                raise ValueError("query configuration no longer matches")
            original = matches[0]
        elif role == "profile":
            original = knowledge / "profile.toml"
        elif role == "snapshot_manifest":
            original = knowledge / "prepared.json"
        else:
            kind, instance = role.split(":")
            name = {"database": "signalnest.sqlite3", "instance_config": "instance.toml",
                    "snapshot_manifest": "snapshot.json"}[kind]
            original = knowledge / instance / name
        if original.is_symlink() or digest(original.read_bytes()) != expected:
            raise ValueError("snapshot input changed: " + role)
        if role.startswith("database:"):
            wal = original.with_name(original.name + "-wal")
            if wal.exists() and wal.stat().st_size:
                raise ValueError("snapshot has a nonempty WAL")
        inputs[role] = original
    output.mkdir(mode=0o700, parents=False)
    write(output / "model-five-final.json", report_bytes)
    for role, original in inputs.items():
        write(output / "knowledge" / original.relative_to(knowledge), original.read_bytes())
        if role.startswith("database:"):
            (output / "knowledge" / original.relative_to(knowledge)).chmod(0o400)

    corpus = []
    status = []
    for instance in SELECTED:
        database = inputs["database:" + instance]
        con = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute("""SELECT d.id document_id, d.source_id, d.source_document_id,
                d.discovered_title, d.status, d.last_error_code,
                v.id version_id, v.title, v.published_date, v.content_sha256,
                v.parser_version, v.parsed_at, v.normalized_content,
                r.id response_id, r.requested_url, r.final_url, r.fetched_at,
                r.status_code, r.body_sha256, r.body_path
                FROM documents d LEFT JOIN notice_versions v ON v.id=d.current_version_id
                LEFT JOIN raw_responses r ON r.id=v.raw_response_id ORDER BY d.id""").fetchall()
        finally:
            con.close()
        for row in rows:
            item = dict(row)
            normalized = item.pop("normalized_content")
            item.update(instance_id=instance, corpus_id=f"{instance}:{row['document_id']}",
                        content=json.loads(normalized) if normalized else None)
            corpus.append(item)
        status.append(json.loads(inputs["snapshot_manifest:" + instance].read_text()))
    by_id = {item["corpus_id"]: item for item in corpus}
    packet = [by_id[f"{iid}:{doc}"] for iid, ids in SELECTED.items() for doc in ids]
    public_packet = []
    for item in packet:
        original = source / "source-backups" / item["instance_id"] / item["body_path"]
        body = original.read_bytes()
        if digest(body) != item["body_sha256"]:
            raise ValueError("archived original body does not match")
        filename = item["corpus_id"].replace(":", "-") + ".html"
        write(output / "original-pages" / filename, body)
        public_packet.append({k: item[k] for k in (
            "corpus_id", "source_id", "source_document_id", "version_id", "title",
            "published_date", "content_sha256", "parser_version", "parsed_at", "response_id",
            "final_url", "fetched_at", "body_sha256")})
    write_json(output / "corpus.json", corpus)
    write_json(output / "packet.json", packet)

    catalog = ["# 全部冻结知识库目录", "", "645个身份；有成功正文者644个。缺原文不是负例。", "",
               "ID | 发布日期 | 标题 | 正文", "--- | --- | --- | ---"]
    for item in corpus:
        catalog.append(f"{item['corpus_id']} | {item['published_date'] or '未知'} | "
                       f"{(item['title'] or item['discovered_title']).replace('|', '／')} | "
                       f"{'有' if item['content'] else '缺失'}")
    write(output / "catalog.md", "\n".join(catalog) + "\n")
    cards = ["# 25份先读原文", "", "先标相关性，随后再打开 answers.md。选材是工程取样，不是人工相关集。",
             "正文来自冻结数据库；HTML原文在original-pages，版本及摘要见packet.json。", ""]
    for item in packet:
        cards += [f"## {item['corpus_id']} {item['title']}", "",
                  f"发布 {item['published_date']}；版本 {item['version_id']}；[原始HTML](original-pages/{item['corpus_id'].replace(':', '-')}.html)",
                  "", item["content"]["body_text"], ""]
    write(output / "originals.md", "\n".join(cards))

    scenarios = []
    answers = ["# 五题实际结构化回答", "", "忠实展示冻结JSON字段，不用已变化的当前renderer重绘旧结果。",
               "原终端逐字文本未取得；UI措辞不在本次可验证范围。候选及轨迹见model-five-final.json。", ""]
    for row in report["results"]:
        actual = row["report"]
        for notice in actual["notices"]:
            original = by_id[f"{notice['instance_id']}:{notice['document_id']}"]
            if any(notice[key] != original[key] for key in ("version_id", "content_sha256", "parser_version")):
                raise ValueError("answer notice version does not match database")
            for kind in ("relevance", "conditions", "deadline"):
                for quote in notice[kind]:
                    if quote["text"] not in original["content"][quote["field"]]:
                        raise ValueError("answer quote not present in frozen original")
        case_input = {"case_id": row["case_id"], "question": actual["question"],
                      "at": actual["at"], "publication_window": actual["publication_window"],
                      "corpus_sha256": digest(canonical(corpus)),
                      "packet_ids": [x["corpus_id"] for x in packet],
                      "original_replay_sha256": digest(report_bytes)}
        scenarios.append({**case_input, "input_sha256": digest(canonical(case_input)),
                          "review_status": "pending", "relevant_ids": None,
                          "relevance_scope": None, "must_answer_ids": None,
                          "retrieval_assessment": None, "answer_assessment": None,
                          "missed_ids": None, "evidence_errors": None,
                          "reviewer": None, "reviewed_at": None, "notes": None})
        answers += [f"## {row['case_id']}", "", actual["question"], "",
                    "```json", json.dumps({k: actual[k] for k in (
                        "status", "publication_window", "notices", "warnings", "error_code")},
                        ensure_ascii=False, indent=2), "```", ""]
    write_json(output / "review-template.json", scenarios)
    write(output / "answers.md", "\n".join(answers))
    write(output / "README.md", """# 五题人工核对入口

固定时钟2026-10-15（Asia/Shanghai）；实际模型标识保留在本机原始报告中。
先读originals.md，按各问题独立填写相关ID和must_answer_ids，再打开answers.md核对证据。
review-template.json全部pending/null。另存review-<实际时间>.json，不能覆盖模板或伪填真人。
第一轴：相关／不相关／无法判断；分别记录候选漏检和已检出但未入回答。
第二轴：supported／unsupported／incomplete／not_answered，并写主体、条件、期限、未知边界的原文依据。
范围可填packet25或whole_successful_corpus644；有未判条目时只报partial，不算整库召回率。
originals.md含25份初始材料；catalog.md及corpus.json可查全部645身份、644份成功正文。
研究材料保留真实通知及画像副本；仅在本机使用，不提交仓库。未复制或读取任何credential文件。
此包是既有运行的证据整理，不运行模型、采集、生产服务或发送邮件。
""")
    observed = {name: digest((root / name).read_bytes()) for name in report["source_sha256"]}
    public = {
        "format": "signalnest-five-question-research-materials-v1",
        "prepared_at_utc": datetime.now(UTC).isoformat(),
        "private_packet": str(output), "original_replay_sha256": digest(report_bytes),
        "query_version": report["query_version"], "at": report["at"],
        "model_config": report["model_config"], "query_limits": report["query_limits_per_case"],
        "reported_run_summary": report["summary"], "input_sha256": report["input_sha256"],
        "recorded_run_source_sha256": report["source_sha256"],
        "current_checkout_source_sha256": observed,
        "changed_since_run": [name for name in observed if observed[name] != report["source_sha256"][name]],
        "run_source_bytes_archived_by_this_task": False,
        "renderer_terminal_text_available": False,
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "platform": platform.platform()},
        "corpus_count": len(corpus), "successful_content_count": sum(bool(x["content"]) for x in corpus),
        "initial_packet_count": len(packet), "packet_selection": "engineering_evidence_pool_not_human_gold",
        "corpus_sha256": digest(canonical(corpus)), "packet": public_packet,
        "scenarios": [{k: s[k] for k in ("case_id", "question", "at", "publication_window", "input_sha256")} for s in scenarios],
        "source_snapshots": status, "new_model_requests": 0, "new_campus_requests": 0,
        "production_database_access": False, "human_confirmed": 0, "human_pending": 5,
        "input_files_unchanged": True,
    }
    for role, original in inputs.items():
        if digest(original.read_bytes()) != report["input_sha256"][role]:
            raise ValueError("input changed during export")
    if report_path.read_bytes() != report_bytes:
        raise ValueError("replay changed during export")
    public["private_output_sha256"] = {str(f.relative_to(output)): digest(f.read_bytes())
                                       for f in sorted(output.rglob("*")) if f.is_file()}
    write_json(manifest, public)
    print(json.dumps({"packet": str(output), "cases": 5, "packet_documents": len(packet),
                      "corpus_documents": len(corpus), "new_model_requests": 0,
                      "human_confirmed": 0}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.output, args.manifest)
