# Deploying Binjamin

Everything runs on one small server in Alfred's AWS account (Lightsail, London), with Docker:

- **api**: the website and API
- **worker**: sends reminders and checks councils
- **scraper**: runs the council scrapers, locked down with no database access and no secrets
- **selenium**: a browser for councils that need one
- **db**: Postgres, plus a daily backup
- **caddy**: HTTPS, with certificates that renew themselves

The domain is **binjamin.uk**. Allow about an hour the first time.

## Before you start

- [ ] An **SSO login** (IAM Identity Center) to the AWS account Binjamin runs in, with access to Lightsail and the London region (eu-west-2) enabled. Not the root user, and not a shared IAM user. Best is a separate Binjamin account inside Alfred's AWS organisation, so it can't touch anything else Alfred runs and its bill shows on its own
- [ ] Free accounts for the monitoring: **Cloudflare** (bot check), **healthchecks.io** (reminder alert), **sentry.io** (error alerts, EU region) and **uptimerobot.com** (site-down alert)
- [ ] **binjamin.uk** registered to Alfred, with auto-renew on, and someone who can add a DNS record at its registrar
- [ ] These to hand for the settings file (step 6). Never paste them in chat, a ticket or git:
  - Meta: permanent token, phone number ID, app secret, and the business number
  - a new **OS Places** API key (osdatahub.os.uk). Make a fresh one: the old one was shared in chat
  - optional: an Ideal Postcodes key

## 1. Create the server

1. Sign in to Alfred's AWS, open **Lightsail** (https://lightsail.aws.amazon.com) and check the region at the top says **London**.
2. **Create instance** → Platform **Linux/Unix** → blueprint **OS Only → Ubuntu 24.04 LTS**.
3. Plan: **4 GB RAM** ($24 a month). The council browser is memory-hungry; 2 GB works with the swap file in step 4 but runs close to the edge.
4. Name it `binjamin` and click **Create instance**.

## 2. Static IP and firewall

1. Lightsail → **Networking** → **Create static IP** → attach it to `binjamin`. Note the IP.
2. Open the instance → **Networking** → **IPv4 Firewall**:
   - **Add rule**: **HTTPS (443)**.
   - Edit the **SSH (22)** rule → tick **Restrict to IP address** → tick **Allow Lightsail browser SSH** and add no other IPs → **Save**. Now only the **Connect using SSH** button in Lightsail can open a terminal, behind your AWS login. Nothing else needs SSH: deploys are pulled by the server (step 9).
   - Keep **HTTP (80)** open: Caddy needs it to get certificates.
3. Instance → **Snapshots** → turn on **Automatic snapshots** (daily, kept 7 days).

## 3. Point binjamin.uk at it

At binjamin.uk's registrar, add an **A record**: host `@` (the bare domain) → the static IP. TTL 300
if it asks. Then check it from any terminal (can take a few minutes):

```
dig +short binjamin.uk
```

It should print the static IP. Don't start step 7 until it does, or the HTTPS certificate fails
(Caddy retries, but it's quicker to wait).

## 4. Install Docker

In Lightsail, open the instance and click **Connect using SSH** (a terminal opens in the browser), then:

```
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
exit
```

Click **Connect using SSH** again so the Docker group applies.

## 5. Get the code

The server reads the repo with its own read-only key, so updates (and auto-deploy) never ask for a password:

```
ssh-keygen -t ed25519 -N "" -f ~/.ssh/github -C "binjamin server" -q
printf 'Host github.com\n  IdentityFile ~/.ssh/github\n' >> ~/.ssh/config
ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null
cat ~/.ssh/github.pub
```

Copy the line it prints. On GitHub: **adnan-atl/binbot → Settings → Deploy keys → Add deploy key**,
title `binjamin server`, paste it, leave **Allow write access** unticked, **Add key**. Then:

```
git clone git@github.com:adnan-atl/binbot.git ~/binbot
cd ~/binbot
```

## 6. Fill in the settings

```
cp deploy/.env.production.example deploy/.env.production
openssl rand -hex 24      # database password: copy it
openssl rand -hex 24      # Meta verify token: copy it too
nano deploy/.env.production
```

1. Fill in every line marked **REQUIRED**.
2. Put the database password in **both** `POSTGRES_PASSWORD` and `DATABASE_URL`.
3. **Bot check:** Cloudflare dashboard → **Turnstile** → **Add widget**, hostname `binjamin.uk`, mode **Managed**. Put the site key in `TURNSTILE_SITE_KEY` and the secret key in `TURNSTILE_SECRET`.
4. **Reminder alert:** healthchecks.io → **Add check**, period **5 minutes**, grace **10 minutes**, and add your email (or Slack) under **Integrations**. Put the check's ping URL in `HEARTBEAT_URL`. The worker pings it every minute, so if reminders stop sending (worker down, server down, sends failing) you hear within about 15 minutes.
5. **Error alerts:** sentry.io → sign up and choose the **EU** data region → **Create project**,
   platform **FastAPI**, alert on every new issue. Put the **DSN** in `SENTRY_DSN`. Unexpected errors
   in the site or the worker then email you, with phone numbers, postcodes and form data removed.
6. Save with Ctrl+O, Enter, then Ctrl+X.

## 7. Start it

```
docker compose -f deploy/docker-compose.prod.yml up -d --build
```

The first build takes a few minutes. Then check:

```
docker compose -f deploy/docker-compose.prod.yml ps
curl https://binjamin.uk/api/health
```

The health check returns `{"ok":true,...}` and the site loads at https://binjamin.uk.

## 8. Connect WhatsApp

1. Meta app dashboard → **WhatsApp → Configuration → Webhook → Edit**:
   - Callback URL: `https://binjamin.uk/webhooks/meta`
   - Verify token: the same `META_VERIFY_TOKEN` as in `.env.production`
2. **Verify and save**, then under **Webhook fields** subscribe to **messages**.
3. Test end to end with your own number: sign up on the site, tap **Say hi on WhatsApp**, send it.
   The welcome message arrives within seconds.

Until the real number is registered and the Meta app is published, only the numbers you've added as
test recipients get messages. The site itself works for everyone.

## 9. Auto-deploy (about 5 minutes)

After this, every push to main that passes the tests goes live by itself within a few minutes, and a
push with failing tests never deploys. Nothing logs in to the server: GitHub moves a `live` branch to
the tested commit, and the server checks `live` every 2 minutes and rebuilds when it moves. If the
new version doesn't come up healthy, it goes back to the previous one by itself.
Every run (every 2 minutes, deploy or not) also checks that all containers are running and the
site answers through Caddy: anything stopped is started again, and if that doesn't fix it the team
gets one Slack alert in SLACK_ALERTS_CHANNEL (and another when it's back). Runs never overlap.

On the server (browser SSH terminal):

```
sudo tee /etc/systemd/system/binjamin-update.service >/dev/null <<'UNIT'
[Unit]
Description=Binjamin: deploy the live branch if it moved
After=docker.service network-online.target
[Service]
Type=oneshot
User=ubuntu
ExecStart=/home/ubuntu/binbot/deploy/autoupdate.sh
UNIT
sudo tee /etc/systemd/system/binjamin-update.timer >/dev/null <<'UNIT'
[Unit]
Description=Binjamin: check for a new version every 2 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now binjamin-update.timer
```

**See what it did:** `journalctl -u binjamin-update -n 50`

**Pause deploys** (say, during an incident): `sudo systemctl stop binjamin-update.timer`, and
`start` to resume.

Each release shows under the repo's **Actions** tab as **deploy**.

## 10. Site-down alert (2 minutes)

The reminder alert (step 6) watches the worker. This watches the website: uptimerobot.com → sign
up (free) → **New monitor** → **HTTP(s)**, URL `https://binjamin.uk/api/ready`, every 5 minutes,
alert by email. `/api/ready` only answers OK when the site, the database and the scraper container
are all up, so it also catches a database that's fallen over.

## Everyday commands

Run these from `~/binbot`.

| What | Command |
|---|---|
| See what's running | `docker compose -f deploy/docker-compose.prod.yml ps` |
| Follow the logs | `docker compose -f deploy/docker-compose.prod.yml logs -f api worker` |
| Restart after changing settings | `docker compose -f deploy/docker-compose.prod.yml up -d` |
| Stop everything | `docker compose -f deploy/docker-compose.prod.yml down` (the data is kept) |
| Messages the bot didn't understand | `docker compose -f deploy/docker-compose.prod.yml exec db psql -U binny binny -c "select detected_at, diff->>'text' from change_log where kind = 'unrecognised' order by detected_at desc limit 50;"` |

## Updating by hand

With auto-deploy set up you don't need this. Otherwise:

```
cd ~/binbot
git pull
docker compose -f deploy/docker-compose.prod.yml up -d --build
```

Database migrations run automatically when the api starts.

## Backups

A backup is taken once a day into the `backups` volume, and the last 14 days are kept.

**List them:**

```
docker compose -f deploy/docker-compose.prod.yml exec backup ls -lh /backups
```

**Take one now:**

```
docker compose -f deploy/docker-compose.prod.yml exec backup sh -c \
  'PGUSER=$POSTGRES_USER PGPASSWORD=$POSTGRES_PASSWORD pg_dump --no-owner $POSTGRES_DB | gzip > /backups/binny-manual.sql.gz'
```

**Copy one to your computer:** run this on your computer, not the server. First copy the file
out of the volume on the server:

```
ssh ubuntu@<static IP> "cd ~/binbot && docker compose -f deploy/docker-compose.prod.yml cp backup:/backups/binny-2026-11-02.sql.gz ."
scp ubuntu@<static IP>:binbot/binny-2026-11-02.sql.gz .
```

The daily Lightsail **snapshots** (step 2) are the second copy, of the whole server.

**Test a restore before launch, then every few months.** A backup you haven't restored isn't a
backup. This loads the latest daily backup into a throwaway database on the same server, without
touching the real one:

```
cd ~/binbot
C="docker compose -f deploy/docker-compose.prod.yml"
F=$($C exec -T backup sh -c 'ls -t /backups/*.sql.gz | head -1')
$C cp "backup:$F" /tmp/latest.sql.gz
docker run -d --name restore-test -e POSTGRES_PASSWORD=test postgres:16-alpine && sleep 8
gunzip -c /tmp/latest.sql.gz | docker exec -i restore-test psql -q -U postgres
docker exec restore-test psql -U postgres -c "select count(*) from users;"
$C exec db psql -U binny binny -c "select count(*) from users;"
docker rm -f restore-test && rm /tmp/latest.sql.gz
```

The two counts should match (or nearly, if someone signed up since last night's backup).

Don't test a snapshot by starting a new instance from it while production is running: everything
starts by itself on boot, including the worker, and residents would get every reminder twice.

## Off-server backups (about 15 minutes)

The daily backups above live on the server, so losing the server loses them too. This copies
each one to an S3 bucket in the same AWS account, every 6 hours. The server gets a key that can
**add** files to the bucket but can't read, list contents of other buckets, or delete anything,
so someone who broke into the server still couldn't wipe the backups.

In the AWS console (region **London**):

1. **S3 → Create bucket**: name `binjamin-backups-` plus a few random letters. Leave **Block all
   public access** on. **Bucket Versioning: Enable**. **Create bucket**.
2. Open the bucket → **Management → Create lifecycle rule**: name `keep 35 days`, apply to all
   objects, tick **Expire current versions** (35 days) and **Permanently delete noncurrent
   versions** (7 days). This is also what makes deleted accounts disappear from backups.
3. **IAM → Users → Create user**: `binjamin-backup-upload`, no console access, no groups. Open it →
   **Add permissions → Create inline policy → JSON**, paste this with your bucket name, save:

   ```json
   {"Version": "2012-10-17", "Statement": [
     {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::BUCKET"},
     {"Effect": "Allow", "Action": "s3:PutObject", "Resource": "arn:aws:s3:::BUCKET/db/*"}]}
   ```
4. The user → **Security credentials → Create access key** → **Application running outside AWS**.
   Put the bucket name and both halves of the key in `.env.production` (`BACKUP_S3_BUCKET`,
   `BACKUP_AWS_ACCESS_KEY_ID`, `BACKUP_AWS_SECRET_ACCESS_KEY`). Never paste the key in chat.
5. Optional: a second healthchecks.io check (period **1 day**, grace **12 hours**) in
   `BACKUP_HEARTBEAT_URL`, so you hear if the copies stop.
6. `docker compose -f deploy/docker-compose.prod.yml up -d`, then check
   `docker compose -f deploy/docker-compose.prod.yml logs offsite` says "backups copied".

**To get one back** (server gone): sign in to the console with your own login, open the bucket →
`db/` → the file → **Download**, and copy it to the new server. Then restore it as below.

## Restoring a backup

This replaces the current data with the backup. Use the file name from the list above.

```
cd ~/binbot
C="docker compose -f deploy/docker-compose.prod.yml"
$C stop api worker
$C exec backup sh -c 'export PGUSER=$POSTGRES_USER PGPASSWORD=$POSTGRES_PASSWORD;
  dropdb --if-exists $POSTGRES_DB && createdb $POSTGRES_DB &&
  gunzip -c /backups/binny-2026-11-02.sql.gz | psql -q $POSTGRES_DB'
$C start api worker
```
