"""Task handlers compose existing Radar modules with immutable course records."""
from __future__ import annotations

from career_assistant.config import resume_path

from datetime import datetime
import html
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

from career_assistant.config import CONFIG_PATH, load_config
from career_assistant.db import Database
from career_assistant.models import HK_TZ, JobPosting
from career_assistant.scheduler import Scheduler, _JOB_DEFAULTS
from .store import Store, encode, stamp


def require(store, table, record_id):
    row = store.get(table, record_id)
    if row is None:
        raise ValueError(f"记录不存在：{table} #{record_id}")
    return row


def material_markdown(data):
    if "markdown" in data:
        return data["markdown"]
    label = {"reviewed": "AI 审阅通过", "problem_draft": "问题草稿", "user_edited": "用户编辑、未经 AI 审阅"}.get(data.get("status"), "草稿")
    parts = [f"# 针对性求职材料（{label}）", "## 求职信", data.get("cover_letter", "")]
    for item in data.get("suggestions", []):
        parts += ["## 修改建议", "原文：" + item["original"], "建议：" + item["suggested"], "理由：" + item["reason"]]
    for omitted in data.get("omitted", []):
        parts += ["未生成／已跳过：" + omitted]
    for answer in data.get("answers", []):
        parts += ["## " + answer["question"], answer["answer"]]
    return "\n\n".join(parts)


def queue_search(store, preference_id, key=None):
    preference = require(store, "preferences", preference_id)
    return store.enqueue("search", {"preference_id": preference["id"]},
                         key or f"search:{preference_id}:{datetime.now(HK_TZ).isoformat()}", max_attempts=2)


def handlers(store: Store, upload_dir: Path, base_url: str, ai=None):
    from .ai import AIReviewError, AIService
    from .resume import extract_pdf
    from . import integrations

    def service():
        return ai if ai is not None else AIService()

    def reviewed(handler):
        def run(task):
            try:
                return handler(task)
            except AIReviewError as exc:
                # The AI flow has already used its bounded review/correction
                # attempts. Keep the actual objections, without replaying it.
                return {"status": "failed", "error": str(exc),
                        "review_issues": exc.issues, "review_history": exc.review_history}
        return run

    def analyze(task):
        resume = require(store, "resumes", task["payload"]["resume_id"])
        existing = store.list("reports", task_id=task["id"])
        if existing:
            return {"report_id": existing[0]["id"]}
        raw = resume_path(resume["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != resume["sha256"]:
            raise ValueError("上传原始 PDF 已改变；请重新上传，不能使用改变后的内容生成原报告")
        text = extract_pdf(raw)
        store.set_resume_text(resume["id"], text)
        cached = [v for v in store.list("resume_versions", resume_id=resume["id"]) if v["origin"] == "automatic"]
        def save_facts(facts):
            if cached:
                return
            store.confirm_facts(resume["id"], {**facts, "confirmed": False},
                                origin="automatic", extraction_task_id=task["id"])
            store.resume_waiting(resume["id"])
        engine = service()
        data = engine.analyze(text, facts=cached[0]["facts"] if cached else None, on_facts=save_facts)
        report_id = store.save_report(resume["id"], data, task_id=task["id"])
        return {"report_id": report_id}

    def search(task):
        preference = require(store, "preferences", task["payload"]["preference_id"])
        source_results = (task.get("result") or {}).get("sources", {})
        snapshots = list((task.get("result") or {}).get("snapshot_ids", []))
        for source in preference["data"].get("sources", []):
            if source_results.get(source, {}).get("status") == "succeeded":
                continue
            try:
                jobs = integrations.search_jobs({**preference["data"], "sources": [source]},
                    config_path=Path(os.environ.get("RADAR_CONFIG_PATH", str(CONFIG_PATH))), db_path=store.db_path)
                db = Database(store.db_path)
                try:
                    for job in jobs:
                        db.upsert_job(job)
                        canonical = store.get("jobs", job.job_uid)
                        if not canonical:
                            canonical = next(j for j in store.list("jobs") if j["url_normalized"] == job.url_normalized)
                        snapshots.append(store.snapshot_job(canonical["job_uid"], job.description_snippet, is_full=False, fetched=True,
                            metadata={key: getattr(job, key) for key in ("title", "company", "source", "external_id", "url")}))
                finally:
                    db.close()
                source_results[source] = {"status": "succeeded", "count": len(jobs)}
            except Exception as exc:
                from .worker import safe_error
                source_results[source] = {"status": "failed", "error": safe_error(exc)}
            with store.connect() as conn:
                conn.execute("UPDATE course_tasks SET result_json=? WHERE id=?", (encode({
                    "sources": source_results, "snapshot_ids": list(dict.fromkeys(snapshots))}), task["id"]))
        failed = source_results and all(r["status"] == "failed" for r in source_results.values())
        partial = any(r["status"] == "failed" for r in source_results.values()) and not failed
        return {"status": "failed" if failed else "succeeded", "outcome": "failed" if failed else "partial" if partial else "complete", "sources": source_results,
                "error": "全部来源失败；请查看各来源原因。" if failed else "",
                "snapshot_ids": list(dict.fromkeys(snapshots)), "count": len(set(snapshots)),
                "message": "部分成功：有来源失败，不能判定全范围无结果。" if partial else "全部来源失败。" if failed else "全部来源成功，未发现岗位。" if not snapshots else "全部来源成功，候选已保存。"}

    def match_result(version, snapshot, preference_id):
        engine = service()
        preference = store.get("preferences", preference_id) if preference_id else None
        if isinstance(engine, AIService):
            return engine.match(version["facts"], snapshot["jd"], preferences=preference["data"] if preference else None)
        return engine.match(version["facts"], snapshot["jd"])

    def match(task):
        p = task["payload"]
        existing = store.list("matches", task_id=task["id"])
        if existing:
            return {"match_id": existing[0]["id"]}
        preference = store.get("preferences", p.get("preference_id")) if p.get("preference_id") else None
        version = require(store, "resume_versions", p["resume_version_id"])
        snapshot = require(store, "job_snapshots", p["snapshot_id"])
        if not snapshot["is_full"]:
            raise ValueError("请先补充完整 JD；摘要不能用于详细匹配")
        match_id = store.save_match(version["id"], snapshot["id"], match_result(version, snapshot, preference["id"] if preference else None),
                                   preference_id=preference["id"] if preference else None, task_id=task["id"])
        return {"match_id": match_id}

    def materials(task):
        existing = store.list("materials", task_id=task["id"])
        if existing:
            item = existing[0]
            return {"material_id": item["id"], "status": "failed" if item["data"].get("status") == "problem_draft" else "succeeded",
                    "error": "材料未通过校验或审阅" if item["data"].get("status") == "problem_draft" else ""}
        match = require(store, "matches", task["payload"]["match_id"])
        version = require(store, "resume_versions", match["resume_version_id"])
        snapshot = require(store, "job_snapshots", match["snapshot_id"])
        from .worker import safe_error
        cp = task.get("checkpoint") or {}
        try:
            engine = service()
            if isinstance(engine, AIService):
                data = engine.materials(version["facts"], snapshot["jd"], match["data"],
                    checkpoint=cp, on_checkpoint=lambda value: store.checkpoint(task["id"], value))
            else:
                data = engine.materials(version["facts"], snapshot["jd"], match["data"])
            if data.get("status") == "waiting_input":
                return data
            data["status"] = "reviewed"
        except Exception as exc:
            draft = getattr(exc, "draft", None) or (store.get("tasks", task["id"]).get("checkpoint") or {}).get("draft")
            if not draft:
                raise
            issues = getattr(exc, "issues", [safe_error(exc)])
            data = {**draft, "status": "problem_draft", "review": {"approved": False, "issues": issues}}
            data["raw_draft"] = draft
            data["cover_letter"] = draft.get("cover_letter") if isinstance(draft.get("cover_letter"), str) else ""
            for group, fields in (("suggestions", ("original", "suggested", "reason")), ("answers", ("question", "answer"))):
                rows = draft.get(group, [])
                data[group] = [{**row, **{key: str(row.get(key, "")) for key in fields}} for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            material_id = store.save_material(match["id"], data, parent_id=cp.get("parent_id"), task_id=task["id"])
            return {"status": "failed", "error": safe_error(exc), "material_id": material_id,
                    "review_issues": issues}
        data["markdown"] = material_markdown(data)
        return {"material_id": store.save_material(match["id"], data, parent_id=cp.get("parent_id"), task_id=task["id"])}

    def email(task):
        p = task["payload"]
        if p["type"] == "digest":
            from .digest import settings
            body = p['html']
            if not p['job_uids']:
                return {'status': 'skipped', 'html': body, 'detail': '无新增本地候选，未发送。'}
            if not settings(store)['enabled'] and not task['idempotency_key'].startswith('retry:'):
                return {'status': 'preview', 'html': body, 'detail': '推送已关闭，未发送；可显式重试。'}
            result = integrations.send_email('INT6181 每日岗位汇总 · ' + p['day'], body)
            store.record_email_attempt(task['id'], result['status'], result.get('error') or result.get('detail', ''))
            return {**result, 'html': body}
        if p["type"] == "report":
            report = require(store, "reports", p["id"])
            body = '<h1>简历分析报告</h1><p>' + html.escape(report["data"]["summary"]) + '</p>'
            body += f'<a href="{base_url}/resumes/{report["resume_id"]}">在本机查看报告与事实</a>'
        else:
            match = require(store, "matches", p["id"])
            snapshot = require(store, "job_snapshots", match["snapshot_id"])
            job = JobPosting(snapshot["source"], snapshot["external_id"], snapshot["title"],
                             snapshot["company"], snapshot["url"], snapshot["url"])
            coverage = match["data"]["coverage"]["required"]
            job.llm_score = coverage["percent"]
            job.llm_reason = (f"必须条件：{coverage['matched']}/{coverage['total']} 条有证据，"
                              f"{coverage['unknown']} 条待确认。覆盖率不是录用概率。")
            body = integrations.render_recommendations([job], f'{base_url}/matches/{match["id"]}')
        result = integrations.send_email("INT6181 求职助手报告", body)
        store.record_email_attempt(task["id"], result["status"], result.get("error") or result.get("detail", ""))
        return {**result, "html": body}

    def prepare(task):
        from .worker import safe_error
        record = require(store, "preparations", task["payload"]["preparation_id"])
        pid = record["id"]
        try:
            if record["state"] in {"ready", "matched"}:
                return {"preparation_id": pid, "material_id": record["material_id"]}
            if not record["resume_version_id"]:
                store.update_preparation(pid, state="waiting_facts", error="请选择或上传简历，取得可用事实。")
                return {"preparation_id": pid, "status": "waiting_input"}
            version = require(store, "resume_versions", record["resume_version_id"])
            snapshot = require(store, "job_snapshots", record["snapshot_id"])
            if not snapshot["is_full"]:
                store.update_preparation(pid, state="fetching_jd", error="")
                try:
                    if not integrations.SOURCE_CAPABILITIES.get(snapshot["source"], {}).get("details"):
                        raise ValueError("此来源尚不支持完整 JD 获取，请粘贴原文。")
                    jd = integrations.fetch_full_jd(snapshot)
                except Exception as exc:
                    store.update_preparation(pid, state="waiting_jd", error=safe_error(exc))
                    return {"preparation_id": pid, "status": "waiting_input"}
                sid = store.snapshot_job(snapshot["job_uid"], jd, True, metadata=snapshot, fetched=True)
                store.update_preparation(pid, snapshot_id=sid)
                snapshot = require(store, "job_snapshots", sid)
            match_id = record["match_id"]
            if not match_id:
                store.update_preparation(pid, state="matching", error="")
                existing = [m for m in store.list("matches", resume_version_id=version["id"], snapshot_id=snapshot["id"]) if m["preference_id"] == record["preference_id"]]
                match_id = existing[0]["id"] if existing else store.save_match(version["id"], snapshot["id"],
                    match_result(version, snapshot, record["preference_id"]), preference_id=record["preference_id"], task_id=task["id"])
                store.update_preparation(pid, match_id=match_id)
            store.update_preparation(pid, state="matched", error="")
            return {"preparation_id": pid, "match_id": match_id}
        except Exception as exc:
            store.update_preparation(pid, state="failed", error=safe_error(exc))
            raise

    return {"analyze": reviewed(analyze), "search": search, "match": reviewed(match),
            "materials": reviewed(materials), "email": email, "prepare": reviewed(prepare)}


def start_schedule(store, monitoring=True):
    """Reuse Radar's trigger; scheduled and manual searches share one handler."""
    from apscheduler.schedulers.background import BackgroundScheduler
    config = load_config(Path(os.environ.get("RADAR_CONFIG_PATH", str(CONFIG_PATH))), require_runnable=False)

    def enqueue_latest():
        preferences = store.list("preferences")
        if preferences:
            latest = preferences[0]
            # Coalesce repeated trigger delivery within the same minute.
            queue_search(store, latest["id"], f'scheduled:{latest["id"]}:{datetime.now(HK_TZ):%Y%m%d%H%M}')

    scheduler = BackgroundScheduler(timezone=HK_TZ, job_defaults=_JOB_DEFAULTS)
    if monitoring:
        Scheduler(config, SimpleNamespace(run_monitoring_cycle=enqueue_latest)).setup_monitoring_job(scheduler)
    from .digest import enqueue_due
    from career_assistant.scheduler import _safe
    scheduler.add_job(_safe(lambda: enqueue_due(store), 'daily_email'), trigger='interval', seconds=30,
                      id='daily_email', next_run_time=datetime.now(HK_TZ))
    scheduler.start()
    return scheduler
