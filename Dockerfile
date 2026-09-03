# The shards, served as files, for anyone who would rather not use the
# Cloudflare endpoint — or who wants to run their own.
#
# `alpine` plus `nginx` from its repository rather than `nginx:alpine`: this
# serves static files and needs nothing else, and the difference is about 8 MB
# against 50. The base is pinned so a rebuild of the same commit is the same
# image; the data changes monthly, the base should not change under it.
#
# The build context must already contain `public/`, which `src/build.py`
# writes. It is *not* built here on purpose: doing so would put a 90 MB
# download of DB-IP's export inside every image build, and would make the
# image's contents depend on the day it was built rather than on the commit.
FROM alpine:3.24

RUN apk add --no-cache nginx

# The pid file's directory. Everything else nginx writes is redirected to
# /tmp by the config — see the note there — and both are tmpfs at run time.
RUN mkdir -p /run/nginx && chown nginx:nginx /run/nginx

COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY public/ /srv/ipgeo/

# A bad config fails the build rather than the deploy. It is not something a
# test can check — a config file is only wrong to nginx — and the failure it
# catches is total: the container never serves a byte and restart-loops.
RUN nginx -t

# Readable by nginx, writable by nobody. The whole point of this container is
# that it has nothing to write.
RUN chown -R root:root /srv/ipgeo && chmod -R a-w,a+rX /srv/ipgeo

USER nginx

# busybox's wget, so there is no curl and no dependency chain behind it.
HEALTHCHECK --interval=30s --timeout=3s --start-period=3s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/v1/index.json || exit 1

EXPOSE 8080

# `-e` before anything else: nginx opens its compiled-in error log *before*
# it reads the config, so `error_log` in the file cannot stop it complaining
# about a path it cannot write. This can.
CMD ["nginx", "-e", "/dev/stderr", "-g", "daemon off;"]
