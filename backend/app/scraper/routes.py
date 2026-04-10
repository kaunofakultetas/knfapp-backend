"""Scraper status and manual trigger routes."""

from flask import Blueprint, jsonify

from app.auth.routes import require_role
from app.database import get_db
from app.scraper.knf_scraper import scrape_knf_news
from app.scraper.schedule_scraper import scrape_knf_schedule
from app.scraper.info_scraper import scrape_faculty_info
from app.scraper.vu_scraper import scrape_vu_news

scraper_bp = Blueprint("scraper", __name__)


@scraper_bp.route("/status", methods=["GET"])
@require_role("admin")
def scraper_status():
    """Get recent scraper run history (admin only)."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM scraper_runs ORDER BY started_at DESC LIMIT 20"
        ).fetchall()
        runs = [
            {
                "id": r["id"],
                "source": r["source"],
                "status": r["status"],
                "articlesFound": r["articles_found"],
                "articlesNew": r["articles_new"],
                "error": r["error_message"],
                "startedAt": r["started_at"],
                "finishedAt": r["finished_at"],
            }
            for r in rows
        ]
        return jsonify({"runs": runs})
    finally:
        db.close()


@scraper_bp.route("/trigger", methods=["POST"])
@scraper_bp.route("/run", methods=["POST"])
@require_role("admin")
def trigger_scrape():
    """Manually trigger a news scrape (admin only). Aliased as /run and /trigger."""
    knf_result = scrape_knf_news(pages=3)
    vu_result = scrape_vu_news(pages=1)
    return jsonify({
        "knf": knf_result,
        "vu": vu_result,
    })


@scraper_bp.route("/schedule", methods=["POST"])
@require_role("admin")
def trigger_schedule_scrape():
    """Manually trigger a schedule scrape from tvarkarasciai.vu.lt (admin only)."""
    result = scrape_knf_schedule()
    return jsonify(result)


@scraper_bp.route("/info", methods=["POST"])
@require_role("admin")
def trigger_info_scrape():
    """Manually trigger a faculty info scrape from knf.vu.lt (admin only)."""
    result = scrape_faculty_info()
    return jsonify(result)
