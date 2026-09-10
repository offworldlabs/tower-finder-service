# The edge's config travels in the image rather than a bind mount, so a
# template change alters the image and `up -d --build` recreates the container.
# A mounted file's contents are invisible to compose, and nginx renders this
# once, at start.
FROM nginx:1.31-alpine
COPY edge.conf.template /etc/nginx/templates/default.conf.template
