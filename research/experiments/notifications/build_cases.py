"""Prepare frozen research cases, proposed labels pending HUMAN confirmation."""
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEST = Path(__file__).resolve().parent
BASE = dict(institution="whu", role="student", study_level="undergraduate", entry_year=None,
            interest_topics=["research", "minor", "teaching_assistant"],
            high_value_topics=["research", "minor", "teaching_assistant"], store_only_topics=["course_enrollment"])


def captured(article_id):
    metas = sorted((ROOT / "research/fixtures/notifications").glob(f"notice-{article_id}-*.json"))
    for p in reversed(metas):
        m = json.loads(p.read_text())
        if m.get("status_code") == 200:
            return str((p.parent / m["fixture"]).relative_to(ROOT)), m["final_url"], str(p.relative_to(ROOT))
    raise ValueError(f"missing capture {article_id}")


def main():
    prior = DEST / "cases.json"
    if prior.exists() and any(case.get("annotation_status") != "pending_human" for case in json.loads(prior.read_text())["cases"]):
        raise ValueError("refuse_overwriting_human_annotations")
    cases = []
    def add(key, article, at, proposal, description, changes=None, old=False):
        if old:
            path, url, metadata = article
        else:
            path, url, metadata = captured(article)
        profile = {**BASE, **(changes or {})}
        cases.append(dict(case_id=key, fixture=path, source_url=url, metadata=metadata,
                          sha256=sha256((ROOT / path).read_bytes()).hexdigest(),
                          evaluated_at=at, context="new", mode="hybrid", profile=profile,
                          description=description, proposed_action=proposal, human_expected_action=None,
                          annotation_status="pending_human"))
    current = ("research/fixtures/current-notice-detail.html", "https://uc.whu.edu.cn/info/1517/128231.htm", "research/fixtures/current-notice-detail.headers")
    legacy = ("research/fixtures/legacy-notice-detail.html", "https://uc.whu.edu.cn/2022/show.jsp?urltype=news.NewsContentUrl&wbtreeid=1517&wbnewsid=127581", "research/fixtures/legacy-notice-detail.headers")
    add("S01", current, "2026-09-28T09:00:00+08:00", "STORE_ONLY", "2026级选课对2025级档案；菜单教学科研不能算研究机会", {"entry_year": 2025}, True)
    add("S02", current, "2026-09-28T09:00:00+08:00", "STORE_ONLY", "选课归保存主题；入学年份未知", old=True)
    add("S03", legacy, "2026-09-04T09:00:00+08:00", "STORE_ONLY", "时间表图片与附件细项，当前Profile只保存选课", old=True)
    add("S04", "128291", "2026-09-28T09:00:00+08:00", "PUSH_NOW", "真实科研选课截止当日23:59；助教字样不是招聘")
    add("S05", "128291", "2026-09-28T09:00:00+08:00", "STORE_ONLY", "明确本科对象，对虚构研究生Profile不符合", {"study_level": "graduate"})
    add("S06", "17361", "2024-02-24T09:00:00+08:00", "PUSH_NOW", "补报科研机会，团队条件未知，2月25日前团队提交临近")
    add("S07", "14147", "2022-05-14T09:00:00+08:00", "STORE_ONLY", "当前选题征集主体教师，不是学生现在申请")
    add("S08", "117011", "2024-12-09T09:00:00+08:00", "PUSH_NOW", "辅修真实当日24:00截止，GPA/专业/年级例外及附件条件未知")
    add("S09", "127511", "2026-06-26T09:00:00+08:00", "STORE_ONLY", "科研结题结果不是新申请阶段")
    add("S10", "18135", "2024-06-28T09:00:00+08:00", None, "当前Parser失败；违规取消参赛资格不是整个机会取消")
    add("S11", "128291", "2026-09-10T09:00:00+08:00", "DIGEST", "同一真实科研机会，非高价值Profile，非临近截止", {"high_value_topics": []})
    for case in cases:
        datetime.fromisoformat(case["evaluated_at"])
    (DEST / "cases.json").write_text(json.dumps(dict(note="Historical clock replay; proposals are NOT human gold labels.", cases=cases), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
