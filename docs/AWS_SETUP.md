# Putting it on AWS Lightsail

Clips Kitty Game Day runs on one small Lightsail server. The AI runs on
Meta's servers, so the server needs no graphics card: it only runs ffmpeg,
the app and Caddy for HTTPS. Setup is one command.

## 1. Make the server (AWS console)

1. Lightsail → **Create instance**. Choose **Linux/Unix**, then **OS Only** → **Ubuntu 24.04 LTS**.
2. Plan: **2 vCPUs, 4 GB memory**. That's the plan it was tested on: it clipped a 45-minute game in about 14 minutes.
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
- makes two passwords, for you and for the judges, without printing them;
- runs the app as a service that restarts on its own;
- gets an HTTPS certificate.

It takes about 5 minutes.

## 3. Sign in

1. In the same terminal, show your password: `sudo grep GAMEDAY_PASSWORD /etc/gameday.env`.
   Read it yourself; don't paste it anywhere else.
2. Open `https://203-0-113-7.sslip.io` and sign in as `owner`.
3. In **Setup**, save your Meta key and press **Test**.

## The judges' login

Judges sign in as `judge`, with a password of its own, so you never give out
yours. The judge login can upload games and see everything the dashboard
shows, but it can't change settings, keys, watched folders or anything for
the phone. Its games are never offered on your phone, and their Meta usage
counts toward the same budget.

- Show its password: `sudo grep GAMEDAY_JUDGE_PASSWORD /etc/gameday.env`.
- Put it in the submission's private field, with the user `judge`.
- A server set up before the judge login gets this password the next time
  you run the setup script.

## Using it

- **Games:** upload them on **Game day**, or copy them into `/srv/gameday-watch` with `scp`.
- **Games from a shared Google Drive folder:** the server can check one Drive
  folder every minute and clip each new game in it.
  1. On a computer with a browser and rclone, run
     `rclone authorize "drive" "eyJzY29wZSI6ImRyaXZlLnJlYWRvbmx5In0"`. It is
     a read-only sign-in: the server can't change or delete anything in your
     Drive. Copy what it prints.
  2. On the server, run `sudo bash scripts/setup-drive-sync.sh "Game Day"`
     with your folder's name, and paste it when asked.
  3. New games in that folder are copied into `/srv/gameday-watch`. Each
     video in it becomes a job.
- **Logs:** `journalctl -u gameday -f`.
- **Update:** `git pull`, then run the setup script again. Your passwords and data stay.
- **Bigger games:** uploads are capped at 4 GB. To raise the cap, run the
  script again with the new limit in GB, for example
  `sudo bash scripts/setup-lightsail.sh 203-0-113-7.sslip.io 20`. Keep it well
  under the disk's free space, since each game also needs room for its clips.
- **Data** is kept in `/var/lib/gameday`.

## When you're done

Delete the instance, and then the static IP, to stop the charges. A static
IP is only free while it's attached to an instance.
