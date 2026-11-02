# Deploying to AWS Lightsail

Everything runs on one small server with Docker:

- **api**: the website and API
- **worker**: sends reminders and checks councils
- **selenium**: a browser for councils that need one
- **db**: Postgres, plus a daily backup
- **caddy**: HTTPS, with certificates that renew themselves

Allow about 30 minutes. Replace `binny.example.co.uk` with your domain throughout.

## 1. Create the server

1. Go to https://lightsail.aws.amazon.com and click **Create instance**.
2. Region: **London (eu-west-2)**.
3. Platform **Linux/Unix**, blueprint **OS Only → Ubuntu 24.04 LTS**.
4. Plan: **2 GB RAM or more** (4 GB is comfortable with the browser running).
5. Name it `binny` and click **Create instance**.

## 2. Static IP and firewall

1. Lightsail → **Networking** → **Create static IP** → attach it to `binny`. Note the IP.
2. Open the instance → **Networking** → **IPv4 Firewall** → **Add rule**: add **HTTPS (443)**.
   SSH (22) and HTTP (80) are open already. Keep 80 open: Caddy needs it to get certificates.

## 3. Point your domain at it

At your DNS provider, add an **A record**: `binny.example.co.uk` → the static IP.
Check it before going on (it can take a few minutes):

```
dig +short binny.example.co.uk
```

## 4. Install Docker

In Lightsail, click **Connect using SSH** (or `ssh ubuntu@<static IP>`), then:

```
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
exit
```

Connect again so the group change applies. If the plan has 2 GB of RAM, add 2 GB of swap too:

```
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 5. Get the code

```
git clone https://github.com/adnan-atl/binbot.git
cd binbot
git checkout main
```

If the repo is private, git asks for a username and password. Use a GitHub personal access
token (fine-grained, read-only on this repo) as the password. Use the branch you're deploying
(`main` once the work is merged).

## 6. Fill in the settings

```
cp deploy/.env.production.example deploy/.env.production
nano deploy/.env.production
```

1. Fill in every line marked **REQUIRED**.
2. Make up a database password with `openssl rand -hex 24` and put it in **both**
   `POSTGRES_PASSWORD` and `DATABASE_URL`.
3. Save with Ctrl+O, Enter, then Ctrl+X.

## 7. Start it

```
docker compose -f deploy/docker-compose.prod.yml up -d --build
```

The first build takes a few minutes. Then check:

```
docker compose -f deploy/docker-compose.prod.yml ps
curl https://binny.example.co.uk/api/health
```

The health check should return `{"ok":true,...}`, and the site should load at
https://binny.example.co.uk.

## 8. Connect WhatsApp

1. In the Meta app dashboard → **WhatsApp → Configuration → Webhook → Edit**:
   - Callback URL: `https://binny.example.co.uk/webhooks/meta`
   - Verify token: the same `META_VERIFY_TOKEN` as in `.env.production`
2. Click **Verify and save**.
3. Under **Webhook fields**, subscribe to **messages**.
4. Submit the two message templates in `docs/whatsapp-templates.md` and wait for approval.
5. Test end to end:
   - sign up on the site with your own number
   - tap **Say hi on WhatsApp** and send the message
   - the welcome message should arrive within seconds

## Everyday commands

Run these from the `binbot` folder.

| What | Command |
|---|---|
| See what's running | `docker compose -f deploy/docker-compose.prod.yml ps` |
| Follow the logs | `docker compose -f deploy/docker-compose.prod.yml logs -f api worker` |
| Restart after changing settings | `docker compose -f deploy/docker-compose.prod.yml up -d` |
| Stop everything | `docker compose -f deploy/docker-compose.prod.yml down` (the data is kept) |

## Updating

```
cd binbot
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
ssh ubuntu@<static IP> "cd binbot && docker compose -f deploy/docker-compose.prod.yml cp backup:/backups/binny-2026-11-02.sql.gz ."
scp ubuntu@<static IP>:binbot/binny-2026-11-02.sql.gz .
```

Lightsail **snapshots** (instance → Snapshots → enable automatic) are a good second copy of
the whole server.

## Restoring a backup

This replaces the current data with the backup. Use the file name from the list above.

```
cd binbot
C="docker compose -f deploy/docker-compose.prod.yml"
$C stop api worker
$C exec backup sh -c 'export PGUSER=$POSTGRES_USER PGPASSWORD=$POSTGRES_PASSWORD;
  dropdb --if-exists $POSTGRES_DB && createdb $POSTGRES_DB &&
  gunzip -c /backups/binny-2026-11-02.sql.gz | psql -q $POSTGRES_DB'
$C start api worker
```
