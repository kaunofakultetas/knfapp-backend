############################################################
#  [*] Uploads — package marker for the image upload module
#
#  routes.py is the whole module: authenticated image
#  uploads (jpg/jpeg/png/gif/webp, 5 MB, magic bytes checked)
#  stored under UPLOAD_DIR as "<uuid4 hex>.<ext>" and
#  answered as a RELATIVE /api/uploads/... url, plus the
#  public file server that hands them back with a 24 h
#  max_age. Avatars, post images and chat images all come
#  through here.
#
#    POST /api/uploads            — upload one image
#                                   (require_auth, 201)
#    GET  /api/uploads/<filename> — serve one stored file
#                                   (anonymous)
#
#  uploads_bp is re-exported below, but create_app() imports
#  it from app.uploads.routes directly — nothing imports it
#  from the package at the moment. Importing the package
#  still pulls routes.py in (and through it app.auth.routes
#  and app.database), so it is not a free import.
#
#  Used by:
#    - app/__init__.py — registers the blueprint at
#      /api/uploads (via app.uploads.routes)
#    - mobile services/api/uploads.ts — uploadImageApi (POST);
#      services/api/client.ts getUploadUrl builds the GET URLs
############################################################


from app.uploads.routes import uploads_bp

# The package's public name — see the header for who actually
# imports it (nobody, today)
__all__ = ["uploads_bp"]
