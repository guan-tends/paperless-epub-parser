# Thin derived image: paperless-ngx + the EPUB parser plugin.
#
# A derived image is the *only* durable install path.  The container's
# site-packages is root-owned and the image is ephemeral, so a `pip install`
# run inside a live container appears to succeed and then evaporates on the
# next `docker compose up`.  The Dockerfile is the persistence layer.
#
# The base tag is pinned deliberately.  `latest` is a moving tag -- rebuilding
# against it months from now would silently install the plugin into a
# different paperless than the one the archive's metadata was built against.

FROM ghcr.io/paperless-ngx/paperless-ngx:3.1.3

# Installing the plugin needs root, and we stay root deliberately.
#
# The base image leaves `USER` unset, so its s6-overlay entrypoint (/init)
# starts as root and then drops privileges to the paperless account itself,
# honouring USERMAP_UID/USERMAP_GID.  We must let that handoff happen.
#
# `USER root` here is equivalent to leaving it unset (both mean uid 0) and is
# stated explicitly so the intent is visible.  The line that breaks this image
# is a NON-root switch -- e.g. a trailing `USER paperless`.  That pre-empts the
# handoff, and the container then exits cleanly (status 0), prints no error,
# and crash-loops with a rising restart count.
USER root

# Copy only the package metadata and source -- the build context excludes
# tests, the local virtualenv and VCS data (see .dockerignore).
COPY pyproject.toml README.md LICENSE /tmp/paperless-epub-parser/
COPY src /tmp/paperless-epub-parser/src

# `--no-cache-dir` keeps the layer small; the plugin pulls in markitdown
# (EPUB conversion) and Pillow (thumbnails), both of which are needed at
# runtime.  Installing from a local path is preferred over a registry
# round-trip so the image is reproducible from this repo alone.
RUN pip install --no-cache-dir /tmp/paperless-epub-parser \
    && rm -rf /tmp/paperless-epub-parser
