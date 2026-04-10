"""Shared API utilities."""

from flask import jsonify, request


def parse_pagination(max_per_page=50, default_per_page=20):
    """Parse and validate pagination query params.

    Returns (page, per_page, error_response) where error_response is
    a (response, status_code) tuple if validation failed, or None on success.

    Usage::

        page, per_page, err = parse_pagination()
        if err:
            return err
    """
    raw_page = request.args.get("page")
    raw_per_page = request.args.get("per_page")

    # Validate page
    if raw_page is not None:
        try:
            page = int(raw_page)
        except (ValueError, TypeError):
            return None, None, (jsonify({"error": "page must be a positive integer"}), 400)
        if page < 1:
            return None, None, (jsonify({"error": "page must be a positive integer"}), 400)
    else:
        page = 1

    # Validate per_page
    if raw_per_page is not None:
        try:
            per_page = int(raw_per_page)
        except (ValueError, TypeError):
            return None, None, (jsonify({"error": "per_page must be a positive integer"}), 400)
        if per_page < 1:
            return None, None, (jsonify({"error": "per_page must be a positive integer"}), 400)
        per_page = min(per_page, max_per_page)
    else:
        per_page = default_per_page

    return page, per_page, None
