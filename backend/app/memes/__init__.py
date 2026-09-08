# -----------------------------------------------------------
#  [*] Memes package — the shared meme library
#
#  Re-exports the blueprint so create_app registers it the same
#  way every other feature package does.
#
#  Used by:
#    - app/__init__.py — create_app
# -----------------------------------------------------------

from app.memes.routes import memes_bp

__all__ = ["memes_bp"]
