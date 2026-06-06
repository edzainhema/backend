# Deploy checklist

Single source of truth for "things you must do at deploy time that the code
alone can't enforce." Organized by the audit finding that introduced each
item. Inline comments in `settings.py` / `services/throttles.py` cover the
*why*; this file is the *do-this*.

Update as new findings land.

---

## Environment variables

Already documented inline in `backend/backend/settings.py`, but consolidated
here for the deploy-time checklist:

| Variable                  | Required when            | Notes                                                                                  |
| ------------------------- | ------------------------ | -------------------------------------------------------------------------------------- |
| `DJANGO_DEBUG`            | always in prod (=`False`)| Defaults to `True` for dev. Production guards in `settings.py` refuse to boot with the scaffolding secret / `ALLOWED_HOSTS=*` / `localhost` SITE_URL when DEBUG is off. |
| `DJANGO_SECRET_KEY`       | DEBUG=False              | Long random string (50+ chars).                                                        |
| `DJANGO_ALLOWED_HOSTS`    | DEBUG=False              | Comma-separated hostnames.                                                             |
| `SITE_URL`                | DEBUG=False              | Public origin, no trailing slash. Used for absolute URLs in WebSocket payloads, password-reset email links, etc. |
| `DATABASE_URL`            | DEBUG=False              | `postgres://user:pass@host:5432/db`. Or set `POSTGRES_*` discrete vars. SQLite fallback is refused under DEBUG=False. |
| `REDIS_URL`               | DEBUG=False              | `redis://host:6379/0`. Required for the channel layer (multi-worker WS fan-out) and for the cache (cross-worker throttle counters — see B4). |
| `REDIS_CHANNEL_URL`       | optional                 | Dedicated Redis DB index for channels-redis (keeps pub/sub traffic off the cache keyspace). Falls back to `REDIS_URL`. |
| `REDIS_CACHE_URL`         | optional                 | Same idea for the cache. Falls back to `REDIS_URL`.                                    |
| `CELERY_BROKER_URL`       | optional                 | Falls back to `REDIS_URL`. Without it Celery runs EAGER (inline), which is fine but loses the off-request-thread benefit. |
| `GOOGLE_PLACES_API_KEY`   | optional                 | Enables `/pages/location/*` autocomplete proxy. Without it the proxy returns 503 and the modal falls back to plain free-text entry. See `SETUP_GOOGLE_PLACES.md`. |
| `AWS_STORAGE_BUCKET_NAME` | prod media uploads       | Activates S3 storage path. Requires `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optionally `AWS_S3_REGION_NAME`, `AWS_S3_CUSTOM_DOMAIN`. |
| `AWS_SES_REGION_NAME`     | prod email               | Defaults to `us-east-1`. Used by `django_ses.SESBackend` for password-reset email and any future transactional mail. |
| `DEFAULT_FROM_EMAIL`      | prod email               | Defaults to `Here Social <noreply@here-social.com>`.                                   |
| `SENTRY_DSN`              | optional in prod         | Only initialised when set AND DEBUG=False. Errors silently drop if unset.              |
| `DB_SSLMODE`              | managed Postgres         | `require` or `verify-full` for RDS / Cloud SQL / etc.                                  |

---

## At every deploy

### 1. Apply migrations
```
python manage.py migrate
```

This is the catch-all. The notable per-finding details:

- **B2 (token blacklist)** — `rest_framework_simplejwt.token_blacklist` is in
  `INSTALLED_APPS`. Its migrations ship with the package (no app-level
  migration in `api/migrations/`); `migrate` creates `token_blacklist_*`
  tables on first deploy. Without these tables, `/auth/logout/` and the
  rotation-blacklist on `/auth/token/refresh/` will both 500.

### 2. Collect static
```
python manage.py collectstatic --noinput
```

Required for the Django admin's CSS. When `AWS_STORAGE_BUCKET_NAME` is set,
this also uploads to S3 under `static/`.

### 3. Restart workers
- gunicorn (HTTP) — graceful restart picks up new code.
- daphne (ASGI / WebSocket) — graceful restart.
- celery worker (`celery -A backend worker`) — restart to pick up new
  `api/tasks.py`.

---

## Per-finding deploy notes

### B1 — frontend `IS_DEV` hardcoded (not yet implemented)
Status: NOT FIXED. `frontend/src/api/config.ts` still has `const IS_DEV = true`
hardcoded; a production release build will hit the LAN-IP dev backend.
**One-line fix** (`const IS_DEV = __DEV__;`) must land before any signed build.

### B2 — logout + refresh-token blacklist
**Deploy step (one-time):** the simplejwt blacklist app's migrations create
the `token_blacklist_outstandingtoken` and `token_blacklist_blacklistedtoken`
tables. `python manage.py migrate` handles it.

**Runtime check after deploy:**
```
curl -X POST https://<host>/auth/logout/ -H 'Content-Type: application/json' \
  -d '{"refresh":""}'
# expect: 200 {"status":"ok"}
```

### B3 — password reset flow
**Deploy steps:**
1. Ensure `DEFAULT_FROM_EMAIL` is set (defaults to `noreply@here-social.com`).
2. Ensure the SES sender domain is **verified** in the AWS SES console for the
   region named by `AWS_SES_REGION_NAME`. Unverified senders silently fail-mail.
3. If you want graphic templates / domain authentication (DKIM/SPF), do that
   in SES too — the password-reset email goes through the same backend.

**Deep linking (recommended, optional):** the email body contains both a
clickable link (`<SITE_URL>/reset-password?uid=...&token=...`) and a pasteable
`Code: <uid>.<token>` line. Without deep linking, the link opens in a web
browser and the user has to copy the code into the in-app **ResetPassword**
screen. With deep linking, the link opens the app directly on that screen
with `uid` and `token` already populated. See
`frontend/SETUP_PASSWORD_RESET_DEEP_LINK.md` for the iOS / Android config.

**Runtime check after deploy:**
```
curl -X POST https://<host>/auth/password-reset-request/ \
  -H 'Content-Type: application/json' \
  -d '{"identifier":"a-real-user@example.com"}'
# expect: 200 generic body; inbox should receive a reset email within ~1 min
```

### B4 — rate limiting
**Deploy step #1 (CRITICAL):** ensure nginx forwards the real client IP.
DRF's `AnonRateThrottle` keys on `request.META['REMOTE_ADDR']`; behind
nginx without this config, every anonymous request looks like it's
coming from the nginx host IP, and the per-IP buckets collapse into
one global bucket per scope. That means a single attacker exhausts
the bucket for every other anonymous user, and the throttle becomes
a self-DoS.

The DJANGO half is already in code -- `api.middleware.RealClientIPMiddleware`
ships in `MIDDLEWARE` and promotes `X-Forwarded-For`'s first entry to
`REMOTE_ADDR` so DRF sees the real client. The NGINX half (this deploy
step) is forwarding the header in the first place. Add to your site's
`location` or `server` block:

```nginx
proxy_set_header X-Real-IP        $remote_addr;
proxy_set_header X-Forwarded-For  $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;   # already in place for HTTPS
```

⚠️ Trust model: `RealClientIPMiddleware` trusts XFF unconditionally,
which is correct ONLY because nginx is the public-facing layer that
rewrites the header before reaching Django. If Django is ever exposed
without a trusted proxy in front, a malicious client can spoof the
header. See the middleware docstring for the rewrite to restrict
trust to a list of known proxy IPs.

**Deploy step #2:** ensure `REDIS_URL` is set. Throttle counters live in
`caches['default']`; with the LocMemCache dev fallback, counters are
per-process and the throttle is per-process too — meaning N workers each
honour their own copy of the rate limit, so the effective limit is N× the
configured one. Redis makes counters shared.

**Deploy step #3 (optional, defense in depth):** add nginx `limit_req` for
`/auth/*`. The DRF throttle is the primary defense; nginx `limit_req` catches
floods that would otherwise hit Django at all (cheaper to reject at the
edge):

```nginx
limit_req_zone $binary_remote_addr zone=auth:10m rate=30r/m;

location /auth/ {
    limit_req zone=auth burst=20 nodelay;
    proxy_pass http://gunicorn;
    # ... existing proxy_set_header lines ...
}
```

The DRF per-scope rates are tighter than this — the nginx limit only fires
on egregious abuse.

**Tuning rates without code changes:** every per-scope rate is in
`REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']` in `settings.py`. Edit the dict
and restart workers; no view code touches the numbers.

**Runtime check after deploy:**
```
# Trip the login throttle to confirm it's wired:
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<host>/auth/login/ \
    -H 'Content-Type: application/json' \
    -d '{"identifier":"throttle-test","password":"wrong"}'
done
# expect: ten 400s (invalid credentials) then 429s for the rest
```

### B5 — upload validation (DM / page-chat / posts / comments)
**No deploy step required.** The fix is purely code-level: the new
`validate_uploaded_media_file` in `api/services/media/validation.py` is
called from every upload-accepting view (post create, comment create, DM
send, page-chat send) and runs the same size cap + magic-byte sniff +
decompression-bomb guard on all of them. The DM and page-chat paths
previously only checked the client's Content-Type header.

Default per-kind caps (tunable inline in `validation.py` or per-callsite
via `max_bytes_by_kind`):
- `IMAGE_MAX_BYTES = 30 MB`
- `VIDEO_MAX_BYTES = 250 MB`
- `AUDIO_MAX_BYTES = 50 MB` (new — for voice notes)

**Runtime check after deploy:** the fastest smoke test is to attach a
tiny `.txt` file to a DM in the app — the send should fail with
"Unsupported file type: text/plain". Pre-fix, that file would have been
written to `media/message_media/`.

For an automated check:
```
# Posting random bytes labelled image/png to a DM should now 400.
curl -X POST https://<host>/auth/send-message/ \
  -H "Authorization: Bearer <valid access token>" \
  -F "conversation_id=<id>" \
  -F "media=@<random.bin>;type=image/png"
# expect: 400 with body mentioning "valid image" / "valid video"
```

**Note on the legacy `_detect_media_type` helper** (in
`views/page_chat.py`): kept for the reply-preview path that only needs a
"what kind of attachment is this" label and doesn't run the safety
pipeline. The actual send-message flow no longer uses it — every byte on
the way in goes through `validate_uploaded_media_file`.

---

## Verifying a deploy

After every deploy, run this smoke checklist:

```
# 1. Health
curl -fsS https://<host>/health/

# 2. Auth alive
curl -X POST https://<host>/auth/login/ -H 'Content-Type: application/json' \
  -d '{"identifier":"<a real user>","password":"<their password>"}' | jq .access

# 3. Logout reaches the blacklist (B2)
curl -X POST https://<host>/auth/logout/ -H 'Content-Type: application/json' \
  -d '{"refresh":""}'
# expect: 200 {"status":"ok"}

# 4. Throttle (B4) — see runtime check in the B4 section above.

# 5. WebSocket
# Open the app, send a DM, watch it deliver across two devices with no reopen.
```
