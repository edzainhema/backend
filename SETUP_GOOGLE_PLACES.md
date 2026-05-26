# Google Places setup (location autocomplete)

The location modal lets page owners search for real addresses as they type.
The app **never** calls Google directly — it goes through three backend proxy
endpoints so the API key stays on the server:

- `GET  /pages/location/autocomplete/?input=...&session_token=...`
- `GET  /pages/location/details/?place_id=...&session_token=...`
- `POST /pages/location/set/` (saves `location` + `latitude`/`longitude`/`place_id`)

The proxy reads its key from `settings.GOOGLE_PLACES_API_KEY`, which is loaded
from the `GOOGLE_PLACES_API_KEY` environment variable. **If the variable is
unset, the proxy returns 503 and the modal silently falls back to plain
free-text entry** — so nothing breaks before you add a key.

## 1. Get an API key

1. Open the [Google Cloud Console](https://console.cloud.google.com/). You can
   reuse the existing Firebase project (`here-d43c4`) or make a new one.
2. Enable billing on the project. Places API requires a billing account, though
   Google includes a recurring monthly credit that covers light usage.
3. Go to **APIs & Services → Library**, search "places api", and enable
   **Places API (New)** — the first result, *not* the plain "Places API"
   (that legacy product was frozen in March 2025 and can no longer be enabled
   by new projects). The proxy calls the New API's `places:autocomplete` and
   `v1/places/{id}` endpoints.
4. Go to **APIs & Services → Credentials → Create credentials → API key**.
   Copy the key (looks like `AIza...`).

## 2. Restrict the key (recommended)

Because this key lives only on your server, restrict it tightly:

- **Application restriction:** set **IP addresses** and add your backend
  server's public IP(s). (Do *not* use an Android/iOS restriction — those are
  for keys embedded in apps; this one is server-side.)
- **API restriction:** restrict the key to **Places API (New)** only.

## 3. Give the backend the key

Set the environment variable wherever the Django process runs.

Local development:

```bash
export GOOGLE_PLACES_API_KEY="AIza...your-key..."
python manage.py runserver
```

systemd / production (example):

```ini
# in the [Service] section of your unit file
Environment="GOOGLE_PLACES_API_KEY=AIza...your-key..."
```

Docker:

```bash
docker run -e GOOGLE_PLACES_API_KEY="AIza...your-key..." ...
```

Restart the backend after setting it. That's it — the modal will start showing
address suggestions immediately.

## 4. Run the migration

This feature adds three columns to `Page` (`event_latitude`,
`event_longitude`, `event_place_id`). Apply the migration once:

```bash
python manage.py migrate
```

## Cost note

The `session_token` passed from the app groups all the keystrokes of one search
plus the final Place Details lookup into a single billed "Autocomplete session,"
which is far cheaper than billing each keystroke. The proxy also ignores inputs
shorter than 2 characters to avoid needless calls. Set a billing budget/alert in
Google Cloud if you want a hard ceiling.
