# smartfritz

Light schedules and grow history for plugs on a FRITZ!Box (FRITZ!OS 8.20 or newer).
Standard library Python, one container, a web page for phone and desktop.

Every round works out what each plug should be doing and corrects it, so a restart
or a missed switch fixes itself. A cycle counts elapsed time from its first
lights-on: any length works, in minutes, including always on.

A plug without a running grow is left alone. Turn off the FRITZ!Box's own timers on
the plugs this app controls, or the two will fight.

## Tests

```bash
python3 -m unittest discover -s tests
```

```bash
python3 tests/devserver.py
```

The second runs the page at http://127.0.0.1:8099 against a fake box (password `dev-password`).

## Settings

| Variable | |
|---|---|
| `SMARTFRITZ_FRITZ_URL` | The box's address. Not `fritz.box`, which can be a public name. |
| `SMARTFRITZ_FRITZ_USER`, `SMARTFRITZ_FRITZ_PASSWORD` | A FRITZ!Box user with the Smart Home right |
| `SMARTFRITZ_APP_PASSWORD` | The web page's password |
| `SMARTFRITZ_FRITZ_CERT_SHA256` | Optional. Without it the box's certificate is remembered on first contact |
| `SMARTFRITZ_FRITZ_CERT_AUTO` | Optional. `1` trusts a changed certificate by itself and notes it in the log |
| `SMARTFRITZ_TICK`, `SMARTFRITZ_PORT`, `SMARTFRITZ_BIND`, `SMARTFRITZ_DB`, `SMARTFRITZ_COOKIE_SECURE` | Defaults: 30, 8080, 0.0.0.0, /data/smartfritz.db, 1 |

If the box gets a new certificate (it makes one when it restarts after an update) the
app refuses to log in and says so. Set the new fingerprint in `SMARTFRITZ_FRITZ_CERT_SHA256`.

With `SMARTFRITZ_FRITZ_CERT_AUTO=1` the app trusts the new certificate instead, so the
lights keep their schedule. The price: someone on the network who answers as the box
gets a Smart Home session. Leave `SMARTFRITZ_FRITZ_CERT_SHA256` unset with it.

## Deploying on m1 (compose stack)

`deploy/m1/` holds the service, the nginx blocks and the settings template.

1. Copy `Containerfile`, `.containerignore` and `src/` to `/home/user/docker/knsmartfritz/app/`, and create `/home/user/docker/knsmartfritz/data/`, owned by uid 1000.
2. Fill in `knsmartfritz/smartfritz.env` from the template and `chmod 600` it.
3. Add `compose-service.yml` to the compose file and run `docker compose -f docker-compose-kiwi-node.yml up -d --build knsmartfritz`.
4. Add a Pi-hole record for `grow.m1.kiwi`, pointing to `192.168.2.20`.
5. Only now add `nginx-blocks.conf` to `knnginx/nginx.conf`, check it with `docker exec knnginx nginx -t`, and reload with `docker exec knnginx nginx -s reload`. nginx refuses to start when an upstream's container does not exist. The file is owned by root and bind-mounted on its own, so write it in place with `sudo` (`cat new > nginx.conf`); replacing the file would leave the container on the old one.

## Image

`ghcr.io/derlocke-ng/smartfritz`, for amd64 and arm64: `latest` is main, `X.Y.Z` a release
(a tag `vX.Y.Z`, the version in `pyproject.toml`). It runs as uid 1000 and keeps its
database in `/data`.

```bash
podman run -d --name smartfritz --read-only --cap-drop ALL -p 8080:8080 -v smartfritz_data:/data \
  --env-file smartfritz.env -e SMARTFRITZ_COOKIE_SECURE=0 ghcr.io/derlocke-ng/smartfritz:latest
```

`SMARTFRITZ_COOKIE_SECURE=0` only for plain http, as here; behind a reverse proxy with TLS leave it out.

## As a kiwi module

The module for [kiwi-server](https://github.com/derlocke-ng/kiwi-server) is in
[kiwi-modules-personal](https://github.com/derlocke-ng/kiwi-modules-personal/tree/main/modules/smartfritz):

```bash
kiwi-server sources ~/kiwi/fleet.yaml add https://github.com/derlocke-ng/kiwi-modules-personal
```
