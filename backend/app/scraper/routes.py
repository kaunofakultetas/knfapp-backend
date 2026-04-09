"""Scraper status and manual trigger routes."""

from flask import Blueprint, jsonify

from app.auth.routes import require_role
from app.database import get_db
from app.scraper.knf_scraper import scrape_knf_news
from app.scraper.vu_scraper import scrape_vu_news

scraper_bp = Blueprint("scraper", __name__)


@scraper_bp.route("/status", methods=["GET"])
def scraper_status():
    """Get recent scraper run history."""
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
    """Manually trigger a scrape (admin only). Aliased as /run and /trigger."""
    knf_result = scrape_knf_news(pages=3)
    vu_result = scrape_vu_news(pages=1)
    return jsonify({
        "knf": knf_result,
        "vu": vu_result,
    })
