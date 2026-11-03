# Putting it on AWS Lightsail

Clips Kitty Game Day runs on one small Lightsail server. The AI runs on
Meta's servers, so the server needs no graphics card: it only runs ffmpeg,
the app and Caddy for HTTPS. Setup is one command.

## 1. Make the server (AWS console)

1. Lightsail → **Create instance**. Choose **Linux/Unix**, then **OS Only** → **Ubuntu 24.04 LTS**.
2. Plan: **2 vCPUs, 4 GB memory**. 2 GB also works, but 4 GB leaves room for long games.
3. Give it a name and press **Create instance**.
4. **Networking** → **Create static IP** and attach it to the instance.
5. On the instance's **Networking** tab, under the IPv4 firewall, add **HTTPS (443)**. SSH (22) and HTTP (80) are there already.

## 2. Install it (one command)

Open the instance's browser terminal (**Connect using SSH**) and run the
commands below. Replace `203-0-113-7` with your static IP, written with dashes:

```
git clone https://github.com/ColinGPT9/clips-kitty-gameday.git
cd clips-kitty-gameday
sudo bash scripts/setup-lightsail.sh 203-0-113-7.sslip.io
```

`sslip.io` turns that name into your IP, so there's no DNS to set up.

The script:
- installs ffmpeg, Python and Caddy;
- installs the app;
- runs the app as a service that restarts on its own;
- gets an HTTPS certificate.

It takes about 5 minutes.

## 3. Sign in

1. In the same terminal, show your password: `sudo grep GAMEDAY_PASSWORD /etc/gameday.env`.
   Read it yourself; don't paste it anywhere else.
2. Open `https://203-0-113-7.sslip.io` and sign in as `owner`.
3. In **Setup**, save your Meta key and press **Test**.

## Using it

- **Games:** upload them on **Game day**, or copy them into `/srv/gameday-watch` with `scp`.
- **Logs:** `journalctl -u gameday -f`.
- **Update:** `git pull`, then run the setup script again. Your password and data stay.
- **Bigger games:** uploads are capped at 4 GB. To raise the cap, run the
  script again with the new limit in GB, for example
  `sudo bash scripts/setup-lightsail.sh 203-0-113-7.sslip.io 20`. Keep it well
  under the disk's free space, since each game also needs room for its clips.
- **Data** is kept in `/var/lib/gameday`.

## When you're done

Delete the instance, and then the static IP, to stop the charges. A static
IP is only free while it's attached to an instance.
