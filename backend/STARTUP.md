# Starting the WEMIX Backend

Two sets of steps below: **first-time setup** (only needed once per
machine) and **daily startup** (every time you sit down to work on it).

---

## First-time setup (once per machine)

```bash
cd /var/www/html/habari_platform
```

**1. Create and activate the virtual environment.**

```bash
python3 -m venv venv
source venv/bin/activate
```

Your prompt should now start with `(venv)`. Keep it activated for every
command below — if you ever see errors mentioning `~/.local` paths
instead of `venv/lib/...`, you're not actually in the venv; re-run
`source venv/bin/activate`.

**2. Install dependencies.**

```bash
pip install -r requirements.txt
```

**3. Set up PostgreSQL** (only if you haven't already).

```bash
sudo systemctl status postgresql   # confirm it's running
sudo -u postgres psql
```

Inside the `psql` prompt:

```sql
CREATE USER habari WITH PASSWORD 'habari';
CREATE DATABASE habari_platform OWNER habari;
GRANT ALL PRIVILEGES ON DATABASE habari_platform TO habari;
\q
```

(Use a stronger password if you like — just keep it consistent with step 4.)

**4. Create your `.env` file.**

```bash
cp .env.example .env
```

Then edit `.env` and make sure at minimum:

```
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1,0.0.0.0
CSRF_TRUSTED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000
DATABASE_URL=postgres://habari:habari@localhost:5432/habari_platform
```

(Leave `DEBUG=True` for local dev — setting it to `False` locally forces
HTTPS redirects that the dev server can't handle, and causes confusing
"You're accessing the development server over HTTPS" errors.)

**5. Run migrations and create an admin user.**

```bash
python3 manage.py migrate
python3 manage.py createsuperuser
```

---

## Daily startup (every time)

```bash
cd /var/www/html/habari_platform
source venv/bin/activate
python3 manage.py runserver
```

Leave that running. In a **second terminal**, you can hit the API:

```bash
curl http://127.0.0.1:8000/api/news/listings/
```

Django admin is at: `http://127.0.0.1:8000/admin/`
(There is no page at `http://127.0.0.1:8000/` itself — that's expected,
this is an API-only backend. Always hit a specific `/api/...` or
`/admin/` path.)

To stop the server: `Ctrl+C` in the terminal it's running in.

---

## If something's not working

**"Command 'python' not found"**
Use `python3` instead — this system doesn't have the `python` alias.
Inside an activated venv, `python` should work too; if it doesn't, the
venv isn't actually active (check for `(venv)` in your prompt and
re-run `source venv/bin/activate`).

**"Error: That port is already in use"**
Something's already running on 8000 (maybe a previous `runserver` you
forgot to stop). Free it, then retry:

```bash
fuser -k 8000/tcp
python3 manage.py runserver
```

**"DisallowedHost" error mentioning 127.0.0.1**
Your `.env` is missing `127.0.0.1` in `ALLOWED_HOSTS`. Fix:

```bash
sed -i "s|^ALLOWED_HOSTS=.*|ALLOWED_HOSTS=localhost,127.0.0.1,0.0.0.0|" .env
```

Then restart the server.

**"You're accessing the development server over HTTPS, but it only
supports HTTP" / garbled bytes in the log**
Something (usually a leftover browser tab) is trying to reach
`127.0.0.1:8000` over `https://`. Find and close it:

```bash
sudo ss -tnp | grep :8000
```

Close whatever's shown (browser tab, or `pkill -f chrome` if you can't
find it) — it doesn't break the server, it's just noisy.

**"password authentication failed for user habari"**
Your `.env`'s `DATABASE_URL` password doesn't match what Postgres has.
Either reset the Postgres password to match `.env`, or update `.env` to
match Postgres:

```bash
sudo -u postgres psql -c "ALTER USER habari WITH PASSWORD 'habari';"
```

**Checking everything is healthy without starting the server:**

```bash
python3 manage.py check
```

Should print `System check identified no issues (0 silenced).`

---

## Quick reference: all commands in order

```bash
cd /var/www/html/habari_platform
source venv/bin/activate
python3 manage.py runserver
```

That's the whole daily startup once first-time setup is done.
